# 017: Event Hosting Access & Self-Serve Organizer Workspace

## Context & Problem

Previously, only global Administrators could initiate event creation in VoxBento. To get an event running, an administrator had to create it, configure it, and manually assign event ownership to an organizer. People who signed up on their own landed on an empty account page with no indication of how to host an event or request access.

## Architecture & Target Model

1. **Admins delegate hosting access**: Super administrators decide who can host events via `/admin/users/` (toggling hosting access directly for registered accounts, or sending email invitations with hosting access tokens).
2. **First-party Organizers**:
   - Invited users redeem a secure token at `/invite/accept/{token}`, set up their password, and immediately gain hosting access (`can_host = True`), landing directly in their workspace (`/workspace/`).
   - Active hosts can access `/workspace/` and create new events with the guided setup wizard (`/workspace/setup`).
   - The creator of an event is automatically recorded as an `event_owner` in `event_memberships`.
3. **Non-hosts & Visitors**:
   - Signed-in users without hosting access or owned events see an "Event Hosting" card on `/account` with a button to "Request Hosting Access".
   - Non-hosts attempting to access `/workspace/setup` or `/admin/setup` are redirected to `/account?error=hosting_access_required` with an explanatory notification banner.
   - Machine integrations (`POST /api/v1/events/`, `GET /oauth/authorize`, `POST /api/events/{slug}/booths`) remain completely exempt from `can_host` gating to ensure full backwards compatibility with external services (e.g. Eventyay).

## Data Model & Migrations

- **Migration `029_add_can_host_to_users`**:
  - Adds `can_host` (Boolean, default `False`), `hosting_requested_at` (DateTime, nullable), and `hosting_revoked_at` (DateTime, nullable) columns to the `users` table using Alembic batch operations for SQLite compatibility.
  - Automatically backfills `can_host = True` for all existing users with an active `event_owner` membership.
  - Logs backfilled user IDs for auditability.
- **SQLite Configuration**:
  - PRAGMA `busy_timeout=5000` is guaranteed to execute before `journal_mode=WAL` to prevent lock contention.

## Security & Concurrency Mitigations

- **Invite Redemption Security**:
  - Tokens (`token_type="hosting_invite"`) are validated with constant-time equality checks and single-use enforcement (`is_used` check and two-phase concurrency check).
  - Primitives (`user_id`, `email`, `display_name`, `is_admin`) are copied within the session block before context exit to prevent `DetachedInstanceError`.
  - Same-origin verification (`verify_same_origin`) protects all invitation and hosting toggle endpoints against CSRF.
  - Inactive users are forbidden from receiving invitations or redeeming tokens.
- **Rate Limiting**:
  - Event creation (`/workspace/setup`, `/workspace/events/`) enforces validation and slug collision pre-checks before recording against the IP rate limiter, ensuring duplicate slug collisions do not consume quota.
  - Access request endpoint (`/api/request-demo`) enforces 20 req/hour per IP and 3 req/hour per email, strips CRLF injection characters, and derives `user_id` strictly from session authentication.

## Verification

- Comprehensive test coverage in `tests/test_hosting_access.py` alongside existing suites in `tests/test_workspace_routes.py`, `tests/test_user_auth.py`, and `tests/test_admin_panel.py`.
