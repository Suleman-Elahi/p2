"""
WSGI config for p2 project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/1.10/howto/deployment/wsgi/
"""
import os
from time import time

from django.core.wsgi import get_wsgi_application
import logging

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "p2.root.settings")

LOGGER = logging.getLogger(__name__)


class WSGILogger:
    """ This is the generalized WSGI middleware for any style request logging. """

    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        start = time()
        status_codes = []
        content_lengths = []

        def custom_start_response(status, response_headers, exc_info=None):
            status_codes.append(int(status.partition(' ')[0]))
            for name, value in response_headers:
                if name.lower() == 'content-length':
                    content_lengths.append(int(value))
                    break
            return start_response(status, response_headers, exc_info)
        retval = self.application(environ, custom_start_response)
        runtime = int((time() - start) * 10**6)
        content_length = content_lengths[0] if content_lengths else 0
        self.log(status_codes[0], environ, content_length,
                 ip_header=None, runtime=runtime)
        return retval

    def log(self, status_code, environ, content_length, **kwargs):
        """
        Apache log format 'NCSA extended/combined log':
        "%h %l %u %t \"%r\" %>s %b \"%{Referer}i\" \"%{User-agent}i\""
        see http://httpd.apache.org/docs/current/mod/mod_log_config.html#formats
        """
        ip_header = kwargs.get('ip_header', None)
        if ip_header:
            host = environ.get(ip_header, '')
        else:
            host = environ.get('REMOTE_ADDR', '')
        if environ.get('HTTP_HOST').startswith('kubernetes-healthcheck-host'):
            # Don't log kubernetes health/readiness requests
            return
        query_string = ''
        if environ.get('QUERY_STRING') != '':
            query_string = f"?{environ.get('QUERY_STRING')}"
        LOGGER.info(f"{environ.get('PATH_INFO', '')}{query_string}",
                    host=host,
                    method=environ.get('REQUEST_METHOD', ''),
                    protocol=environ.get('SERVER_PROTOCOL', ''),
                    status=status_code,
                    size=content_length / 1000 if content_length > 0 else '-',
                    runtime=kwargs.get('runtime'))


application = WSGILogger(get_wsgi_application())
