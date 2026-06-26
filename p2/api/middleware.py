"""Middleware to skip CSRF for JWT-authenticated API paths.

JWT tokens are sent via Authorization header, which is immune to CSRF
attacks.  Django's CsrfViewMiddleware has no such exemption — we add one
here so the Frappe UI SPA can POST to /api/v1/auth/token/ without a CSRF cookie.
"""


class ApiCSRFExemptMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith('/api/'):
            setattr(request, 'csrf_processing_done', True)
        return self.get_response(request)
