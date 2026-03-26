"""AWS API Constants"""

XML_NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"

TAG_S3_STORAGE_CLASS = 's3.p2.io/storage/class'
TAG_S3_DEFAULT_STORAGE = 's3.p2.io/storage/default'

TAG_S3_MULTIPART_BLOB_PART = 's3.p2.io/multipart/part-number'
TAG_S3_MULTIPART_BLOB_TARGET_BLOB = 's3.p2.io/multipart/target'
TAG_S3_MULTIPART_BLOB_UPLOAD_ID = 's3.p2.io/multipart/upload-id'

# Object tagging — stored in blob.tags
TAG_S3_USER_TAG_PREFIX = 's3.user/'

# CORS rules — stored in volume.tags as JSON list
TAG_S3_CORS_RULES = 's3.p2.io/cors/rules'

# ACL canned values stored in volume/blob tags
TAG_S3_ACL = 's3.p2.io/acl'

# Presigned URL HMAC secret (reuses Fernet key via settings.SECRET_KEY)
PRESIGNED_MAX_EXPIRY = 604800  # 7 days, AWS maximum
