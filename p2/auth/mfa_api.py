"""TOTP two-factor authentication API (ninja).

This sits in front of ninja-jwt's token issuance rather than replacing it:

  1. POST /api/v1/auth/login          — username + password.
       - If the user has no TOTP authenticator: mints and returns a normal
         ninja-jwt access/refresh pair immediately, same as calling
         /api/v1/auth/token/pair directly.
       - If the user *does* have TOTP enabled: password is verified, but no
         JWT is issued yet. Instead a short-lived, single-use "MFA challenge"
         id is returned. The client must immediately follow up with...
  2. POST /api/v1/auth/mfa/verify      — mfa_token + code (TOTP or a
         recovery code). On success, the challenge is consumed (cannot be
         reused) and the real ninja-jwt pair is minted and returned.

Enrollment (done once the user already holds a valid JWT, i.e. from
Settings in the SPA):
  3. POST /api/v1/auth/mfa/setup       — generates a new TOTP secret, returns
         the otpauth:// URI + an SVG QR code. Secret is cached server-side
         (not the Authenticator table) until confirmed via step 4.
  4. POST /api/v1/auth/mfa/confirm     — code -> activates the TOTP
         Authenticator + generates recovery codes (returned once, per
         allauth.mfa.RecoveryCodes semantics).
  5. POST /api/v1/auth/mfa/disable     — removes TOTP + recovery codes for
         the current user (requires a valid password to avoid a stolen
         access token silently disabling 2FA).
  6. GET  /api/v1/auth/mfa/status      — whether the current user has TOTP
         enabled.

All of this reuses allauth.mfa's own model (Authenticator) and its TOTP /
RecoveryCodes helper classes for secret generation, QR building, and code
validation — we do not reimplement TOTP math. We only replace allauth's own
views, which are session/HTML-form based and don't fit a JWT SPA.
"""
import logging
import secrets

from allauth.mfa.adapter import get_adapter as get_mfa_adapter
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret, validate_totp_code
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.core.cache import cache
from ninja import Router, Schema
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

LOGGER = logging.getLogger(__name__)
User = get_user_model()

router_login = Router(tags=["auth-login"])
router_mfa = Router(tags=["auth-mfa"])

_CHALLENGE_CACHE_PREFIX = 'p2:mfa:challenge:'
_PENDING_SECRET_CACHE_PREFIX = 'p2:mfa:pending_secret:'


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LoginSchema(Schema):
    username: str
    password: str


class LoginResponseSchema(Schema):
    mfa_required: bool = False
    mfa_token: str | None = None
    access: str | None = None
    refresh: str | None = None


class MfaVerifySchema(Schema):
    mfa_token: str
    code: str


class MfaConfirmSchema(Schema):
    code: str


class MfaDisableSchema(Schema):
    password: str


class MfaStatusSchema(Schema):
    enabled: bool


class MfaSetupResponseSchema(Schema):
    secret: str
    otpauth_url: str
    qr_svg: str


class MfaConfirmResponseSchema(Schema):
    recovery_codes: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mint_jwt_pair(user) -> dict:
    refresh = RefreshToken.for_user(user)
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}


def _issue_mfa_challenge(user) -> str:
    """Create a single-use, short-lived token binding this login attempt to
    `user`, so /mfa/verify doesn't need the password again."""
    token = secrets.token_urlsafe(32)
    cache.set(f'{_CHALLENGE_CACHE_PREFIX}{token}', user.pk, timeout=settings.MFA_CHALLENGE_TTL_SECONDS)
    return token


def _consume_mfa_challenge(token: str):
    """Return the User bound to a challenge token, or None. Always deletes
    the token so it cannot be replayed, whether or not it was valid."""
    cache_key = f'{_CHALLENGE_CACHE_PREFIX}{token}'
    user_pk = cache.get(cache_key)
    cache.delete(cache_key)
    if user_pk is None:
        return None
    return User.objects.filter(pk=user_pk, is_active=True).first()


def _totp_authenticator(user) -> Authenticator | None:
    return Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).first()


def _validate_second_factor(user, code: str) -> bool:
    """Accept either a live TOTP code or one of the user's recovery codes."""
    totp_auth = _totp_authenticator(user)
    if totp_auth and TOTP(totp_auth).validate_code(code):
        return True
    recovery_auth = Authenticator.objects.filter(
        user=user, type=Authenticator.Type.RECOVERY_CODES,
    ).first()
    if recovery_auth and RecoveryCodes(recovery_auth).validate_code(code):
        return True
    return False


