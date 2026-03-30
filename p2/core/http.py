"""p2 core http responses

BlobResponse has been removed — downloads now go through Nginx X-Accel-Redirect
(see p2/s3/views/objects.py → GetObject) which is zero-copy and does not need
this Django streaming response wrapper.

This module is kept as a stub so existing imports don't break at startup.
"""
