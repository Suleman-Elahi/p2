# SSO / Social Login & 2FA Setup

Scope: how p2 authenticates users via third-party identity providers
(Google, Microsoft, GitHub, and any other provider supported by
[django-allauth](https://docs.allauth.org/en/latest/socialaccount/providers/index.html)),
and via TOTP-based two-factor authentication as an alternative for users who
prefer a password + authenticator app over SSO — and how both plug into the
existing JWT-based session used by the Vue SPA (`ui/`).

2FA and SSO are independent, user-level choices: a user can enable TOTP 2FA
on their local username/password account, or use a social login provider
instead, or use neither (plain password login). They are not mutually
exclusive at the deployment level — both can be available at once, and the
individual user picks what to use on the login page.

## How it works

- **`django-allauth`** (`p2/core/settings.py` `INSTALLED_APPS`) handles the
  OAuth/OIDC handshake with each provider.
- **`p2/auth/adapters.py`** bridges allauth's session-based login into a
  ninja-jwt access/refresh token pair, because the SPA reads auth state from
  a JWT in `localStorage`, not from a Django session cookie. After a
  successful provider login, `P2AccountAdapter.get_login_redirect_url()` /
  `get_signup_redirect_url()` mint a token pair and redirect the browser to
  `/login?access=<jwt>&refresh=<jwt>`.
- **`ui/src/pages/LoginPage.vue`** reads those query params on mount, stores
  them via the same `saveTokens()` path used for password login, and routes
  into the dashboard — so SSO and password login both land in the same
  authenticated SPA state.
- **Provider credentials are never stored in code or `.env`.** Each provider
  is configured as a `SocialApp` database row via
  `/_/admin/socialaccount/socialapp/`. Enabling a new provider for a given
  deployment is a config change in the admin, not a code change or redeploy
  — as long as that provider's app is already listed in `INSTALLED_APPS`
  (see below).

## Login flow (for reference)

```
Browser                          p2 (Django)                      Provider (Google/etc)
   │  click "Sign in with Google"      │                                   │
   ├──── GET /_/accounts/google/login/ ─►                                   │
   │                                   ├──── redirect to consent screen ───►│
   │◄──── 302 to provider ─────────────┤                                   │
   │  user approves                    │                                   │
   ├────────────────────────────────── GET /_/accounts/google/login/callback/ ─►
   │                                   │◄──── code exchange ───────────────┤
   │                                   │  allauth resolves/creates User    │
   │                                   │  P2AccountAdapter mints JWT pair  │
   │◄──── 302 /login?access=..&refresh=.. ┤                                   │
   │  LoginPage.vue picks up tokens,   │                                   │
   │  saveTokens(), routes to app      │                                   │
```

## Currently wired providers

`p2/core/settings.py` `INSTALLED_APPS` lists:

- `allauth.socialaccount.providers.google`
- `allauth.socialaccount.providers.microsoft`
- `allauth.socialaccount.providers.github`

To add another provider (LinkedIn, X/Twitter, Dropbox, Slack, GitLab, etc.):

1. Add `'allauth.socialaccount.providers.<provider>'` to `INSTALLED_APPS` in
   `p2/core/settings.py`.
2. Restart the app (code change requires a deploy, but only once — no code
   is needed per deployment after that).
3. Register an OAuth app on that provider's developer console (see per-provider
   docs at the allauth link above — each provider has different setup steps).
4. Add the credentials as a `SocialApp` row in `/_/admin/socialaccount/socialapp/`
   (see below) — this step needs no deploy and no code change.

## Environment variables needed

**None are required for SSO to function.** Client ID/secret per provider are
**not** environment variables in this design — they live in the database as
`SocialApp` rows, editable via Django admin without a restart. This was a
deliberate choice so operators can add/rotate/disable providers without
touching `.env` or redeploying.

The only environment variables that matter here are ones p2 already requires
for any deployment, which SSO also depends on transitively:

| Variable | Required | Purpose |
|---|---|---|
| `P2_SECRET_KEY` | yes | Signs Django sessions (used during the OAuth handshake) and is the default ninja-jwt `SIGNING_KEY` (used for the JWT pair minted after SSO login). |
| `P2_REDIS__HOST` (+ related `P2_REDIS__*`) | yes | Django session backend is `django.contrib.sessions.backends.cache`, backed by Redis — the OAuth `state`/PKCE values are stored in the session during the redirect round-trip. If Redis is unreachable, the SSO login flow will fail at the callback step. |
| `P2_ALLOWED_HOSTS` | yes (prod) | Must include the domain used in each provider's "Authorized redirect URI" / callback URL, or Django will reject the callback request. |

Legacy note: `p2/core/settings.py` also still has `OIDC_ENABLED` /
`AUTHLIB_OAUTH_CLIENTS` (`oidc.client_id`, `oidc.client_secret`,
`oidc.discovery_url` env keys) — this is the older, single-provider
`authlib`-based OIDC flow that predates the allauth integration above. It's
independent of the SSO setup described here and only matters if a deployment
still has `P2_OIDC__ENABLED=true` configured. New deployments should use the
`SocialApp` admin flow instead.

## Setting up a provider (example: Google)

1. Create OAuth credentials in the
   [Google Developer Console](https://console.developers.google.com/):
   APIs & Services → Credentials → Create credentials → OAuth client ID →
   Web application.
2. Set **Authorized redirect URIs** to:
   `https://<your-domain>/_/accounts/google/login/callback/`
   (use `http://127.0.0.1:8787/_/accounts/google/login/callback/` for local dev).
3. Note the Client ID and Client secret.
4. In p2, go to `/_/admin/socialaccount/socialapp/add/`:
   - **Provider**: Google
   - **Name**: `Google` (your choice, just a label)
   - **Client id**: from step 3
   - **Secret key**: from step 3
   - **Sites**: add the current site (`example.com` / your `SITE_ID=1` site)
5. Save. The "Sign in with Google" button on `/login` will now work.

Repeat the same pattern for Microsoft (Azure AD app registration) and GitHub
(OAuth App under GitHub Settings → Developer settings → OAuth Apps), using
`/_/accounts/microsoft/login/callback/` and `/_/accounts/github/login/callback/`
respectively as the callback URL.

## Files involved (SSO)

| File | Role |
|---|---|
| `p2/core/settings.py` | `INSTALLED_APPS` (provider apps), `AUTHENTICATION_BACKENDS`, `ACCOUNT_ADAPTER`, `SOCIALACCOUNT_ADAPTER`, `SOCIALACCOUNT_PROVIDERS` (scopes only, no secrets) |
| `p2/auth/adapters.py` | `P2AccountAdapter` / `P2SocialAccountAdapter` — mints the ninja-jwt pair and redirects into the SPA |
| `p2/root/urls.py` | Mounts `allauth.urls` at `/_/accounts/` |
| `ui/src/pages/LoginPage.vue` | Renders provider buttons, picks up `access`/`refresh` query params on redirect back |
| `ui/src/stores/api.js` | `saveTokens()` / `decodeUserFromToken()` — shared with password login |

## 2FA (TOTP) — how it works

p2 uses `allauth.mfa`'s data model (`Authenticator`) and its TOTP/recovery-code
helper classes (secret generation, QR building, code validation), but **not**
its own views — those are session/HTML-form based and don't fit a JWT SPA.
Instead, `p2/auth/mfa_api.py` exposes a small set of ninja endpoints that call
into the same allauth helpers directly.

### Login flow with 2FA

```
POST /api/v1/auth/login  {username, password}
   │
   ├─ user has no TOTP enrolled ──► 200 {mfa_required: false, access, refresh}
   │                                  (SPA proceeds exactly like before)
   │
   └─ user has TOTP enrolled ─────► 200 {mfa_required: true, mfa_token: "..."}
                                       │
                                       ▼
                          POST /api/v1/auth/mfa/verify {mfa_token, code}
                                       │
                          code is a live 6-digit TOTP or a recovery code
                                       │
                                       ▼
                          200 {access, refresh}   (401 if wrong/expired)
```

The `mfa_token` is a short-lived (`MFA_CHALLENGE_TTL_SECONDS`, default 300s)
value stored server-side in the Django cache (Redis in production) — it is
not a JWT itself and carries no user data client-side, it is just an opaque
lookup key. A wrong code does NOT consume it: the client may retry with the
same token until it expires, succeeds, or hits the wrong-attempt limit
(then the server discards it and the user must log in again). Only a
successful verification consumes it, so it cannot be replayed for a second
login. If `/mfa/verify` reports the challenge expired/used, the SPA must
send the user back to the password step for a fresh token — retrying the
old one can never succeed.

### Enrollment flow (Settings → Security in the SPA)

```
POST /api/v1/auth/mfa/setup                     (requires existing JWT)
   → {secret, otpauth_url, qr_svg}
     secret is cached server-side, NOT yet written to the Authenticator
     table — nothing is "on" yet.

User scans qr_svg with an authenticator app, then:

POST /api/v1/auth/mfa/confirm {code}            (requires existing JWT)
   → validates code against the pending secret; if correct, activates the
     TOTP Authenticator + generates 10 recovery codes.
   → {recovery_codes: [...]}   (shown once — not retrievable again)

GET  /api/v1/auth/mfa/status                    (requires existing JWT)
   → {enabled: true|false}

POST /api/v1/auth/mfa/disable {password}        (requires existing JWT)
   → requires the current password (not just the bearer token) so a leaked
     access token alone cannot silently turn off 2FA.
   → removes both the TOTP and recovery-code Authenticator rows.
```

### Environment variables (2FA)

Like SSO, no secrets live in `.env` — the TOTP secret and recovery codes are
per-user, stored in the `Authenticator` table (`allauth.mfa`), not
deployment-wide config. Two optional settings exist, both with sane
defaults, configurable via `p2/lib/config.py`'s YAML/env loader:

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `MFA_TOTP_ISSUER` | `P2_MFA__TOTP_ISSUER` | `p2 Storage` | The issuer name shown in the authenticator app next to the account. |
| `MFA_CHALLENGE_TTL_SECONDS` | `P2_MFA__CHALLENGE_TTL_SECONDS` | `300` | How long a user has to enter their 2FA code after a correct password before the challenge expires and they must log in again. |

Same as SSO, the MFA challenge token is stored in the same Redis-backed
cache used for sessions — `P2_REDIS__HOST` must be reachable or the
`/mfa/setup` and `/mfa/verify` endpoints will fail.

### Security notes

- **Secrets are stored in plaintext by default.** `allauth.mfa`'s
  `encrypt()`/`decrypt()` hooks (`DefaultMFAAdapter.encrypt`/`.decrypt`) are
  no-ops out of the box — the TOTP secret is stored as-is in the
  `Authenticator.data` JSON column. If this matters for your deployment
  (e.g. compliance requirements around secrets-at-rest), set
  `settings.MFA_ADAPTER` to a subclass that encrypts/decrypts using
  `FERNET_KEY` (already used elsewhere in p2 for API key secrets) before
  going to production with 2FA enabled for real users.
- Recovery codes are shown exactly once, at enrollment time, and cannot be
  regenerated/viewed again through the current API — losing them plus the
  authenticator app device means the account's 2FA cannot be recovered
  through self-service; a superuser would need to delete the user's
  `Authenticator` rows via `/_/admin/mfa/authenticator/`.
- `/mfa/disable` intentionally requires the account password again, not just
  a valid bearer token, precisely because a bearer token can be stolen more
  easily than a password re-entry from the legitimate user.

### Files involved (2FA)

| File | Role |
|---|---|
| `p2/core/settings.py` | `INSTALLED_APPS` (`allauth.mfa`), `MFA_TOTP_ISSUER`, `MFA_CHALLENGE_TTL_SECONDS` |
| `p2/auth/mfa_api.py` | Login gate, verify, setup, confirm, disable, status — all ninja endpoints |
| `p2/api/ninja_api.py` | Mounts `router_login` at `/auth`, `router_mfa` at `/auth/mfa` |
| `ui/src/stores/auth.js` | `useLogin()` (now MFA-aware), `useMfaVerify()` |
| `ui/src/stores/settings.js` | `setupMfa()`, `confirmMfa()`, `disableMfa()`, `mfaEnabled` |
| `ui/src/pages/LoginPage.vue` | Second login step: TOTP/recovery code entry |
| `ui/src/pages/SettingsPage.vue` | "Security" tab: enable/disable 2FA, QR code + recovery codes UI |
