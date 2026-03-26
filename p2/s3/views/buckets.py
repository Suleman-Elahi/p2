"""p2 S3 Bucket-related Views"""
import base64
import json
import logging
from xml.etree import ElementTree

from django.http import HttpResponse

from p2.core.acl import VolumeACL
from p2.core.constants import (ATTR_BLOB_HASH_MD5, ATTR_BLOB_IS_FOLDER,
                               ATTR_BLOB_SIZE_BYTES, ATTR_BLOB_STAT_MTIME)
from p2.core.models import Blob, Storage, Volume
from p2.core.prefix_helper import make_absolute_prefix
from p2.s3.constants import (TAG_S3_ACL, TAG_S3_DEFAULT_STORAGE,
                             TAG_S3_STORAGE_CLASS, XML_NAMESPACE)
from p2.s3.cors import (apply_cors_headers, build_cors_xml,
                        find_matching_rule, get_cors_rules, parse_cors_xml)
from p2.s3.errors import AWSAccessDenied, AWSNoSuchKey
from p2.s3.http import XMLResponse
from p2.s3.views.common import S3View

LOGGER = logging.getLogger(__name__)

# Canned ACL → p2 permission list
_CANNED_ACL_PERMS = {
    "private":                  [],
    "public-read":              ["read"],
    "public-read-write":        ["read", "write"],
    "authenticated-read":       ["read"],
}


def _encode_token(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode()).decode()


def _decode_token(token: str) -> str:
    try:
        return base64.urlsafe_b64decode(token.encode()).decode()
    except Exception:
        return ''


