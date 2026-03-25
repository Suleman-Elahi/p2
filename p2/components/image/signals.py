"""Image signals — DEPRECATED (Django signals removed).

EXIF extraction is now triggered by the BLOB_PAYLOAD_UPDATED Redis Stream
event, handled in p2.core.consumers.handle_image_exif.
"""
