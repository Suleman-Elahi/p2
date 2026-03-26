"""AWS API Errors"""

class AWSError(Exception):
    """Base-class for all AWS Errors"""

    code = 'InvalidRequest'
    status = 400


class AWSSignatureMismatch(AWSError):
    """Error codes from https://docs.aws.amazon.com/AmazonS3/latest/API/ErrorResponses.html"""

    code = 'SignatureDoesNotMatch'
    status = 403


class AWSAccessDenied(AWSError):
    """Error codes from https://docs.aws.amazon.com/AmazonS3/latest/API/ErrorResponses.html"""

    code = 'AccessDenied'
    status = 401


class AWSNotImplemented(AWSError):
    """Error codes from https://docs.aws.amazon.com/AmazonS3/latest/API/ErrorResponses.html"""

    code = 'NotImplemented'
    status = 501


class AWSNoSuchKey(AWSError):
    """Error codes from https://docs.aws.amazon.com/AmazonS3/latest/API/ErrorResponses.html"""

    code = 'NoSuchKey'
    status = 404


class AWSNoSuchBucket(AWSError):
    """Error codes from https://docs.aws.amazon.com/AmazonS3/latest/API/ErrorResponses.html"""

    code = 'NoSuchBucket'
    status = 404


class AWSInvalidHMAC(AWSError):
    """Error codes from https://docs.aws.amazon.com/AmazonS3/latest/API/ErrorResponses.html"""

    code = 'InvalidRequest'
    status = 400


class AWSContentSignatureMismatch(AWSError):
    """Error codes from https://docs.aws.amazon.com/AmazonS3/latest/API/ErrorResponses.html"""

    code = 'XAmzContentSHA256Mismatch'
    status = 403


class AWSInvalidDigest(AWSError):
    """Error codes from https://docs.aws.amazon.com/AmazonS3/latest/API/ErrorResponses.html"""

    code = 'InvalidDigest'
    status = 400


class AWSBadDigest(AWSError):
    """Error codes from https://docs.aws.amazon.com/AmazonS3/latest/API/ErrorResponses.html"""

    code = 'BadDigest'
    status = 400


class AWSIncompleteBody(AWSError):
    """Error codes from https://docs.aws.amazon.com/AmazonS3/latest/API/ErrorResponses.html"""

    code = 'IncompleteBody'
    status = 400


class AWSMissingContentLength(AWSError):
    """Error codes from https://docs.aws.amazon.com/AmazonS3/latest/API/ErrorResponses.html"""

    code = 'MissingContentLength'
    status = 411


class AWSNoSuchUpload(AWSError):
    code = 'NoSuchUpload'
    status = 404


class AWSInvalidArgument(AWSError):
    code = 'InvalidArgument'
    status = 400


class AWSExpiredToken(AWSError):
    code = 'ExpiredToken'
    status = 403


class AWSPresignedInvalid(AWSError):
    code = 'AuthorizationQueryParametersError'
    status = 403
