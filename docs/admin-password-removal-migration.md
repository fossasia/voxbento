# Removing the legacy `ADMIN_PASSWORD` login

`ADMIN_PASSWORD` no longer grants administrative access. The `/admin/login` page
now accepts only the email and password of a registered user who is either a
site-wide administrator or has an event-owner or room-coordinator membership.

## Before rollout

1. Back up the database and verify that at least two active administrator
   accounts can sign in with their own email addresses and passwords. Do this
   while the current release is still running.
2. Remove `ADMIN_PASSWORD` from `.env`, Docker/Compose overrides, CI secrets,
   Kubernetes manifests, and any other deployment-secret store. It is ignored
   by the application, so leaving it set does not preserve access.
3. Keep `SECRET_KEY` (or `JWT_SECRET`) and `API_KEY_ENCRYPTION_KEY` set to
   their existing strong values. They remain required for session signing and
   encrypted provider credentials.

## Rollout

1. Deploy the release that removes the shared-password fallback.
2. Restart the portal using the normal deployment procedure.
3. In a private browser window, sign in at `/admin/login` with an existing
   administrator account. Confirm that the response sets `user_token`, not
   `admin_token`.
4. Verify a non-administrator cannot sign in to the admin panel, then revoke
   the retired shared secret from the secret manager and deployment history.

## Break-glass recovery

If every administrator loses access, use a controlled database-console session
to promote an **existing active registered user**, then sign in normally. Record
the incident and remove the emergency privilege when it is no longer needed.

For PostgreSQL:

```sql
UPDATE users SET is_admin = TRUE WHERE email = 'operator@example.com';
```

For SQLite:

```sql
UPDATE users SET is_admin = 1 WHERE email = 'operator@example.com';
```

This is intentionally a database operation, not an environment-variable
fallback: it is auditable through controlled database access and cannot be
enabled accidentally by an old deployment secret.
