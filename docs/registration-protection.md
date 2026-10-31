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
`X-Forwarded-For` or `CF-Connecting-IP` headers. Before relying on per-client
limits in production, configure the proxy chain to resolve the real peer:

- Restrict origin web access so clients cannot bypass Cloudflare. Keep the
  portal's existing loopback-only host port binding.
- Configure Caddy to trust only Cloudflare's published IP ranges for forwarded
  client identity, and Uvicorn to trust only the immediate reverse proxy's
  address (`FORWARDED_ALLOW_IPS`, accounting for Docker networking).
- Verify requests from two distinct client networks resolve to different
  client IPs, and that a forged forwarding header cannot choose that identity.
  Without this configuration, the limiter may group visitors under a proxy IP;
  choose an appropriate allowance while correcting the proxy chain.
- Do not set unrestricted proxy trust on a publicly reachable server.

This PR does not change the deployed proxy trust configuration or Cloudflare
rules. Site-wide bot blocking remains an edge responsibility. Scope browser
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
