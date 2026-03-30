"""p2 signals"""
from django.core.signals import Signal

# BLOB_PRE_SAVE remains as a no-op signal — external code may still import it.
# The actual pre_save / pre_delete hooks on the Blob model have been removed
# since the Blob model has been replaced by the p2_s3_meta LSM engine.
BLOB_PRE_SAVE = Signal()
BLOB_ACCESS = Signal()