# ---------------------------------------------------------------------------
# Login (first factor + MFA gate)
# ---------------------------------------------------------------------------

@router_login.post('/login', response=LoginResponseSchema, auth=None)
def login(request, payload: LoginSchema):
    """Password auth. Returns JWT immediately if the user has no TOTP
    enrolled, otherwise returns an MFA challenge that must be completed via
    /mfa/verify.
    """
    user = authenticate(request, username=payload.username, password=payload.password)
    if user is None or not user.is_active:
        raise HttpError(401, "No active account found with the given credentials")

    if _totp_authenticator(user) is not None:
        return LoginResponseSchema(mfa_required=True, mfa_token=_issue_mfa_challenge(user))

    return LoginResponseSchema(**_mint_jwt_pair(user))


@router_mfa.post('/verify', response=LoginResponseSchema, auth=None)
def verify(request, payload: MfaVerifySchema):
    """Second factor. Consumes the challenge token (single use) and, if the
    TOTP/recovery code is valid, mints and returns the real JWT pair.
    """
    user = _consume_mfa_challenge(payload.mfa_token)
    if user is None:
        raise HttpError(401, "MFA challenge expired or already used. Please log in again.")

    if not _validate_second_factor(user, payload.code):
        raise HttpError(401, "Incorrect code.")

    LOGGER.debug("MFA verify: user=%s", user.get_username())
    return LoginResponseSchema(**_mint_jwt_pair(user))


# ---------------------------------------------------------------------------
# Enrollment (requires an existing valid JWT — normal `auth` on this router)
# ---------------------------------------------------------------------------

@router_mfa.get('/status', response=MfaStatusSchema)
def status(request):
    return MfaStatusSchema(enabled=_totp_authenticator(request.user) is not None)


@router_mfa.post('/setup', response=MfaSetupResponseSchema)
def setup(request):
    """Generate a new TOTP secret and return the QR code. Not yet persisted
    to an Authenticator row — call /mfa/confirm with a valid code from the
    generated secret to actually activate it. Mirrors allauth.mfa's own
    session-held-pending-secret flow.
    """
    if _totp_authenticator(request.user) is not None:
        raise HttpError(400, "Two-factor authentication is already enabled.")

    secret = generate_totp_secret()
    cache.set(
        f'{_PENDING_SECRET_CACHE_PREFIX}{request.user.pk}',
        secret,
        timeout=settings.MFA_CHALLENGE_TTL_SECONDS,
    )

    adapter = get_mfa_adapter()
    otpauth_url = adapter.build_totp_url(request.user, secret)
    qr_svg = adapter.build_totp_svg(otpauth_url)
    return MfaSetupResponseSchema(secret=secret, otpauth_url=otpauth_url, qr_svg=qr_svg)


@router_mfa.post('/confirm', response=MfaConfirmResponseSchema)
def confirm(request, payload: MfaConfirmSchema):
    """Verify a code against the pending secret from /mfa/setup, and if
    correct, activate TOTP + generate recovery codes (shown once).
    """
    cache_key = f'{_PENDING_SECRET_CACHE_PREFIX}{request.user.pk}'
    secret = cache.get(cache_key)
    if not secret:
        raise HttpError(400, "No pending 2FA setup found. Call /mfa/setup first.")

    if not validate_totp_code(secret, payload.code):
        raise HttpError(400, "Incorrect code.")

    cache.delete(cache_key)
    TOTP.activate(request.user, secret)
    recovery_codes = RecoveryCodes.activate(request.user).generate_codes()

    LOGGER.debug("MFA enrolled: user=%s", request.user.get_username())
    return MfaConfirmResponseSchema(recovery_codes=recovery_codes)


@router_mfa.post('/disable')
def disable(request, payload: MfaDisableSchema):
    """Require the current password (not just a bearer token) before turning
    off 2FA, so a stolen/leaked access token alone can't disable it."""
    if not request.user.check_password(payload.password):
        raise HttpError(401, "Incorrect password.")

    Authenticator.objects.filter(
        user=request.user,
        type__in=[Authenticator.Type.TOTP, Authenticator.Type.RECOVERY_CODES],
    ).delete()
    LOGGER.debug("MFA disabled: user=%s", request.user.get_username())
    return {'ok': True}
