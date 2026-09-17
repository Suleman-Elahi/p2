"""p2 core http responses

BlobResponse has been removed. Object reads are served directly by Granian:
the S3 data plane streams block ranges out of the append-only volumes (see
p2/s3/volume_reader.py, driven from p2/s3/asgi_handler.py), and legacy
per-blob files are served with Django's FileResponse.

This module is kept as a stub so existing imports don't break at startup.
"""