class BucketView(S3View):
    """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTBucketOps.html"""

    async def options(self, request, bucket):
        """CORS preflight for bucket-level requests."""
        origin = request.META.get("HTTP_ORIGIN", "")
        req_method = request.META.get("HTTP_ACCESS_CONTROL_REQUEST_METHOD", "GET")
        try:
            volume = await self.get_volume(request.user, bucket, "read")
        except Exception:
            return HttpResponse(status=403)
        rules = get_cors_rules(volume)
        rule = find_matching_rule(rules, origin, req_method)
        if not rule:
            return HttpResponse(status=403)
        from p2.s3.cors import cors_preflight_response
        return cors_preflight_response(rule, origin)

    async def get(self, request, *args, **kwargs):
        bucket = kwargs.get('bucket', '')
        if 'versioning' in request.GET:
            return self._handler_versioning()
        if 'uploads' in request.GET:
            return await self._list_multipart_uploads(request, bucket)
        if 'cors' in request.GET:
            return await self._get_cors(request, bucket)
        if 'acl' in request.GET:
            return await self._get_acl(request, bucket)
        if 'location' in request.GET:
            return await self._get_location(request, bucket)
        if 'tagging' in request.GET:
            return await self._get_bucket_tagging(request, bucket)
        if 'lifecycle' in request.GET:
            return await self._get_lifecycle(request, bucket)
        if 'notification' in request.GET:
            return self._get_notification()
        if 'encryption' in request.GET:
            return self._get_encryption()
        if 'policy' in request.GET:
            return await self._get_bucket_policy(request, bucket)
        return await self.handler_list(request, bucket)

    async def head(self, request, bucket):
        """HeadBucket — check if bucket exists and caller has access."""
        try:
            await self.get_volume(request.user, bucket, 'read')
            return HttpResponse(status=200)
        except Exception:
            return HttpResponse(status=404)

    async def put(self, request, bucket):
        if 'cors' in request.GET:
            return await self._put_cors(request, bucket)
        if 'acl' in request.GET:
            return await self._put_acl(request, bucket)
        if 'tagging' in request.GET:
            return await self._put_bucket_tagging(request, bucket)
        if 'lifecycle' in request.GET:
            return await self._put_lifecycle(request, bucket)
        if 'policy' in request.GET:
            return await self._put_bucket_policy(request, bucket)
        return await self._create_bucket(request, bucket)

    async def delete(self, request, bucket):
        if 'cors' in request.GET:
            return await self._delete_cors(request, bucket)
        if 'tagging' in request.GET:
            return await self._delete_bucket_tagging(request, bucket)
        if 'lifecycle' in request.GET:
            return await self._delete_lifecycle(request, bucket)
        if 'policy' in request.GET:
            return await self._delete_bucket_policy(request, bucket)
        volume = await self.get_volume(request.user, bucket, 'delete')
        await volume.adelete()
        return HttpResponse(status=204)

    async def post(self, request, bucket):
        """Multi-object delete: POST /<bucket>?delete"""
        if 'delete' in request.GET:
            return await self._multi_delete(request, bucket)
        return HttpResponse(status=400)

    # -------------------------------------------------------------------------
    # Bucket list with continuation tokens (ListObjectsV2)
    # -------------------------------------------------------------------------

    def _etree_for_blob(self, blob):
        content = ElementTree.Element("Contents")
        ElementTree.SubElement(content, "Key").text = blob.path[1:]
        mtime = blob.attributes.get(ATTR_BLOB_STAT_MTIME)
        ElementTree.SubElement(content, "LastModified").text = str(mtime) if mtime else ''
        ElementTree.SubElement(content, "ETag").text = blob.attributes.get(ATTR_BLOB_HASH_MD5, '')
        ElementTree.SubElement(content, "Size").text = str(blob.attributes.get(ATTR_BLOB_SIZE_BYTES, 0))
        ElementTree.SubElement(content, "StorageClass").text = \
            blob.volume.storage.controller.tags.get(TAG_S3_STORAGE_CLASS, 'STANDARD')
        return content

    async def handler_list(self, request, bucket):
        """ListObjectsV2 with continuation token support."""
        root = ElementTree.Element("{%s}ListBucketResult" % XML_NAMESPACE)
        volume = await self.get_volume(request.user, bucket, 'list')

        requested_prefix = request.GET.get('prefix', '')
        max_keys = min(int(request.GET.get('max-keys', 1000)), 1000)
        encoding_type = request.GET.get('encoding-type', 'url')
        delimiter = request.GET.get('delimiter', '/')
        continuation_token = request.GET.get('continuation-token', '')
        start_after = request.GET.get('start-after', '')

        # Decode continuation token to get the last key seen
        after_path = _decode_token(continuation_token) if continuation_token else start_after
        if after_path and not after_path.startswith('/'):
            after_path = '/' + after_path

        base_qs = Blob.objects.filter(
            prefix=make_absolute_prefix(requested_prefix),
            volume=volume,
        ).order_by('path').select_related('volume__storage')

        if after_path:
            base_qs = base_qs.filter(path__gt=after_path)

        blobs_qs = base_qs.exclude(attributes__has_key=ATTR_BLOB_IS_FOLDER)
        folders_qs = base_qs.filter(attributes__has_key=ATTR_BLOB_IS_FOLDER)

        blobs = []
        async for blob in blobs_qs[:max_keys + 1].aiterator():
            blobs.append(blob)

        is_truncated = len(blobs) > max_keys
        if is_truncated:
            blobs = blobs[:max_keys]

        ElementTree.SubElement(root, "Name").text = volume.name
        ElementTree.SubElement(root, "Prefix").text = requested_prefix
        ElementTree.SubElement(root, "KeyCount").text = str(len(blobs))
        ElementTree.SubElement(root, "MaxKeys").text = str(max_keys)
        ElementTree.SubElement(root, "Delimiter").text = delimiter
        ElementTree.SubElement(root, "EncodingType").text = encoding_type
        ElementTree.SubElement(root, "IsTruncated").text = str(is_truncated).lower()

        if is_truncated and blobs:
            next_token = _encode_token(blobs[-1].path)
            ElementTree.SubElement(root, "NextContinuationToken").text = next_token

        if continuation_token:
            ElementTree.SubElement(root, "ContinuationToken").text = continuation_token

        if blobs:
            for blob in blobs:
                root.append(self._etree_for_blob(blob))
        elif requested_prefix:
            try:
                directory_blob = await self.get_blob(volume, make_absolute_prefix(requested_prefix))
                root.append(self._etree_for_blob(directory_blob))
            except AWSNoSuchKey:
                pass

        common_prefixes = ElementTree.Element("CommonPrefixes")
        async for blob in folders_qs.aiterator():
            ElementTree.SubElement(common_prefixes, 'Prefix').text = blob.filename
        if len(common_prefixes):
            root.append(common_prefixes)

        response = XMLResponse(root)
        # Apply CORS if applicable
        origin = request.META.get("HTTP_ORIGIN", "")
        if origin:
            rules = get_cors_rules(volume)
            rule = find_matching_rule(rules, origin, "GET")
            if rule:
                apply_cors_headers(response, rule, origin)
        return response

    def _handler_versioning(self):
        root = ElementTree.Element("{%s}VersioningConfiguration" % XML_NAMESPACE)
        ElementTree.SubElement(root, "Status").text = "Disabled"
        return XMLResponse(root)

    # -------------------------------------------------------------------------
    # Lifecycle (exposes existing Expiry component via S3 API)
    # -------------------------------------------------------------------------

    TAG_LIFECYCLE = 's3.p2.io/lifecycle/rules'

    async def _get_lifecycle(self, request, bucket):
        volume = await self.get_volume(request.user, bucket, 'read')
        rules_json = volume.tags.get(self.TAG_LIFECYCLE)
        if not rules_json:
            response = HttpResponse(status=404)
            response['Content-Type'] = 'application/xml'
            root = ElementTree.Element("Error")
            ElementTree.SubElement(root, "Code").text = "NoSuchLifecycleConfiguration"
            ElementTree.SubElement(root, "Message").text = "The lifecycle configuration does not exist"
            response.content = ElementTree.tostring(root, encoding='utf-8', method='xml')
            return response
        rules = json.loads(rules_json)
        root = ElementTree.Element("{%s}LifecycleConfiguration" % XML_NAMESPACE)
        for rule in rules:
            rule_el = ElementTree.SubElement(root, "Rule")
            ElementTree.SubElement(rule_el, "ID").text = rule.get('ID', '')
            ElementTree.SubElement(rule_el, "Status").text = rule.get('Status', 'Enabled')
            if 'Prefix' in rule:
                filt = ElementTree.SubElement(rule_el, "Filter")
                ElementTree.SubElement(filt, "Prefix").text = rule['Prefix']
            if 'Expiration' in rule:
                exp = ElementTree.SubElement(rule_el, "Expiration")
                if 'Days' in rule['Expiration']:
                    ElementTree.SubElement(exp, "Days").text = str(rule['Expiration']['Days'])
        return XMLResponse(root)

    async def _put_lifecycle(self, request, bucket):
        volume = await self.get_volume(request.user, bucket, 'write')
        root = ElementTree.fromstring(request.body)
        rules = []
        for rule_el in root.iter('Rule'):
            rule = {}
            id_el = rule_el.find('ID')
            if id_el is not None and id_el.text:
                rule['ID'] = id_el.text
            status_el = rule_el.find('Status')
            rule['Status'] = status_el.text if status_el is not None else 'Enabled'
            filt = rule_el.find('Filter')
            if filt is not None:
                prefix_el = filt.find('Prefix')
                if prefix_el is not None:
                    rule['Prefix'] = prefix_el.text or ''
            exp_el = rule_el.find('Expiration')
            if exp_el is not None:
                exp = {}
                days_el = exp_el.find('Days')
                if days_el is not None:
                    exp['Days'] = int(days_el.text)
                rule['Expiration'] = exp
            rules.append(rule)
        volume.tags[self.TAG_LIFECYCLE] = json.dumps(rules)
        await volume.asave(update_fields=['tags'])
        return HttpResponse(status=200)

    async def _delete_lifecycle(self, request, bucket):
        volume = await self.get_volume(request.user, bucket, 'write')
        volume.tags.pop(self.TAG_LIFECYCLE, None)
        await volume.asave(update_fields=['tags'])
        return HttpResponse(status=204)

    def _get_notification(self):
        """GetBucketNotification — stub returning empty config.
        SDKs probe this on startup; an empty response prevents errors."""
        root = ElementTree.Element("{%s}NotificationConfiguration" % XML_NAMESPACE)
        return XMLResponse(root)

    def _get_encryption(self):
        """GetBucketEncryption — stub returning no encryption configured.
        Satisfies SDK startup probes that call this before any object ops."""
        from django.http import HttpResponse
        # S3 returns 404 ServerSideEncryptionConfigurationNotFoundError when
        # no encryption is configured — SDKs treat this as "no SSE, proceed normally"
        response = HttpResponse(status=404)
        response['Content-Type'] = 'application/xml'
        root = ElementTree.Element("Error")
        ElementTree.SubElement(root, "Code").text = "ServerSideEncryptionConfigurationNotFoundError"
        ElementTree.SubElement(root, "Message").text = "The server side encryption configuration was not found"
        response.content = ElementTree.tostring(root, encoding='utf-8', method='xml')
        return response

    async def _list_multipart_uploads(self, request, bucket):
        """ListMultipartUploads — returns active (incomplete) multipart uploads."""
        from p2.s3.constants import (TAG_S3_MULTIPART_BLOB_TARGET_BLOB,
                                     TAG_S3_MULTIPART_BLOB_UPLOAD_ID)
        volume = await self.get_volume(request.user, bucket, 'read')
        prefix = request.GET.get('prefix', '')
        max_uploads = min(int(request.GET.get('max-uploads', 1000)), 1000)

        root = ElementTree.Element("{%s}ListMultipartUploadsResult" % XML_NAMESPACE)
        ElementTree.SubElement(root, "Bucket").text = bucket
        ElementTree.SubElement(root, "Prefix").text = prefix
        ElementTree.SubElement(root, "MaxUploads").text = str(max_uploads)
        ElementTree.SubElement(root, "IsTruncated").text = "false"

        seen_upload_ids = set()
        async for blob in Blob.objects.filter(
            volume=volume,
            **{f'tags__{TAG_S3_MULTIPART_BLOB_UPLOAD_ID}__isnull': False},
        ).order_by('path').aiterator():
            upload_id = blob.tags.get(TAG_S3_MULTIPART_BLOB_UPLOAD_ID)
            target = blob.tags.get(TAG_S3_MULTIPART_BLOB_TARGET_BLOB, '')
            if not upload_id or upload_id in seen_upload_ids:
                continue
            if prefix and not target.lstrip('/').startswith(prefix):
                continue
            seen_upload_ids.add(upload_id)
            upload_el = ElementTree.SubElement(root, "Upload")
            ElementTree.SubElement(upload_el, "Key").text = target.lstrip('/')
            ElementTree.SubElement(upload_el, "UploadId").text = upload_id
            initiator = ElementTree.SubElement(upload_el, "Initiator")
            ElementTree.SubElement(initiator, "ID").text = str(request.user.pk)
            ElementTree.SubElement(initiator, "DisplayName").text = request.user.username
            ElementTree.SubElement(upload_el, "StorageClass").text = "STANDARD"

        return XMLResponse(root)

    # -------------------------------------------------------------------------
    # Bucket create
    # -------------------------------------------------------------------------

    async def _create_bucket(self, request, bucket):
        storage = await Storage.objects.filter(**{
            'tags__%s' % TAG_S3_DEFAULT_STORAGE: True
        }).afirst()
        if storage is None:
            LOGGER.warning("No Storage marked as default. Add tag '%s: true'.", TAG_S3_DEFAULT_STORAGE)
            raise AWSAccessDenied
        if not await request.user.ahas_perm('p2_core.add_volume'):
            raise AWSAccessDenied
        volume, _ = await Volume.objects.aget_or_create(
            name=bucket, defaults={'storage': storage}
        )
        # Apply canned ACL from header
        canned = request.META.get('HTTP_X_AMZ_ACL', 'private')
        if canned in _CANNED_ACL_PERMS and 'public-read' in canned:
            volume.public_read = True
            await volume.asave(update_fields=['public_read'])
        volume.tags[TAG_S3_ACL] = canned
        await volume.asave(update_fields=['tags'])

        await VolumeACL.objects.aget_or_create(
            volume=volume, user=request.user,
            defaults={'permissions': ['read', 'write', 'delete', 'list', 'admin']},
        )
        return HttpResponse(status=200)

    # -------------------------------------------------------------------------
    # Multi-object delete
    # -------------------------------------------------------------------------

    async def _multi_delete(self, request, bucket):
        """POST /<bucket>?delete — delete multiple objects in one request."""
        volume = await self.get_volume(request.user, bucket, 'delete')
        root_in = ElementTree.fromstring(request.body)
        ns = XML_NAMESPACE

        keys = []
        for obj_el in root_in.iter("Object"):
            key_el = obj_el.find("Key") or obj_el.find(f"{{{ns}}}Key")
            if key_el is not None and key_el.text:
                keys.append('/' + key_el.text.lstrip('/'))

        root_out = ElementTree.Element("{%s}DeleteResult" % ns)
        for key in keys:
            blob = await Blob.objects.filter(path=key, volume=volume).afirst()
            if blob is not None:
                await blob.adelete()
            deleted = ElementTree.SubElement(root_out, "Deleted")
            ElementTree.SubElement(deleted, "Key").text = key.lstrip('/')

        return XMLResponse(root_out)

    # -------------------------------------------------------------------------
    # CORS
    # -------------------------------------------------------------------------

    async def _get_cors(self, request, bucket):
        volume = await self.get_volume(request.user, bucket, 'read')
        rules = get_cors_rules(volume)
        if not rules:
            return HttpResponse(status=404)
        return XMLResponse(build_cors_xml(rules))

    async def _put_cors(self, request, bucket):
        volume = await self.get_volume(request.user, bucket, 'write')
        rules = parse_cors_xml(request.body)
        volume.tags['s3.p2.io/cors/rules'] = rules
        await volume.asave(update_fields=['tags'])
        return HttpResponse(status=200)

    async def _delete_cors(self, request, bucket):
        volume = await self.get_volume(request.user, bucket, 'write')
        volume.tags.pop('s3.p2.io/cors/rules', None)
        await volume.asave(update_fields=['tags'])
        return HttpResponse(status=204)

    # -------------------------------------------------------------------------
    # Bucket ACL
    # -------------------------------------------------------------------------

    async def _get_acl(self, request, bucket):
        volume = await self.get_volume(request.user, bucket, 'read')
        owner_id = str(request.user.pk)
        owner_name = request.user.username
        canned = volume.tags.get(TAG_S3_ACL, 'private')

        root = ElementTree.Element("{%s}AccessControlPolicy" % XML_NAMESPACE)
        owner = ElementTree.SubElement(root, "Owner")
        ElementTree.SubElement(owner, "ID").text = owner_id
        ElementTree.SubElement(owner, "DisplayName").text = owner_name
        acl_list = ElementTree.SubElement(root, "AccessControlList")

        grant = ElementTree.SubElement(acl_list, "Grant")
        grantee = ElementTree.SubElement(grant, "Grantee")
        grantee.set("{http://www.w3.org/2001/XMLSchema-instance}type", "CanonicalUser")
        ElementTree.SubElement(grantee, "ID").text = owner_id
        ElementTree.SubElement(grant, "Permission").text = "FULL_CONTROL"

        if 'public-read' in canned or 'public-read-write' in canned:
            grant2 = ElementTree.SubElement(acl_list, "Grant")
            grantee2 = ElementTree.SubElement(grant2, "Grantee")
            grantee2.set("{http://www.w3.org/2001/XMLSchema-instance}type", "Group")
            ElementTree.SubElement(grantee2, "URI").text = \
                "http://acs.amazonaws.com/groups/global/AllUsers"
            ElementTree.SubElement(grant2, "Permission").text = "READ"

        return XMLResponse(root)

    async def _put_acl(self, request, bucket):
        volume = await self.get_volume(request.user, bucket, 'write')
        canned = request.META.get('HTTP_X_AMZ_ACL', 'private')
        if canned not in _CANNED_ACL_PERMS:
            canned = 'private'
        volume.tags[TAG_S3_ACL] = canned
        volume.public_read = 'public-read' in canned
        await volume.asave(update_fields=['tags', 'public_read'])
        return HttpResponse(status=200)

    # -------------------------------------------------------------------------
    # Bucket location
    # -------------------------------------------------------------------------

    async def _get_location(self, request, bucket):
        """GetBucketLocation — SDKs call this on startup to discover the region."""
        await self.get_volume(request.user, bucket, 'read')
        root = ElementTree.Element("{%s}LocationConstraint" % XML_NAMESPACE)
        root.text = 'us-east-1'
        return XMLResponse(root)

    # -------------------------------------------------------------------------
    # Bucket tagging
    # -------------------------------------------------------------------------

    async def _get_bucket_tagging(self, request, bucket):
        volume = await self.get_volume(request.user, bucket, 'read')
        from p2.s3.constants import TAG_S3_USER_TAG_PREFIX
        tags = {k[len(TAG_S3_USER_TAG_PREFIX):]: v
                for k, v in volume.tags.items()
                if k.startswith(TAG_S3_USER_TAG_PREFIX)}
        root = ElementTree.Element("{%s}Tagging" % XML_NAMESPACE)
        tag_set = ElementTree.SubElement(root, "TagSet")
        for k, v in tags.items():
            tag_el = ElementTree.SubElement(tag_set, "Tag")
            ElementTree.SubElement(tag_el, "Key").text = k
            ElementTree.SubElement(tag_el, "Value").text = str(v)
        return XMLResponse(root)

    async def _put_bucket_tagging(self, request, bucket):
        volume = await self.get_volume(request.user, bucket, 'write')
        from p2.s3.constants import TAG_S3_USER_TAG_PREFIX
        from p2.s3.views.objects import _parse_tagging_xml
        new_tags = _parse_tagging_xml(request.body)
        volume.tags = {k: v for k, v in volume.tags.items()
                       if not k.startswith(TAG_S3_USER_TAG_PREFIX)}
        for k, v in new_tags.items():
            volume.tags[f"{TAG_S3_USER_TAG_PREFIX}{k}"] = v
        await volume.asave(update_fields=['tags'])
        return HttpResponse(status=200)

    async def _delete_bucket_tagging(self, request, bucket):
        volume = await self.get_volume(request.user, bucket, 'write')
        from p2.s3.constants import TAG_S3_USER_TAG_PREFIX
        volume.tags = {k: v for k, v in volume.tags.items()
                       if not k.startswith(TAG_S3_USER_TAG_PREFIX)}
        await volume.asave(update_fields=['tags'])
        return HttpResponse(status=204)

    # -------------------------------------------------------------------------
    # Bucket policy
    # -------------------------------------------------------------------------

    TAG_BUCKET_POLICY = 's3.p2.io/bucket-policy'

    async def _get_bucket_policy(self, request, bucket):
        volume = await self.get_volume(request.user, bucket, 'admin')
        policy_json = volume.tags.get(self.TAG_BUCKET_POLICY)
        if not policy_json:
            response = HttpResponse(status=404)
            response['Content-Type'] = 'application/xml'
            root = ElementTree.Element("Error")
            ElementTree.SubElement(root, "Code").text = "NoSuchBucketPolicy"
            ElementTree.SubElement(root, "Message").text = "The bucket policy does not exist"
            response.content = ElementTree.tostring(root, encoding='utf-8', method='xml')
            return response
        return HttpResponse(policy_json, content_type='application/json')

    async def _put_bucket_policy(self, request, bucket):
        volume = await self.get_volume(request.user, bucket, 'admin')
        from p2.s3.policy import parse_policy
        try:
            parse_policy(request.body.decode('utf-8'))  # validate
        except (ValueError, json.JSONDecodeError) as exc:
            response = HttpResponse(status=400)
            response['Content-Type'] = 'application/xml'
            root = ElementTree.Element("Error")
            ElementTree.SubElement(root, "Code").text = "MalformedPolicy"
            ElementTree.SubElement(root, "Message").text = str(exc)
            response.content = ElementTree.tostring(root, encoding='utf-8', method='xml')
            return response
        volume.tags[self.TAG_BUCKET_POLICY] = request.body.decode('utf-8')
        await volume.asave(update_fields=['tags'])
        return HttpResponse(status=204)

    async def _delete_bucket_policy(self, request, bucket):
        volume = await self.get_volume(request.user, bucket, 'admin')
        volume.tags.pop(self.TAG_BUCKET_POLICY, None)
        await volume.asave(update_fields=['tags'])
        return HttpResponse(status=204)
