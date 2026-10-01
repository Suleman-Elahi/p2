"""Manual smoke test for the TOTP 2FA flow — exercised once during
implementation to verify the endpoints work end-to-end, then removed.
"""
import base64
import hashlib
import hmac
import struct
import time

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

User = get_user_model()

# The MFA challenge/pending-secret cache entries use Django's default cache
# (Redis in production). Use local-memory here so this test doesn't require
# a running Redis instance — behavior of cache.set/get/delete is identical.
pytestmark = pytest.mark.django_db
_LOCMEM_CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


def _hotp(secret, counter):
    counter_bytes = struct.pack('>Q', counter)
    secret_enc = base64.b32decode(secret.encode('ascii'), casefold=True)
    hmac_result = hmac.new(secret_enc, counter_bytes, hashlib.sha1).digest()
    offset = hmac_result[-1] & 0x0F
    truncated = bytearray(hmac_result[offset:offset + 4])
    truncated[0] &= 0x7F
    value = struct.unpack('>I', truncated)[0] % 10 ** 6
    return f'{value:06}'


@override_settings(CACHES=_LOCMEM_CACHES)
def test_totp_enrollment_and_login_flow(client):
    user = User.objects.create_user(username='mfa_test_user', password='correct-pw-123', email='mfa@test.local')

    # 1. Login with no MFA enrolled -> tokens issued immediately.
    r = client.post('/api/v1/auth/login', data={'username': 'mfa_test_user', 'password': 'correct-pw-123'}, content_type='application/json')
    assert r.status_code == 200, r.content
    body = r.json()
    assert body['mfa_required'] is False
    access = body['access']
    assert access

    # 2. Setup MFA.
    r = client.post('/api/v1/auth/mfa/setup', content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {access}')
    assert r.status_code == 200, r.content
    setup_body = r.json()
    secret = setup_body['secret']
    assert setup_body['otpauth_url'].startswith('otpauth://totp/')
    assert '<svg' in setup_body['qr_svg']

    code = _hotp(secret, int(time.time()) // 30)

    # 3. Confirm enrollment -> recovery codes returned.
    r = client.post('/api/v1/auth/mfa/confirm', data={'code': code}, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {access}')
    assert r.status_code == 200, r.content
    recovery_codes = r.json()['recovery_codes']
    assert len(recovery_codes) == 10

    # 4. status endpoint reflects enrollment.
    r = client.get('/api/v1/auth/mfa/status', HTTP_AUTHORIZATION=f'Bearer {access}')
    assert r.status_code == 200
    assert r.json()['enabled'] is True

    # 5. Login again -> now gated behind MFA.
    r = client.post('/api/v1/auth/login', data={'username': 'mfa_test_user', 'password': 'correct-pw-123'}, content_type='application/json')
    assert r.status_code == 200, r.content
    login_body = r.json()
    assert login_body['mfa_required'] is True
    assert login_body['access'] is None
    mfa_token = login_body['mfa_token']

    # 6. Verify with a fresh TOTP code -> real JWT pair issued.
    code2 = _hotp(secret, int(time.time()) // 30)
    r = client.post('/api/v1/auth/mfa/verify', data={'mfa_token': mfa_token, 'code': code2}, content_type='application/json')
    assert r.status_code == 200, r.content
    verify_body = r.json()
    assert verify_body['access']
    assert verify_body['refresh']

    # 7. Challenge token is consumed on success — replay must fail.
    r = client.post('/api/v1/auth/mfa/verify', data={'mfa_token': mfa_token, 'code': code2}, content_type='application/json')
    assert r.status_code == 401

    # 8. Wrong password on login still rejected normally.
    r = client.post('/api/v1/auth/login', data={'username': 'mfa_test_user', 'password': 'wrong'}, content_type='application/json')
    assert r.status_code == 401

    # 9. A recovery code also works as a second factor.
    r = client.post('/api/v1/auth/login', data={'username': 'mfa_test_user', 'password': 'correct-pw-123'}, content_type='application/json')
    mfa_token2 = r.json()['mfa_token']
    r = client.post('/api/v1/auth/mfa/verify', data={'mfa_token': mfa_token2, 'code': recovery_codes[0]}, content_type='application/json')
    assert r.status_code == 200, r.content

    # 10. Disable requires the password.
    new_access = r.json()['access']
    r = client.post('/api/v1/auth/mfa/disable', data={'password': 'wrong'}, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {new_access}')
    assert r.status_code == 401
    r = client.post('/api/v1/auth/mfa/disable', data={'password': 'correct-pw-123'}, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {new_access}')
    assert r.status_code == 200, r.content

    r = client.get('/api/v1/auth/mfa/status', HTTP_AUTHORIZATION=f'Bearer {new_access}')
    assert r.json()['enabled'] is False


@override_settings(CACHES=_LOCMEM_CACHES)
def test_mfa_verify_survives_wrong_code_then_succeeds(client):
    """Regression test: a wrong TOTP code must NOT burn the MFA challenge.
    The same mfa_token must still accept the correct code afterwards, and
    codes with pasted whitespace (e.g. "123 456") must be accepted."""
    user = User.objects.create_user(username='mfa_retry_user', password='correct-pw-123', email='retry@test.local')

    r = client.post('/api/v1/auth/login', data={'username': 'mfa_retry_user', 'password': 'correct-pw-123'}, content_type='application/json')
    access = r.json()['access']

    r = client.post('/api/v1/auth/mfa/setup', content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {access}')
    secret = r.json()['secret']
    code = _hotp(secret, int(time.time()) // 30)
    r = client.post('/api/v1/auth/mfa/confirm', data={'code': code}, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {access}')
    assert r.status_code == 200, r.content

    r = client.post('/api/v1/auth/login', data={'username': 'mfa_retry_user', 'password': 'correct-pw-123'}, content_type='application/json')
    mfa_token = r.json()['mfa_token']

    # Wrong code -> 401 "Incorrect code.", but the challenge must survive.
    r = client.post('/api/v1/auth/mfa/verify', data={'mfa_token': mfa_token, 'code': '000000'}, content_type='application/json')
    assert r.status_code == 401
    assert 'Incorrect code' in r.json()['detail']

    # Correct code (with pasted whitespace) on the SAME token -> success.
    code2 = _hotp(secret, int(time.time()) // 30)
    spaced = f'{code2[:3]} {code2[3:]}'
    r = client.post('/api/v1/auth/mfa/verify', data={'mfa_token': mfa_token, 'code': spaced}, content_type='application/json')
    assert r.status_code == 200, r.content
    assert r.json()['access']

    # ...but the consumed challenge still cannot be replayed.
    r = client.post('/api/v1/auth/mfa/verify', data={'mfa_token': mfa_token, 'code': code2}, content_type='application/json')
    assert r.status_code == 401
