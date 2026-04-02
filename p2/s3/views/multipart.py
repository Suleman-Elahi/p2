"""p2 S3 Multipart Upload views"""
import asyncio
import logging
import os
import shutil
import uuid
import hashlib
import json
from time import time
from xml.etree import ElementTree

import aiofiles
from django.conf import settings
from django.http.response import HttpResponse
from django.utils.timezone import now

from p2.core.acl import VolumeACL
from p2.core.constants import ATTR_BLOB_HASH_MD5, ATTR_BLOB_SIZE_BYTES, ATTR_BLOB_STAT_MTIME, ATTR_BLOB_STAT_CTIME, ATTR_BLOB_MIME, ATTR_BLOB_IS_FOLDER
from p2.core.prefix_helper import make_absolute_path
from p2.s3.constants import XML_NAMESPACE
from p2.s3.http import XMLResponse
from p2.s3.views.common import S3View
from p2.s3.utils import decode_aws_chunked


LOGGER = logging.getLogger(__name__)

class MultipartUploadView(S3View):
    """Multipart-Object handling via LSM engine"""

    async def dispatch(self, request, bucket, path):
        if request.method == 'POST':
            if 'uploads' in request.GET:
                return await self._create_multipart(request, bucket, path)
            elif 'uploadId' in request.GET:
                return await self._complete_multipart(request, bucket, path)
        elif request.method == 'PUT':
            if 'partNumber' in request.GET and 'uploadId' in request.GET:
                return await self._upload_part(request, bucket, path)
        elif request.method == 'DELETE':
            if 'uploadId' in request.GET:
                return await self._abort_multipart(request, bucket, path)
        elif request.method == 'GET':
            if 'uploadId' in request.GET:
                return await self._list_parts(request, bucket, path)
                
        return HttpResponse(status=405)

    async def _create_multipart(self, request, bucket, path):
        volume = await self.get_volume(request.user, bucket, 'write')
        upload_id = uuid.uuid4().hex
        
        internal_key = f"/.multipart/{upload_id}/_meta"
        engine = await self.get_engine(volume)
        
        engine.put(internal_key, json.dumps({
            "target_path": path,
            "content_type": request.META.get('CONTENT_TYPE', 'application/octet-stream')
        }))

        root = ElementTree.Element("{%s}InitiateMultipartUploadResult" % XML_NAMESPACE)
        ElementTree.SubElement(root, "Bucket").text = bucket
        ElementTree.SubElement(root, "Key").text = path[1:] if path.startswith('/') else path
        ElementTree.SubElement(root, "UploadId").text = upload_id
        return XMLResponse(root)

    async def _upload_part(self, request, bucket, path):
        volume = await self.get_volume(request.user, bucket, 'write')
        upload_id = request.GET['uploadId']
        part_number = int(request.GET['partNumber'])
        
        blob_uuid = uuid.uuid4().hex
        dir_path = os.path.join("/storage/volumes", volume.uuid.hex, "parts", upload_id)
        os.makedirs(dir_path, exist_ok=True)
        fs_path = os.path.join(dir_path, f"{part_number}_{blob_uuid}")
        
        md5_hash = hashlib.md5()
        blob_size = 0
        
        # Read body and decode aws-chunked if needed
        data = await asyncio.to_thread(request.read)
        content_encoding = request.META.get('HTTP_CONTENT_ENCODING', '')
        decoded_length = request.META.get('HTTP_X_AMZ_DECODED_CONTENT_LENGTH')
        if 'aws-chunked' in content_encoding or decoded_length:
            data = decode_aws_chunked(data)
        
        async with aiofiles.open(fs_path, 'wb') as f:
            await f.write(data)
            md5_hash.update(data)
            blob_size = len(data)
                
        final_md5 = md5_hash.hexdigest()

        
        engine = await self.get_engine(volume)
        internal_key = f"/.multipart/{upload_id}/{part_number}"
        
        engine.put(internal_key, json.dumps({
            "fs_path": fs_path,
            "md5": final_md5,
            "size": blob_size
        }))
        
        response = HttpResponse(status=200)
        response['ETag'] = f'"{final_md5}"'
        return response

    async def _complete_multipart(self, request, bucket, path):
        volume = await self.get_volume(request.user, bucket, 'write')
        upload_id = request.GET['uploadId']
        engine = await self.get_engine(volume)
        
        try:
            body = request.body
            root = ElementTree.fromstring(body)
        except Exception:
            return HttpResponse(status=400)
            
        parts_to_merge = []
        for part_el in root.iter("{%s}Part" % XML_NAMESPACE):
            num = part_el.find("{%s}PartNumber" % XML_NAMESPACE)
            etag = part_el.find("{%s}ETag" % XML_NAMESPACE)
            parts_to_merge.append({
                "number": int(num.text),
                "etag": etag.text.strip('"')
            })
            
        if not parts_to_merge:
            for part_el in root.iter("Part"):
                num = part_el.find("PartNumber")
                etag = part_el.find("ETag")
                parts_to_merge.append({
                    "number": int(num.text),
                    "etag": etag.text.strip('"')
                })
            
        valid_parts = []
        for p in parts_to_merge:
            num = p["number"]
            meta_str = engine.get(f"/.multipart/{upload_id}/{num}")
            if not meta_str: return HttpResponse(status=400)
            attr = json.loads(meta_str)
            if attr["md5"] != p["etag"]: return HttpResponse(status=400)
            valid_parts.append(attr)
            
        blob_uuid = uuid.uuid4().hex
        dir_path = os.path.join("/storage/volumes", volume.uuid.hex, blob_uuid[0:2], blob_uuid[2:4])
        os.makedirs(dir_path, exist_ok=True)
        final_fs_path = os.path.join(dir_path, blob_uuid)
        internal_path = f"/internal-storage/volumes/{volume.uuid.hex}/{blob_uuid[0:2]}/{blob_uuid[2:4]}/{blob_uuid}"
        
        total_size = 0
        
        async with aiofiles.open(final_fs_path, 'wb') as outfile:
            for part in valid_parts:
                async with aiofiles.open(part["fs_path"], 'rb') as infile:
                    chunk = await infile.read(1 << 20)  # 1 MB
                    while chunk:
                        await outfile.write(chunk)
                        total_size += len(chunk)
                        chunk = await infile.read(1 << 20)  # 1 MB
                os.remove(part["fs_path"])
                engine.delete(f"/.multipart/{upload_id}/{part['number']}")
                
        final_etag = f"multipart-{len(valid_parts)}"
        
        meta_str = engine.get(f"/.multipart/{upload_id}/_meta")
        m_attr = json.loads(meta_str) if meta_str else {}
        engine.delete(f"/.multipart/{upload_id}/_meta")
        try:
            os.rmdir(os.path.join("/storage", volume.uuid.hex, "parts", upload_id))
        except OSError: pass

        engine.put(path, json.dumps({
            ATTR_BLOB_MIME: m_attr.get('content_type', 'application/octet-stream'),
            ATTR_BLOB_SIZE_BYTES: str(total_size),
            ATTR_BLOB_IS_FOLDER: False,
            ATTR_BLOB_STAT_MTIME: str(now()),
            ATTR_BLOB_STAT_CTIME: str(now()),
            'blob.p2.io/hash/md5': final_etag,
            'internal_path': internal_path
        }))
        
        res = ElementTree.Element("{%s}CompleteMultipartUploadResult" % XML_NAMESPACE)
        ElementTree.SubElement(res, "Location").text = f"http://{request.get_host()}/{bucket}{path}"
        ElementTree.SubElement(res, "Bucket").text = bucket
        ElementTree.SubElement(res, "Key").text = path[1:] if path.startswith('/') else path
        ElementTree.SubElement(res, "ETag").text = f'"{final_etag}"'
        
        return XMLResponse(res)

    async def _abort_multipart(self, request, bucket, path):
        volume = await self.get_volume(request.user, bucket, 'write')
        upload_id = request.GET['uploadId']
        engine = await self.get_engine(volume)
        
        prefix = f"/.multipart/{upload_id}/"
        items = engine.list(prefix, None, 10000)
        for key, val in items:
            try:
                attr = json.loads(val)
                fs_path = attr.get('fs_path')
                if fs_path: os.remove(fs_path)
            except Exception: pass
            engine.delete(key)
            
        try:
            shutil.rmtree(os.path.join("/storage", volume.uuid.hex, "parts", upload_id))
        except OSError: pass
            
        return HttpResponse(status=204)

    async def _list_parts(self, request, bucket, path):
        volume = await self.get_volume(request.user, bucket, 'read')
        upload_id = request.GET['uploadId']
        engine = await self.get_engine(volume)
        
        res = ElementTree.Element("{%s}ListPartsResult" % XML_NAMESPACE)
        ElementTree.SubElement(res, "Bucket").text = bucket
        ElementTree.SubElement(res, "Key").text = path[1:] if path.startswith('/') else path
        ElementTree.SubElement(res, "UploadId").text = upload_id
        
        prefix = f"/.multipart/{upload_id}/"
        items = engine.list(prefix, None, 10000)
        
        sorted_parts = []
        for key, val in items:
            pnum = key.split('/')[-1]
            if pnum != '_meta':
                sorted_parts.append((int(pnum), val))
        
        sorted_parts.sort(key=lambda x: x[0])
        for num, val in sorted_parts:
            attr = json.loads(val)
            part_el = ElementTree.SubElement(res, "Part")
            ElementTree.SubElement(part_el, "PartNumber").text = str(num)
            ElementTree.SubElement(part_el, "ETag").text = f'"{attr["md5"]}"'
            ElementTree.SubElement(part_el, "Size").text = str(attr["size"])
            
        return XMLResponse(res)
