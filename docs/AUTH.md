# OIDC Authentication

Solara serves reads to anonymous users. Every write requires a browser
cookie session from an OIDC login whose identity is on the allowlist.

## Flow

- `GET /oidc/login` — stores a validated `next` path in the session, then
  redirects to the provider (authorization code + PKCE via Authlib).
- `GET /oidc/callback` — exchanges the code, validates the ID token
  (state, nonce, signature via Authlib), stores `{iss, sub, email}` in
  the session, marks it permanent, and redirects to `next`.
- `GET /oidc/logout` — clears the session and redirects to `/`.
- `GET /api/me` — `{"authenticated": bool, "can_write": bool}`. Cosmetic
  only; the server-side gate is the real protection.

Authentication (OIDC login) and authorization (allowlist) are separate
steps. The allowlist matches on `(iss, sub)` pairs, never on email.

## The write gate

A single `@app.before_request` hook in `oidc_auth.py` enforces all of
this, so any future write route is protected by default:

1. `GET`, `HEAD`, `OPTIONS`, and the auth endpoints pass through.
2. Any other request without header `X-Requested-With: fetch` → 403.
3. No session user → 401.
4. Session user not on the allowlist → 403.

There are currently no write routes in the app; the gate exists so the
first one (and any reboots that add one) is protected automatically.

## Configuration

Set in `.env` (see `env.example`):

- `FLASK_SECRET_KEY` — session signing secret. Generate with
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `OIDC_ISSUER` — issuer URL; discovery runs against
  `<OIDC_ISSUER>/.well-known/openid-configuration`. Leave unset to
  disable the login flow (writes fail closed either way).
- `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` — the OAuth client
  credentials issued by the provider.
- `OIDC_ALLOWED_SUB` — comma-separated `iss|sub` pairs allowed to
  write. An empty allowlist denies all writes (fail closed).
- `PUBLIC_URL` — the public base URL (existing knob). The provider
  redirect URI is `<PUBLIC_URL>/oidc/callback`; register that exact
  URI with the provider.
- `SESSION_COOKIE_SECURE` — leave unset: it follows `PUBLIC_URL`
  (secure on https, off for local http). Set explicitly only when the
  proxy chain needs it.
- `PROXY_TRUSTED_HOPS` — number of reverse-proxy hops to trust for
  `X-Forwarded-For/Proto/Host` (ProxyFix). Default `0` disables it.
  Set to the real hop count when serving behind an HTTPS proxy.

Session cookies: `HttpOnly`, `SameSite=Lax`, 30-day permanent lifetime.

## Bootstrapping the allowlist

With `OIDC_ALLOWED_SUB` empty, the callback logs the authenticated
identity at INFO so it can be copied into config:

```text
OIDC login bootstrap: iss=... sub=... not yet allowlisted; writes denied
```

Copy the pair into `.env` as `OIDC_ALLOWED_SUB=iss|sub` (comma-separated
for several users), then restart the app. This log line exists only for
bootstrap; it is not a user-facing feature. Remove it once every
deployment has a non-empty allowlist.

## Providers

The issuer is the only per-provider input, so Google, Authentik, and
Keycloak all work through the same discovery URL:

- Google: `https://accounts.google.com`
- Authentik: `https://<host>/application/o/<slug>/`
- Keycloak: `https://<host>/realms/<realm>`

## Deployment

The app binds `127.0.0.1:8000` under gunicorn; behind a TLS-terminating
proxy, set `PUBLIC_URL` to the public `https://...` URL and
`PROXY_TRUSTED_HOPS` to the hop count. The Tesla OAuth flow remains on
`/api/v1/tesla/auth/initiate` and `/callback`, so the OIDC redirect URI
is intentionally namespaced under `/oidc/callback`.

## Frontend

The header shows a sign-in link that becomes a sign-out (or read-only
note) based on `/api/me`. A `window.solaraFetch` wrapper sends
`X-Requested-With: fetch` on writes and redirects to `/oidc/login` on a
401. No HTML forms post writes, so no CSRF token is needed; the fetch
header check serves that role.
