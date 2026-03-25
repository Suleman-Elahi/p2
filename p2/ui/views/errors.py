"""p2 error views"""
import sys
import traceback

from django.http.response import (HttpResponseBadRequest,
                                  HttpResponseForbidden, HttpResponseNotFound,
                                  HttpResponseServerError)
from django.template.response import TemplateResponse
from django.views.generic import TemplateView


class BadRequestTemplateResponse(TemplateResponse, HttpResponseBadRequest):
    """Combine Template response with Http Code 400"""


class ForbiddenTemplateResponse(TemplateResponse, HttpResponseForbidden):
    """Combine Template response with Http Code 403"""


class NotFoundTemplateResponse(TemplateResponse, HttpResponseNotFound):
    """Combine Template response with Http Code 404"""


class ServerErrorTemplateResponse(TemplateResponse, HttpResponseServerError):
    """Combine Template response with Http Code 500"""


class ServerErrorView(TemplateView):
    """Show server error message and sentry feedback integration"""

    response_class = ServerErrorTemplateResponse
    template_name = 'errors/500.html'

    def dispatch(self, *args, **kwargs):
        """Little wrapper so django accepts this function"""
        type_, _error, trace = sys.exc_info()
        self.extra_context = {
            'exception': str(type_),
            'error': traceback.format_tb(trace),
            'hide_navbar': True,
        }
        return super().dispatch(*args, **kwargs)
