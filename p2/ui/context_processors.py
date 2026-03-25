"""p2 UI Context Processors"""

from django.conf import settings
from p2 import __version__


def version(request):
    """return version number"""
    return {
        'p2_version': __version__,
        'oidc_enabled': getattr(settings, 'OIDC_ENABLED', False),
    }
