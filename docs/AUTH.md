# OIDC Authentication

Solara gates data reads and writes behind an OIDC browser-cookie session.
Every data read requires an allowlisted session when `OIDC_ISSUER` is set;
every write requires one in all configurations.

## Flow

- `GET /oidc/login` — stores a validated `next` path in the session, then
  redirects to the provider (authorization code + PKCE via Authlib).
- `GET /oidc/callback` — exchanges the code, validates the ID token
  (state, nonce, signature, issuer, audience, expiry via Authlib), then
  stores `{iss, sub, email}` in the session from the validated
  `token["userinfo"]` without re-parsing the ID token. Missing or
  malformed claims fail closed with `502` and store no session user.
  Marks the session permanent and redirects to `next`.
- `GET /oidc/logout` — clears the session and redirects to `/`.
- `GET /api/me` — `{"authenticated": bool, "can_read": bool,
  "can_write": bool, "login_required": bool}`. Cosmetic only; the
  server-side gate is the real protection.

Authentication (OIDC login) and authorization (allowlist) are separate
steps. The allowlist matches on `(iss, sub)` pairs, never on email.

## The read gate

A single `@app.before_request` hook in `oidc_auth.py` enforces this, so
any future route is protected by default. `_can_access()` is the single
allowlist check; both `can_read` and `can_write` use it.

Data reads — `/`, `/api/v1/tou`, `/api/v1/load/status`,
`/api/v1/metrics`, `/api/v1/tesla/status`, and `/stream/status` —
require an allowlisted OIDC session when `OIDC_ISSUER` is set. The gate
applies to every request source, including loopback.

When `OIDC_ISSUER` is unset, anonymous reads are allowed only when both
`request.remote_addr` and the request host are loopback. This keeps
local development and the test client working. A non-loopback request
is denied with `503` because no login flow exists to satisfy the gate.
The loopback exception never applies when `OIDC_ISSUER` is set.

Public paths (safe reads, no session needed):

```text
/health
/api/me
/oidc/login
/oidc/callback
/oidc/logout
/api/v1/tesla/auth/initiate
/callback
/static/*
```

The Tesla paths are public because `/initiate` is gated by its one-time
token and `/callback` is gated by the OIDC state plus `TESLA_OWNER_EMAILS`.
`/health` is a probe endpoint; it still exposes some internal state
(error counts, MQTT/cache state), so do not add energy or credential
data to it.

### Response matrix

| Request | Session / allowlist | Result |
|---|---|---|
| Public safe path | any | pass |
| Data read, OIDC configured, anonymous | none | HTML page: `302` to `/oidc/login?next=...`; API/SSE/partial: `401` |
| Data read, OIDC configured, authenticated but not allowlisted | user, no match | `403` |
| Data read, OIDC configured, allowlisted | match | pass |
| Data read, OIDC unconfigured, loopback request | none | pass |
| Data read, OIDC unconfigured, non-loopback request | none | `503` with a config message |
| Write | no fetch header | `403` |
| Write | fetch header, no session | `401` |
| Write | fetch header, session, not allowlisted | `403` |
| Write | fetch header, allowlisted session | pass |

A redirect is used only for HTML page requests (`/` and `/api/v1/tou`
with `Accept: text/html`). API paths, `X-Requested-With: fetch`
requests, and `?partial=` requests return JSON `401`. SSE
(`/stream/status`) returns a plain `401`; EventSource cannot read the
status, so the dashboard probes `/api/me` on error and redirects only
when `login_required` is true.

Writes keep the existing `X-Requested-With: fetch` requirement, session
requirement, and allowlist check. There are currently no write routes
in the app; the gate exists so the first one is protected
automatically. There is no CSRF token: the custom fetch header plus
`SameSite=Lax` is the CSRF control. A permissive CORS policy would
break that property — do not add one without a real CSRF token.

## Configuration

Set in `.env` (see `env.example`):

- `FLASK_SECRET_KEY` — session signing secret. Generate with
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
  Mandatory when `OIDC_ISSUER` is set: startup raises `RuntimeError`
  when it is empty. When OIDC is not configured, a random per-process
  key is generated and a warning is logged; sessions do not survive
  restart. There is no fixed fallback key.
- `OIDC_ISSUER` — issuer URL; discovery runs against
  `<OIDC_ISSUER>/.well-known/openid-configuration`. Must use `https`;
  `http` is accepted only for loopback hosts (`localhost`, `127.0.0.1`,
  `::1`). An insecure issuer makes the login flow return `503` and
  data reads fail closed. Leave unset to disable the login flow
  (writes fail closed either way; reads become loopback-only).
- `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` — the OAuth client
  credentials issued by the provider.
- `OIDC_ALLOWED_SUB` — comma-separated `iss|sub` pairs allowed to read
  and write. An empty allowlist denies all reads and writes (fail
  closed).
- `PUBLIC_URL` — the public base URL (existing knob). The provider
  redirect URI is `<PUBLIC_URL>/oidc/callback`; register that exact
  URI with the provider. Include any subpath prefix in this value
  (for example `https://host/solara`), because the app cannot detect
  the prefix that the proxy strips.
