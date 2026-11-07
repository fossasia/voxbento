# Registration abuse protection

Email verification controls account activation, but registration stores an
unverified account and queues an email before the mailbox owner clicks a link.
Signup protection rejects abusive requests before these side effects.

## Deploy without interrupting existing users

Turnstile defaults to disabled. Deploying this change requires no new keys,
database migration, or dependency changes. Password and passwordless signup,
email verification, login and invitations retain their existing behavior.
Every `POST /register` is limited to 20 attempts per resolved client IP per
60 seconds, including invalid submissions and requests for existing accounts.
The limit is configurable with `REGISTRATION_REQUESTS_PER_MINUTE` (minimum 1).
Excess attempts receive a form with HTTP 429 and `Retry-After: 60`.

The IP limiter uses the existing in-process store: counters reset on restart
and are not shared between workers. Shared event Wi-Fi or NAT also shares an
allowance. Adjust the limit for expected signup traffic and use Cloudflare
rate limiting for enforcement across processes and distributed attacks.

## Enable Turnstile

1. In Cloudflare Turnstile, create a **Managed** widget restricted to your
   production hostname. This can be used even without Cloudflare DNS proxying.
2. Set `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY` in the deployment's private
   environment, and ensure `PUBLIC_BASE_URL` uses the hostname visitors use.
   Keep the secret out of source control and browser code. Do not use Cloudflare
   test keys in production.
3. Set `REGISTRATION_TURNSTILE_ENABLED=true` and recreate the portal container
   using the normal deployment procedure. Docker Compose already reads `.env`.
4. Check `/register` in a real browser: complete the check, submit with and
   without a password, and verify that the normal email confirmation appears.
   A direct POST without `cf-turnstile-response` must return 403 and create
   neither a user nor an email. Verify the email link still works.
5. If your proxy adds a Content Security Policy, allow
   `https://challenges.cloudflare.com` in `script-src` and `frame-src`.
   The portal loads Cloudflare's external script only on the registration form
   when enabled. JavaScript and access to Cloudflare are then required to sign up.

The server validates every token with Cloudflare Siteverify, including
`success`, the configured hostname and the `register` action. Cloudflare rejects
expired and already-used tokens. Missing or rejected tokens return 403; a
timeout, invalid service response, or missing enabled configuration returns
503. These responses render the signup form again with a new widget and retain
the name/email, never passwords. No account lookup, password hash, mutation,
verification token or email is performed for a rejected challenge.

To roll back the integration, set `REGISTRATION_TURNSTILE_ENABLED=false` and
recreate the portal. The IP limit remains active. Disabling the widget also
disables server verification, so this is a deliberate reduction in protection.
Changing keys or encountering an outage never silently disables verification.

## Client IPs behind Cloudflare and Caddy

The application uses `request.client.host`; it does **not** read arbitrary
`X-Forwarded-For` or `CF-Connecting-IP` headers. Uvicorn resolves the client
from `X-Forwarded-For`, walking right to left past trusted peers only:

- **Caddy** (`trusted_proxies` on the portal's `reverse_proxy`) keeps `X-Forwarded-For` only
  from Cloudflare's published ranges and appends the Cloudflare peer. From any
  other peer, including a direct request to the origin, Caddy replaces the
  header with that peer's address, so it cannot be forged.
- **Uvicorn** (`FORWARDED_ALLOW_IPS` in `docker-compose.yml`) trusts the Docker
  network gateway that Caddy reaches the portal through, then the same
  Cloudflare ranges. Set `PORTAL_PROXY_GATEWAY` in `.env` to that gateway:
  `docker network inspect voxbento_default -f '{{(index .IPAM.Config 0).Gateway}}'`.
  Without it, Uvicorn trusts only `127.0.0.1` and every visitor appears as the
  gateway, sharing one rate-limit allowance.
- Keep both Cloudflare lists in sync with <https://www.cloudflare.com/ips/>.
  Keep the portal's loopback-only host port binding; do not trust broader ranges.

After deploying, confirm the portal access log shows distinct client addresses
for two networks rather than the gateway, and that sending a forged
`X-Forwarded-For` straight to the origin does not change the logged address.

Site-wide bot blocking remains an edge responsibility. Scope browser
challenges carefully so authenticated API clients, WebSocket connections,
WHIP/WHEP, HLS and Jitsi traffic continue to work. Turnstile reduces signup
abuse; it does not guarantee that all bots are blocked, nor delete old accounts.

## Validation

Run `uv run pytest tests/test_registration_protection.py tests/test_user_auth.py`.
Tests use an isolated database and mock the Siteverify HTTP boundary, covering
missing/oversized/replayed tokens, bad hostname/action, malformed replies,
timeouts, IP throttling and both existing signup flows. They do not require
live Cloudflare credentials. Cloudflare's test widget can check browser rendering;
its canned Siteverify hostname/action are not a substitute for the production
hostname/action acceptance test.

References: [server validation](https://developers.cloudflare.com/turnstile/get-started/server-side-validation/),
[test keys](https://developers.cloudflare.com/turnstile/troubleshooting/testing/),
[WAF challenges](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/create-custom-rule/).
