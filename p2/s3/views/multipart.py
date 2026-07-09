"""p2 S3 Multipart Upload — Zero-Copy Assembly.

Architecture (per new_architecture.md)
---------------------------------------
Part uploads write bytes to the volume pool exactly like regular PUTs.
The block coordinate ``{vol_uuid, offset, length}`` is stored in LMDB under a
temporary namespace:  ``/.multipart/<upload_id>/<part_number>``

CompleteMultipartUpload assembles the object by:
1. Reading all part block coords from LMDB.
2. Concatenating them into a single ``blocks`` array.
3. Writing ONE metadata entry for the target key in LMDB.

Zero physical bytes are moved or copied.  The operation is O(1) I/O.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from xml.etree import ElementTree

from django.http.response import HttpResponse
from django.utils.timezone import now

from p2.s3.constants import XML_NAMESPACE
from p2.s3.http import XMLResponse
from p2.s3.utils import decode_aws_chunked, iter_request_body
from p2.s3.views.common import S3View
from p2.s3.volume_pool import BlockCoord, VolumePool
from p2.s3.volume_writer import write_block
from p2.s3.cache import invalidate_metadata, invalidate_volume_global

LOGGER = logging.getLogger(__name__)


class MultipartUploadView(S3View):
    """Zero-copy multipart uploads via block metadata assembly."""

    async def dispatch(self, request, bucket, path):
        self.request = request
        method = request.method
        if method == "POST":
            if "uploads" in request.GET:
                return await self._create(request, bucket, path)
            if "uploadId" in request.GET:
                return await self._complete(request, bucket, path)
        elif method == "PUT":
            if "partNumber" in request.GET and "uploadId" in request.GET:
                return await self._upload_part(request, bucket, path)
        elif method == "DELETE":
            if "uploadId" in request.GET:
                return await self._abort(request, bucket, path)
        elif method == "GET":
            if "uploadId" in request.GET:
                return await self._list_parts(request, bucket, path)
        return HttpResponse(status=405)

    # ── Initiate ──────────────────────────────────────────────────────────

    async def _create(self, request, bucket, path):
        volume = await self.get_volume(request.user, bucket, "write")
        upload_id = uuid.uuid4().hex
        engine = await self.get_engine(volume)
        meta_key = f"/.multipart/{upload_id}/_meta"
        await asyncio.to_thread(engine.put, meta_key, json.dumps({
            "target_path": path,
            "content_type": request.META.get("CONTENT_TYPE", "application/octet-stream"),
        }))
        root = ElementTree.Element(f"{{{XML_NAMESPACE}}}InitiateMultipartUploadResult")
        ElementTree.SubElement(root, "Bucket").text = bucket
        ElementTree.SubElement(root, "Key").text = path.lstrip("/")
        ElementTree.SubElement(root, "UploadId").text = upload_id
        return XMLResponse(root)

    # ── Upload Part ───────────────────────────────────────────────────────

    async def _upload_part(self, request, bucket, path):
        volume = await self.get_volume(request.user, bucket, "write")
        upload_id = request.GET["uploadId"]
        part_number = int(request.GET["partNumber"])
        engine = await self.get_engine(volume)
        pool = VolumePool.get()

        content_encoding = request.META.get("HTTP_CONTENT_ENCODING", "")
        decoded_length = request.META.get("HTTP_X_AMZ_DECODED_CONTENT_LENGTH")
        is_aws_chunked = "aws-chunked" in content_encoding or decoded_length

        md5_h = hashlib.md5()
        chunks: list[bytes] = []

        if is_aws_chunked:
            raw = await asyncio.to_thread(request.read)
            data = decode_aws_chunked(raw)
            chunks = [data]
        else:
            async for chunk in iter_request_body(request, 4 * 1024 * 1024):
                chunks.append(chunk)

        data = b"".join(chunks)
        blob_size = len(data)
        md5_h.update(data)
        final_md5 = md5_h.hexdigest()

        # Allocate block in volume pool and write data
        part_key = f"/.multipart/{upload_id}/{part_number}"
        if blob_size > 0:
            handle, offset = await asyncio.to_thread(pool.allocate_block, blob_size)
            block = BlockCoord(vol_uuid=handle.uuid_hex, offset=offset, length=blob_size)
            part_meta = json.dumps({
                "block": block.to_dict(),
                "md5": final_md5,
                "size": blob_size,
            })
            await write_block(handle, offset, data, engine, part_key, part_meta)
        else:
            part_meta = json.dumps({"block": None, "md5": final_md5, "size": 0})
            await asyncio.to_thread(engine.put, part_key, part_meta)

        response = HttpResponse(status=200)
        response["ETag"] = f'"{final_md5}"'
        return response

    # ── Complete ──────────────────────────────────────────────────────────

    async def _complete(self, request, bucket, path):
        volume = await self.get_volume(request.user, bucket, "write")
        upload_id = request.GET["uploadId"]
        engine = await self.get_engine(volume)

        try:
            root = ElementTree.fromstring(request.body)
        except Exception:
            return HttpResponse(status=400)

        # Parse requested part list from XML (namespace-agnostic)
        parts_requested: list[dict] = []
        part_els = [el for el in root.iter() if el.tag == "Part" or el.tag.endswith("}Part")]
        for part_el in part_els:
            num_el = next((c for c in part_el if c.tag == "PartNumber" or c.tag.endswith("}PartNumber")), None)
            etag_el = next((c for c in part_el if c.tag == "ETag" or c.tag.endswith("}ETag")), None)
            if num_el is not None and etag_el is not None:
                parts_requested.append({"number": int(num_el.text), "etag": etag_el.text.strip('"')})

        if not parts_requested:
            return HttpResponse(status=400)

        # Load stored part block coords from LMDB and validate ETags
        all_blocks: list[BlockCoord] = []
        total_size = 0
        for p in sorted(parts_requested, key=lambda x: x["number"]):
            num = p["number"]
            raw = await asyncio.to_thread(engine.get, f"/.multipart/{upload_id}/{num}")
            if not raw:
                return HttpResponse(status=400)
            part_attr = json.loads(raw)
            if part_attr["md5"] != p["etag"]:
                return HttpResponse(status=400)
            block_dict = part_attr.get("block")
            if block_dict:
                all_blocks.append(BlockCoord.from_dict(block_dict))
                total_size += part_attr["size"]

        # Load upload metadata
        meta_raw = await asyncio.to_thread(engine.get, f"/.multipart/{upload_id}/_meta")
        m_attr = json.loads(meta_raw) if meta_raw else {}

        # Existing object check for stats
        existing_raw = await asyncio.to_thread(engine.get, path)
        existing_size = 0
        existing_counted = False
        if existing_raw:
            ex = json.loads(existing_raw)
            if not ex.get("is_folder", False):
                existing_size = int(ex.get("size", 0) or 0)
                existing_counted = True

        # Versioning
        bucket_versioning = (volume.tags or {}).get("versioning") == "true"
        new_vid = None
        if bucket_versioning:
            from p2.s3.versioning import archive_version, new_version_id as _new_vid
            if existing_raw:
                await archive_version(engine, path, existing_raw)
            new_vid = _new_vid()

        # Build multipart ETag: "md5-of-md5s"-<partcount>
        part_etags = [p["etag"] for p in sorted(parts_requested, key=lambda x: x["number"])]
        combined = hashlib.md5(b"".join(binascii.unhexlify(e) for e in part_etags)).hexdigest()
        final_etag = f"{combined}-{len(parts_requested)}"

        # Zero-copy assembly: write single metadata entry with all block coords
        now_ts = str(now())
        payload = {
            "size": total_size,
            "mime": m_attr.get("content_type", "application/octet-stream"),
            "blocks": [b.to_dict() for b in all_blocks],
            "etag": final_etag,
            "mtime": now_ts,
            "ctime": now_ts,
            "is_folder": False,
        }
        if new_vid:
            payload["version_id"] = new_vid
        payload_json = json.dumps(payload)

        # LMDB commit only — no disk I/O for the merge
        await asyncio.to_thread(engine.put, path, payload_json)

        if new_vid:
            from p2.s3.versioning import _version_lmdb_key
            lk = _version_lmdb_key(path, new_vid)
            await asyncio.to_thread(engine.put_raw, lk, payload_json.encode("utf-8"))

        # Clean up temporary multipart keys from LMDB
        items = await asyncio.to_thread(engine.list, f"/.multipart/{upload_id}/", None, 10000)
        for key, _ in items:
            await asyncio.to_thread(engine.delete, key)

        invalidate_metadata(volume.uuid.hex, path)
        if existing_raw:
            invalidate_volume_global(volume.name)

        from p2.core.volume_stats import adjust_volume_stats
        await adjust_volume_stats(
            volume,
            object_delta=0 if existing_counted else 1,
            bytes_delta=total_size - existing_size,
        )

        res = ElementTree.Element(f"{{{XML_NAMESPACE}}}CompleteMultipartUploadResult")
        ElementTree.SubElement(res, "Location").text = f"http://{request.get_host()}/{bucket}{path}"
        ElementTree.SubElement(res, "Bucket").text = bucket
        ElementTree.SubElement(res, "Key").text = path.lstrip("/")
        ElementTree.SubElement(res, "ETag").text = f'"{final_etag}"'
        return XMLResponse(res)

    # ── Abort ─────────────────────────────────────────────────────────────

    async def _abort(self, request, bucket, path):
        volume = await self.get_volume(request.user, bucket, "write")
        upload_id = request.GET["uploadId"]
        engine = await self.get_engine(volume)
        # Remove all temporary LMDB keys (physical bytes reclaimed by compaction)
        items = await asyncio.to_thread(engine.list, f"/.multipart/{upload_id}/", None, 10000)
        for key, _ in items:
            await asyncio.to_thread(engine.delete, key)
        return HttpResponse(status=204)

    # ── List Parts ────────────────────────────────────────────────────────

    async def _list_parts(self, request, bucket, path):
        volume = await self.get_volume(request.user, bucket, "read")
        upload_id = request.GET["uploadId"]
        engine = await self.get_engine(volume)

        res = ElementTree.Element(f"{{{XML_NAMESPACE}}}ListPartsResult")
        ElementTree.SubElement(res, "Bucket").text = bucket
        ElementTree.SubElement(res, "Key").text = path.lstrip("/")
        ElementTree.SubElement(res, "UploadId").text = upload_id

        items = await asyncio.to_thread(engine.list, f"/.multipart/{upload_id}/", None, 10000)
        sorted_parts = []
        for key, val in items:
            pnum = key.split("/")[-1]
            if pnum != "_meta":
                try:
                    sorted_parts.append((int(pnum), json.loads(val)))
                except (ValueError, json.JSONDecodeError):
                    pass
        sorted_parts.sort(key=lambda x: x[0])

        for num, attr in sorted_parts:
            part_el = ElementTree.SubElement(res, "Part")
            ElementTree.SubElement(part_el, "PartNumber").text = str(num)
            ElementTree.SubElement(part_el, "ETag").text = f'"{attr["md5"]}"'
            ElementTree.SubElement(part_el, "Size").text = str(attr["size"])

        return XMLResponse(res)


# Fix missing import
import binascii
