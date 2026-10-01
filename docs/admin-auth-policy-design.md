# Technical Design: Admin-Managed Auth Policy & Bucket Access UI

Status: proposed, not yet implemented.
Builds on: `docs/sso-and-2fa-setup.md` (SSO via django-allauth, TOTP 2FA via
`p2/auth/mfa_api.py`) — read that first, this document assumes it.

## 1. Problem statement

Today, three things that a super admin should be able to control from the
web UI instead of editing `.env` or raw Django admin are still admin-only
via `/_/admin/`:

1. **SSO provider credentials** (`SocialApp` rows) — functional today, but
   only editable through Django admin, not the p2 Settings UI.
2. **Login method enforcement** — does not exist at all. Any user can log
   in with just a password, forever, regardless of what an admin wants.
   There is no way to say "everyone in this org must use SSO" or "this
   group must have 2FA."
3. **Bucket access control** — `VolumeACL` (`p2/core/acl.py`) already does
   real per-user/per-group bucket permissions (`read`/`write`/`delete`/
   `list`/`admin`) on every request. It has no UI at all — managing it
   today means either the Django admin's raw model form, or calling the
   Ninja volume API by hand.

This document designs #2 as new capability, and #1/#3 as UI-only work on
top of what already exists. It deliberately does **not** invent a second
bucket-permission system — `VolumeACL` stays the single source of truth for
bucket access; we are only adding a management UI for it, plus (in one
specific way, see §4.3) letting login-method policy read/gate on top of it.

## 2. Goals / non-goals

**Goals**
- A super admin can set an org-wide default login requirement (none / 2FA
  required / SSO only / SSO-or-2FA) and override it per Django `Group`.
- A super admin can add/edit/remove SSO provider credentials from the
  Settings UI, without touching Django admin or `.env`.
- A super admin can view and edit bucket ACLs (which users/groups have
  which permissions on which bucket) from the Settings UI.
- None of the above can lock out every admin at once (see §5, lockout
  safety — this is the section that most needs review before building).

**Non-goals**
- Per-object (key/prefix) ACLs — out of scope, `VolumeACL` is bucket-level
  only today and this doc doesn't change that granularity.
- SAML / enterprise IdP-initiated SSO — allauth's OIDC/OAuth2 providers
  only, as already scoped in `docs/sso-and-2fa-setup.md`.
- Self-service password reset / account recovery flows — unrelated.
- Rate-limiting/brute-force protection on `/auth/login` itself — should
  exist, but is a separate piece of work, not covered here.

## 3. New module: `p2.core.auth_policy`

New Django app-less module inside the existing `p2.core` app (it already
owns `acl.py`, so this is consistent placement — no new `INSTALLED_APPS`
entry needed).

```
p2/core/auth_policy.py          — AuthPolicy model + resolve_policy(user)
p2/core/api/auth_policy_api.py  — ninja router: policy CRUD, provider CRUD
p2/core/api/acl_api.py          — ninja router: VolumeACL CRUD (new, thin)
p2/core/migrations/00XX_authpolicy.py
```

### 3.1 `AuthPolicy` model

