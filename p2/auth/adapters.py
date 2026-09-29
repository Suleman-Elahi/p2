"""django-allauth adapters that bridge social login into p2's JWT auth.

p2's SPA (ui/) is JWT-based (ninja-jwt access/refresh tokens in localStorage),
not session-based. django-allauth authenticates the user into a Django
*session* by default. P2AccountAdapter overrides the point where allauth
decides "where does the browser go after login" (get_login_redirect_url) and
sends it back to the SPA's login page with a freshly minted JWT pair attached
as query params. LoginPage.vue reads those params on load, stores them via
the same saveTokens() path used for password login, and routes to the
dashboard — so social login and password login both end up in the same
authenticated SPA state.

Adding a new provider requires no changes to this file — only:
  1. Add 'allauth.socialaccount.providers.<provider>' to INSTALLED_APPS.
  2. Create a SocialApp row for it via /_/admin/socialaccount/socialapp/.
"""
import logging
from urllib.parse import urlencode

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.http import HttpRequest
from ninja_jwt.tokens import RefreshToken

LOGGER = logging.getLogger(__name__)

# Where the SPA's login page lives — it reads `access`/`refresh` query params
# on load, stores them via saveTokens(), and then routes to the dashboard.
SPA_LOGIN_PATH = '/login'


def _build_jwt_redirect(user) -> str:
    refresh = RefreshToken.for_user(user)
    params = urlencode({
        'access': str(refresh.access_token),
        'refresh': str(refresh),
    })
    LOGGER.debug("SSO login: minted JWT pair for user=%s", user.get_username())
    return f'{SPA_LOGIN_PATH}?{params}'


class P2AccountAdapter(DefaultAccountAdapter):
    """Redirect post-login/signup to the SPA with a minted JWT pair instead
    of leaving the user on a bare Django session.
    """

    def get_login_redirect_url(self, request: HttpRequest) -> str:
        return _build_jwt_redirect(request.user)

    def get_signup_redirect_url(self, request: HttpRequest) -> str:
        return _build_jwt_redirect(request.user)


class P2SocialAccountAdapter(DefaultSocialAccountAdapter):
    """Auto-provision a local User for first-time social logins (p2 has no
    extra required profile fields, so allauth's intermediate signup form
    would just be friction) and keep the same JWT redirect for the
    "connect an additional provider to an existing account" flow.
    """

    def is_auto_signup_allowed(self, request: HttpRequest, sociallogin) -> bool:
        return True

    def get_connect_redirect_url(self, request: HttpRequest, socialaccount) -> str:
        return _build_jwt_redirect(socialaccount.user)