- `SESSION_COOKIE_SECURE` — leave unset: it follows `PUBLIC_URL`
  (secure on https, off for local http). Set explicitly only when the
  proxy chain needs it.
- `PROXY_TRUSTED_HOPS` — exact number of reverse-proxy hops to trust for
  `X-Forwarded-For/Proto/Host/Prefix` (ProxyFix). Default `0` disables
  it. Must stay `0` when `OIDC_ISSUER` is unset: startup raises
  `RuntimeError` otherwise, because forwarded headers would let a remote
  client spoof the loopback read exception. When OIDC is enabled,
  startup warns and the proxy must append or overwrite the headers.
  See the configuration safety matrix in `docs/SECURITY.md` (M6).

Session cookies: `HttpOnly`, `SameSite=Lax`, 30-day permanent lifetime.
The allowlist is checked on every request; there is no server-side
session store or token revocation.

## Rate limiting

`/oidc/login` and `/oidc/callback` share an in-memory fixed-window
limiter keyed by `request.remote_addr`: 20 requests per 60 seconds per
IP, returning `429` with `Retry-After: 60` beyond that. The lock covers
one process, so multi-worker (gunicorn) deployments get a per-worker
limit; put a shared limit at the reverse proxy. `reset_auth_rate_limits()`
exposes a reset helper for tests. There is no read rate limit for
`/api/v1/tou` or `/stream/status` (see M4 in `docs/SECURITY.md`).

## Bootstrapping the allowlist

With `OIDC_ALLOWED_SUB` empty, the callback logs the authenticated
identity at INFO so it can be copied into config:

```text
OIDC login bootstrap: iss=... sub=... not yet allowlisted; writes denied
```

Copy the pair into `.env` as `OIDC_ALLOWED_SUB=iss|sub` (comma-separated
for several users), then restart the app. The log line exists only for
bootstrap; it is not a user-facing feature. Remove it once every
deployment has a non-empty allowlist.

## Diagnosing a denial

With `DEBUG` enabled, `oidc_auth` logs these lines:

- `OIDC config: issuer=... allowlist=[...]` — at startup, the parsed
  pairs (not the raw env value), so a typo is visible.
- `OIDC login: iss=... sub=... allowlisted=False` — at the callback,
  when a non-empty allowlist rejects the identity.
- `OIDC access denied: iss=... sub=... not in allowlist` — on each
  denied request from an authenticated session.

Anonymous denials log nothing, so unknown clients cannot add debug
noise. The lines are off unless `DEBUG` is on.

The match is exact. The `iss` claim from Google is
`https://accounts.google.com` with no trailing slash, even when
`OIDC_ISSUER` has one. The parser does not trim spaces around `|`.

## Providers

The issuer is the only per-provider input, so Google, Authentik, and
Keycloak all work through the same discovery URL:

- Google: `https://accounts.google.com`
- Authentik: `https://<host>/application/o/<slug>/`
- Keycloak: `https://<host>/realms/<realm>`

## Deployment

The app binds `127.0.0.1:8000` under gunicorn; behind a TLS-terminating
proxy, set `PUBLIC_URL` to the public `https://...` URL and
`PROXY_TRUSTED_HOPS` to the exact hop count. The proxy must append or
overwrite `X-Forwarded-For`, `X-Forwarded-Proto`, and `X-Forwarded-Host`;
never forward a client-supplied `X-Forwarded-For` unchanged. OIDC must
be enabled whenever hops > 0: startup refuses the unsafe combination.
The Tesla OAuth flow remains on `/api/v1/tesla/auth/initiate` and
`/callback`, so the OIDC redirect URI is intentionally namespaced under
`/oidc/callback`.

To mount the app under a subpath such as `/solara`, strip the prefix at
the proxy and send it in `X-Forwarded-Prefix`:

```nginx
location /solara/ {
    proxy_pass http://127.0.0.1:8000/;
    proxy_set_header X-Forwarded-Prefix /solara;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
}
```

`ProxyFix` turns that header into `SCRIPT_NAME`, so login, logout, and
the post-login redirect stay under `/solara`. Set
`PUBLIC_URL=https://<host>/solara` and register
`https://<host>/solara/oidc/callback` with the provider. The dashboard
HTML and `static/app.js` already use relative URLs, so they need no
prefix configuration. See the safety matrix in `docs/SECURITY.md` (M6).

Enable OIDC before exposing the app publicly: anonymous data reads are
denied once `OIDC_ISSUER` is set, and a non-loopback deployment with
OIDC unset returns `503` for data reads. Behind a proxy, OIDC must be
enabled — a proxy that rewrites the peer address and host to loopback
would weaken the loopback exception.

## Frontend

The header shows a sign-in link that becomes a sign-out (or read-only
note) based on `/api/me` (`can_read` drives the read state,
`login_required` drives the redirect). A `window.solaraFetch` wrapper
sends `X-Requested-With: fetch` on partial fragment fetches and
redirects to `/oidc/login?next=...` on a `401`. On SSE `onerror` the
client re-fetches `/api/me` and redirects only when `login_required`
is true, which avoids a redirect loop for authenticated but
non-allowlisted users (they get `403`, not a login redirect). No HTML
forms post writes, so no CSRF token is needed; the fetch header check
serves that role.
