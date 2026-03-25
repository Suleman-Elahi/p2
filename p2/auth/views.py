"""p2 OIDC authentication views using authlib Django integration."""
import logging

from authlib.integrations.django_client import OAuth
from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

LOGGER = logging.getLogger(__name__)

oauth = OAuth()
oauth.register(
    name='oidc',
    server_metadata_url=settings.AUTHLIB_OAUTH_CLIENTS['oidc']['server_metadata_url'],
    client_id=settings.AUTHLIB_OAUTH_CLIENTS['oidc']['client_id'],
    client_secret=settings.AUTHLIB_OAUTH_CLIENTS['oidc']['client_secret'],
    client_kwargs=settings.AUTHLIB_OAUTH_CLIENTS['oidc']['client_kwargs'],
)

User = get_user_model()


def oidc_login(request: HttpRequest) -> HttpResponse:
    """Redirect to OIDC provider with PKCE code challenge."""
    redirect_uri = request.build_absolute_uri(reverse('p2_auth:oidc_callback'))
    return oauth.oidc.authorize_redirect(request, redirect_uri)


def oidc_callback(request: HttpRequest) -> HttpResponse:
    """Handle OIDC callback: exchange code, create/update user, log in."""
    try:
        token = oauth.oidc.authorize_access_token(request)
    except Exception as exc:  # pylint: disable=broad-except
        LOGGER.warning("OIDC callback failed: %s", exc)
        return redirect(settings.LOGIN_URL)

    userinfo = token.get('userinfo') or oauth.oidc.userinfo(token=token)
    if not userinfo:
        LOGGER.warning("OIDC callback: no userinfo returned")
        return redirect(settings.LOGIN_URL)

    email = userinfo.get('email', '')
    sub = userinfo.get('sub', '')

    if not email and not sub:
        LOGGER.warning("OIDC callback: userinfo missing email and sub")
        return redirect(settings.LOGIN_URL)

    username = email or sub
    user, created = User.objects.get_or_create(
        username=username,
        defaults={
            'email': email,
            'first_name': userinfo.get('given_name', ''),
            'last_name': userinfo.get('family_name', ''),
        },
    )

    if not created:
        # Keep email/name in sync with the provider
        updated = False
        if email and user.email != email:
            user.email = email
            updated = True
        first_name = userinfo.get('given_name', '')
        last_name = userinfo.get('family_name', '')
        if first_name and user.first_name != first_name:
            user.first_name = first_name
            updated = True
        if last_name and user.last_name != last_name:
            user.last_name = last_name
            updated = True
        if updated:
            user.save(update_fields=['email', 'first_name', 'last_name'])

    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    LOGGER.debug("OIDC login: user=%s created=%s", username, created)

    next_url = request.session.pop('oidc_next', None) or settings.LOGIN_REDIRECT_URL
    return redirect(next_url)