```python
# p2/core/auth_policy.py
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.db import models


class AuthPolicy(models.Model):
    """Org-wide default (group=NULL) or per-Group override of the required
    login method. Reuses the same Group model VolumeACL already uses for
    bucket permissions, rather than introducing a second grouping concept.
    """

    class Requirement(models.TextChoices):
        NONE = 'none', 'No requirement (password alone is allowed)'
        MFA_REQUIRED = 'mfa_required', 'Password + TOTP 2FA required'
        SSO_REQUIRED = 'sso_required', 'SSO only (password login disabled)'
        SSO_OR_MFA = 'sso_or_mfa', 'SSO OR password+2FA (bare password forbidden)'

    group = models.OneToOneField(
        Group, null=True, blank=True, on_delete=models.CASCADE,
        related_name='auth_policy',
        help_text="NULL = the org-wide default policy. Exactly one row may have group=NULL.",
    )
    requirement = models.CharField(
        max_length=20, choices=Requirement.choices, default=Requirement.NONE,
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [
            # Enforce "at most one global default row" at the DB level,
            # not just in application code.
            models.UniqueConstraint(
                fields=['group'], condition=models.Q(group__isnull=True),
                name='authpolicy_one_global_default',
            ),
        ]

    def __str__(self):
        return f"AuthPolicy({self.group or 'GLOBAL'} -> {self.requirement})"


# Strictness ordering — used to resolve "user is in two groups with
# different policies" by picking the strictest, never the weakest. This is
# the one line in this whole feature most worth a second pair of eyes:
# getting the ordering backwards silently creates a bypass.
_STRICTNESS = {
    AuthPolicy.Requirement.NONE: 0,
    AuthPolicy.Requirement.MFA_REQUIRED: 1,
    AuthPolicy.Requirement.SSO_OR_MFA: 1,   # same tier as MFA_REQUIRED: both
                                             # forbid bare password, neither
                                             # forbids the other's method.
    AuthPolicy.Requirement.SSO_REQUIRED: 2,  # strictest: forbids password
                                             # entirely, even with 2FA.
}


def resolve_policy(user) -> str:
    """Effective requirement for `user` = strictest policy across all
    Groups they belong to, falling back to the global default (or NONE if
    no global default row exists yet). Superusers are resolved separately
    by the caller (see §5.1) — this function does not special-case them,
    so it stays reusable/testable as "what would apply if not exempt."
    """
    group_reqs = list(
        AuthPolicy.objects.filter(group__in=user.groups.all())
        .values_list('requirement', flat=True)
    )
    global_req = (
        AuthPolicy.objects.filter(group__isnull=True)
        .values_list('requirement', flat=True)
        .first()
    ) or AuthPolicy.Requirement.NONE

    candidates = group_reqs + [global_req]
    return max(candidates, key=lambda r: _STRICTNESS[r])
```

### 3.2 Pseudo-code: enforcement in the login flow

This is the part that changes existing behavior, in
`p2/auth/mfa_api.py::login()`. Pseudo-code (not final syntax) showing the
new branches added around the existing password-check/MFA-gate logic:

```
function login(username, password):
    user = authenticate(username, password)
    if user is None or not user.is_active:
        return 401 "No active account found with the given credentials"

    if user.is_superuser:
        # Superuser exemption (see §5.1) — policy is advisory for them,
        # never enforced. Existing MFA-if-enrolled behavior only.
        if user has TOTP enrolled:
            return {mfa_required: true, mfa_token: issue_challenge(user)}
        return {**mint_jwt_pair(user)}

    policy = resolve_policy(user)

    if policy == SSO_REQUIRED:
        return 403 "This account must sign in via SSO. Password login is disabled."
        # Deliberately: do NOT fall through to MFA gate below — SSO_REQUIRED
        # forbids password login outright, 2FA on top of it is irrelevant.

    if policy in (MFA_REQUIRED, SSO_OR_MFA):
        if user has no TOTP enrolled:
            # Cannot silently allow password-only login (defeats the
            # policy) and cannot silently hard-lock the user out with no
            # path forward either. Return a distinct state the SPA
            # recognizes and routes to forced enrollment.
            return {mfa_setup_required: true, enrollment_token: issue_enrollment_token(user)}
        return {mfa_required: true, mfa_token: issue_challenge(user)}

    # policy == NONE
    if user has TOTP enrolled:
        return {mfa_required: true, mfa_token: issue_challenge(user)}
    return {**mint_jwt_pair(user)}
```

`enrollment_token` is a new, distinct short-lived cache token (same shape
as `mfa_token`, different cache prefix so the two can't be confused) that
authorizes exactly one call to a new `/auth/mfa/enroll-and-confirm`
endpoint — it lets a user who passed password auth, but has no JWT yet,
call `/mfa/setup` + `/mfa/confirm` before they're actually logged in. This
needs its own pair of endpoints (`enrollment_token` instead of a Bearer
JWT for auth) rather than reusing `/mfa/setup`/`/mfa/confirm` as-is, since
those currently require `request.user` from an existing JWT.

```
function enroll_and_confirm(enrollment_token, code):
    user = lookup_and_validate(enrollment_token)  # single-use, like mfa_token
    if user is None: return 401 "Enrollment session expired. Please log in again."

    pending_secret = cache.get(pending_secret_key_for(user))
    if pending_secret is None:
        # First call: no secret generated yet for this enrollment session.
        secret = generate_totp_secret()
        cache.set(pending_secret_key_for(user), secret, ttl=CHALLENGE_TTL)
        return {secret, otpauth_url, qr_svg}  # same shape as /mfa/setup

    if not validate_totp_code(pending_secret, code):
        return 400 "Incorrect code."

    TOTP.activate(user, pending_secret)
    recovery_codes = RecoveryCodes.activate(user).generate_codes()
    delete_enrollment_token(enrollment_token)
    return {**mint_jwt_pair(user), recovery_codes}
    # User is now logged in AND enrolled in one round trip — no second
    # "please log in again" needed right after forced enrollment.
```

### 3.3 Enforcement for SSO login path

`p2/auth/adapters.py`'s `P2AccountAdapter` also needs one check: if
`resolve_policy(user) == SSO_REQUIRED` or the login came through SSO at
all, no gate is needed — SSO already satisfies every policy tier except...
there isn't a tier that forbids SSO. The only thing to add here is:
**`MFA_REQUIRED` alone (not `SSO_OR_MFA`) should still be interpreted as
"2FA is mandatory even for SSO users"** if a deployment wants 2FA
regardless of login method. That's a product decision, not a technical
one — pseudo-code for the stricter interpretation:

```
function get_login_redirect_url(request):
    user = request.user
    if not user.is_superuser and resolve_policy(user) == MFA_REQUIRED:
        if user has no TOTP enrolled:
            redirect to SPA with enrollment_token (same as password path)
        # if enrolled, still just mint tokens — allauth's social login
        # already proved identity via the IdP; we don't re-prompt for
        # TOTP on top of a fresh SSO login in this design. (Flag: confirm
        # this is the desired UX before building — an alternative is to
        # always re-prompt TOTP after SSO too, which is stricter but adds
        # friction to the "SSO is supposed to be the easy path" story.)
    return build_jwt_redirect(user)
```

## 4. New module: Bucket ACL management API (thin wrapper)

No new authorization model — `VolumeACL` (`p2/core/acl.py`) already does
everything needed. This is purely exposing CRUD on it via Ninja, mirroring
the existing pattern in `p2/core/api/endpoints.py`.

```python
# p2/core/api/acl_api.py  (new file)
router_acl = Router(tags=["core-acl"])

@router_acl.get("/volumes/{volume_uuid}/acl/", response=List[VolumeACLSchema])
def list_acl(request, volume_uuid: str):
    vol = get_object_or_404(Volume, uuid=volume_uuid)
    require_admin_permission(request.user, vol)   # existing has_volume_permission(.., 'admin')
    return VolumeACL.objects.filter(volume=vol).select_related('user', 'group')

@router_acl.post("/volumes/{volume_uuid}/acl/", response=VolumeACLSchema)
def grant_acl(request, volume_uuid: str, payload: VolumeACLCreateSchema):
    vol = get_object_or_404(Volume, uuid=volume_uuid)
    require_admin_permission(request.user, vol)
    # exactly one of payload.user_id / payload.group_id must be set —
    # validate in the schema or here, mirroring VolumeACL's own
    # unique_together/nullable-pair constraint.
    acl, _ = VolumeACL.objects.update_or_create(
        volume=vol, user_id=payload.user_id, group_id=payload.group_id,
        defaults={'permissions': payload.permissions},
    )
    invalidate_acl(str(vol.pk))   # p2.s3.cache — already exists, already
                                  # called elsewhere on ACL/volume changes
    return acl

@router_acl.delete("/volumes/{volume_uuid}/acl/{acl_id}/")
def revoke_acl(request, volume_uuid: str, acl_id: int):
    vol = get_object_or_404(Volume, uuid=volume_uuid)
    require_admin_permission(request.user, vol)
    acl = get_object_or_404(VolumeACL, pk=acl_id, volume=vol)
    # Guard rail: don't let the last admin-permission ACL on a bucket be
    # deleted by the person deleting it, if it's their own only grant —
    # mirrors the auth-policy lockout concern at bucket scope. See §5.2.
    acl.delete()
    invalidate_acl(str(vol.pk))
    return {"ok": True}
```

`require_admin_permission` = the existing `_check_permission(user, vol,
'admin')` pattern already used in `create_volume`/`update_volume`/
`delete_volume` in `p2/core/api/endpoints.py` — reuse it, don't duplicate.

## 5. Lockout safety (the part that most needs scrutiny)

Any system that can restrict "how do I log in" has one failure mode that
matters more than the feature: **the policy itself becomes the outage.**
Three concrete safeguards, all of which should ship together, not as
follow-ups:

### 5.1 Superuser exemption

Superusers are **never** blocked by `AuthPolicy` (see the `if
user.is_superuser` branch in §3.2's pseudo-code — checked first, before
`resolve_policy` is even consulted for enforcement purposes). Rationale: if
an admin sets `SSO_REQUIRED` org-wide and the IdP has an outage or is
misconfigured, there must be at least one way in that doesn't depend on
the thing that just broke. Superusers can still voluntarily enroll in 2FA
or use SSO — this exemption only means policy can't lock them out, not
that they're barred from using stronger auth.

This should be visible in the UI (§7) as an explicit callout, not a silent
behavior — admins should know their own account is exempt so they don't
assume the policy protects their own login too.

### 5.2 Break-glass recovery path outside the web UI

A `manage.py` command, usable from a server console when the web UI itself
is unreachable (e.g. a bad policy combined with a superuser who also
somehow lost access):

```
python manage.py auth_policy --set-global none
python manage.py auth_policy --clear-group <group_name>
```

This is intentionally a direct DB write via the ORM, not an HTTP endpoint —
it must work even if the API/ninja layer, session middleware, or SSO
provider is the thing that's broken.

### 5.3 UI confirmation gates

- Changing the global policy to anything other than `NONE` shows a
  confirmation dialog stating how many active (non-superuser) users are
  affected, and reiterating the superuser exemption.
  `SELECT COUNT(*) FROM auth_user WHERE is_superuser=false AND is_active=true`
  scoped to the affected group (or all users, for the global policy).
- Setting `SSO_REQUIRED` specifically should require the admin to
  re-enter their password in the dialog (step-up confirmation) before the
  change is applied — mirrors the existing `/mfa/disable` password
  requirement pattern (`p2/auth/mfa_api.py`), reused for consistency.

### 5.4 Bucket ACL equivalent

Deleting the last `admin`-capable `VolumeACL` row on a bucket (whether
it's the acting admin's own grant or the only one left, e.g. via group
membership changes) should warn ("no one will be able to manage this
bucket's permissions afterward — are you sure?") rather than silently
succeed. Superusers bypass `VolumeACL` entirely already (`has_volume_permission`
short-circuits `is_superuser`), so this is a lesser risk than §5.1-5.3, but
still worth a confirmation since it can strand non-superuser bucket owners.

## 6. API surface summary

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/api/v1/system/auth-policy/` | List global + per-group policies | superuser |
| `PUT` | `/api/v1/system/auth-policy/{id}/` | Update a policy (global or group) | superuser, password re-confirm for `SSO_REQUIRED` |
| `POST` | `/api/v1/system/auth-policy/` | Create a per-group override | superuser |
| `DELETE` | `/api/v1/system/auth-policy/{id}/` | Remove a per-group override (falls back to global) | superuser |
| `GET` | `/api/v1/system/sso-providers/` | List configured `SocialApp` rows (no secret echoed) | superuser |
| `POST` | `/api/v1/system/sso-providers/` | Add provider credentials | superuser |
| `PUT` | `/api/v1/system/sso-providers/{id}/` | Update credentials (secret write-only) | superuser |
| `DELETE` | `/api/v1/system/sso-providers/{id}/` | Remove provider | superuser |
| `GET` | `/api/v1/core/volumes/{uuid}/acl/` | List bucket ACL entries | bucket `admin` perm |
| `POST` | `/api/v1/core/volumes/{uuid}/acl/` | Grant/update a user or group's permissions | bucket `admin` perm |
| `DELETE` | `/api/v1/core/volumes/{uuid}/acl/{id}/` | Revoke an ACL entry | bucket `admin` perm |
| `POST` | `/api/v1/auth/mfa/enroll-and-confirm` | Forced-enrollment completion (policy-driven) | `enrollment_token` (not JWT) |

New schemas needed in `p2/core/api/schemas.py` (`AuthPolicySchema`,
`AuthPolicyUpdateSchema`, `SocialAppSchema`/`SocialAppCreateSchema` with
`client_secret` marked write-only like `APIKeySchema` already treats
`secret_key_encrypted` today, `VolumeACLSchema`, `VolumeACLCreateSchema`).

## 7. Settings UI (`ui/src/pages/SettingsPage.vue`)

Add two new tabs alongside the existing `keys` / `users` / `security` /
`policies` / `config` tabs (superuser-gated, following the existing
`is_superuser` checks already used for the Users tab):

### 7.1 "Login Policy" tab (superuser only)

```
┌─ Login Policy ──────────────────────────────────────────────┐
│                                                               │
│  Organization default                                        │
│  ┌───────────────────────────────────────────────────────┐   │
│  │ ○ No requirement (password allowed)                   │   │
│  │ ○ Require 2FA (password + TOTP)                        │   │
│  │ ○ Require SSO or 2FA (no bare password)                │   │
│  │ ○ Require SSO only (password login disabled)           │   │
│  └───────────────────────────────────────────────────────┘   │
│  ⓘ Superusers are always exempt from this policy, to        │
│    prevent accidental lockout. [Save]                        │
│                                                               │
│  Per-group overrides                          [+ Add Override]│
│  ┌─────────────┬───────────────────────┬────────┐            │
│  │ Group        │ Requirement          │        │            │
│  ├─────────────┼───────────────────────┼────────┤            │
│  │ engineering  │ Require 2FA          │ [Edit] [Remove]      │
│  │ contractors  │ Require SSO only     │ [Edit] [Remove]      │
│  └─────────────┴───────────────────────┴────────┘            │
└───────────────────────────────────────────────────────────────┘
```

Save flow for the org-wide default (component pseudo-code, not final Vue):

```
function saveGlobalPolicy(newRequirement):
    affectedCount = await api.get(`/system/auth-policy/affected-count?requirement=${newRequirement}`)
    if newRequirement != 'none':
        confirmed = await confirmDialog({
            title: "Change organization login policy?",
            message: `This will affect ${affectedCount} user(s). Superusers are always exempt.`,
            theme: newRequirement == 'sso_required' ? 'red' : 'orange',
        })
        if not confirmed: return
    if newRequirement == 'sso_required':
        password = await promptPasswordReconfirm()
        # sent alongside the PUT so the backend can step-up-verify before applying
    await api.put('/system/auth-policy/global', {requirement: newRequirement, confirm_password: password})
    toast.success("Login policy updated")
```

### 7.2 "SSO Providers" tab (superuser only) — replaces raw Django admin

```
┌─ SSO Providers ─────────────────────────────────[+ Add Provider]┐
│  ┌────────────┬───────────────┬───────────────────┐             │
│  │ Provider    │ Client ID     │                    │             │
│  ├────────────┼───────────────┼───────────────────┤             │
│  │ 🔵 Google   │ 123...apps... │ [Edit] [Remove]     │             │
│  │ ⬛ GitHub   │ Iv1.abc...    │ [Edit] [Remove]     │             │
│  └────────────┴───────────────┴───────────────────┘             │
│                                                                   │
│  "Add Provider" dialog:                                          │
│    Provider:  [Google ▾]  (dropdown of installed provider apps)  │
│    Client ID: [________________________]                         │
│    Client Secret: [________________________]  (write-only)       │
│    Callback URL (read-only, copy button):                        │
│      https://yourhost/_/accounts/google/login/callback/          │
└───────────────────────────────────────────────────────────────────┘
```

The "Provider" dropdown is populated from a small static list matching
whatever's actually in `INSTALLED_APPS` server-side (new endpoint
`GET /api/v1/system/sso-providers/available` returning provider ids from
`allauth.socialaccount.providers.registry`) — so the UI never offers a
provider that isn't installed, and the callback URL shown is always
correct for that provider without hardcoding it client-side.

### 7.3 Bucket ACL panel — inside `BucketsPage.vue` (per-bucket), not Settings

Bucket permissions are a property of a bucket, not a global setting, so
this belongs as a tab/panel on the existing bucket detail view rather than
in Settings — check `ui/src/pages/BucketsPage.vue` / `ui/src/pages/buckets/`
for where bucket-scoped settings (versioning, encryption — already there
per `VolumeUpdateSchema`) currently live and add this alongside them.

```
┌─ Bucket "my-bucket" → Permissions ──────────────[+ Grant Access]┐
│  ┌──────────────┬─────────────────────────────┬──────┐          │
│  │ Grantee       │ Permissions                 │      │          │
│  ├──────────────┼─────────────────────────────┼──────┤          │
│  │ 👤 alice      │ read, write, list            │ [Edit][Remove]  │
│  │ 👥 engineering│ read, write, delete, list,   │ [Edit][Remove]  │
│  │               │ admin                        │                │
│  └──────────────┴─────────────────────────────┴──────┘          │
│                                                                    │
│  "Grant Access" dialog:                                           │
│    Grant to:  ○ User [dropdown]   ○ Group [dropdown]              │
│    Permissions: [ ] read [ ] write [ ] delete [ ] list [ ] admin  │
└─────────────────────────────────────────────────────────────────────┘
```

## 8. Data migration / rollout plan

1. Migration creates `AuthPolicy` table, no rows — `resolve_policy()`
   already defaults to `NONE` when no global row exists, so this ships
   inert. No existing login behavior changes until an admin explicitly
   sets a policy.
2. Ship `enroll-and-confirm` + policy-check branches in `mfa_api.py`
   behind the same "no rows = NONE = no behavior change" guarantee.
3. Ship the ACL API + UI — additive, `VolumeACL` behavior is unchanged,
   this is strictly a new read/write surface over existing data.
4. Ship the SSO provider management UI — additive, `SocialApp` rows
   created via the new UI behave identically to ones created via Django
   admin (same model, same allauth code path).
5. Only after all of the above is deployed and verified should any
   deployment actually flip a policy away from `NONE` — that's an
   operational decision for each deployment's admin, not something this
   change should default to.

## 9. Testing checklist

- `resolve_policy()`: user in zero groups (falls back to global), one
  group, multiple groups with different tiers (strictest wins), no global
  row at all (defaults to `NONE`).
- Login: superuser bypasses every policy tier; non-superuser under each of
  the four tiers, with and without TOTP already enrolled.
- Forced enrollment: `enrollment_token` single-use, expires correctly,
  results in both an activated `Authenticator` and a valid JWT pair in one
  round trip.
- SSO login under `MFA_REQUIRED`: forces enrollment same as password path.
- ACL API: non-admin on a bucket gets 403 from all three endpoints;
  deleting the last admin grant is confirmed/warned, not silently allowed
  to strand the bucket (product decision from §5.4 — confirm before
  enforcing this as a hard block vs. a soft warning).
- `manage.py auth_policy` break-glass command works against a database
  directly, independent of the web process being healthy.
