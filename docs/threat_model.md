# OAuth Multi-Organizer Trust Model

## Threat Model & Security Posture

### Previous Vulnerability (Implicit Trust)
Historically, the system operated under an "implicit trust" model for OAuth clients. If an OAuth client requested scopes (e.g., `events:write`, `events:read`) for an event, the system implicitly assumed that the client was authorized to manage that event on behalf of the user, provided the user had authorized the client. 

This created a vulnerability:
1. **Public Clients** (e.g., SPAs, Mobile Apps) could theoretically spoof authorization flows or impersonate event owners if they could trick an event owner into authorizing them.
2. **Cross-Organizer Token Reuse**: A token generated for Event A could potentially be used to access Event B if the OAuth client possessed it, leaking existence of events or permitting unauthorized actions.

### The New Trust Model
To mitigate these vulnerabilities, we have shifted from implicit trust to explicit verification:

1. **Confidential vs. Public Clients**:
   - **Confidential Clients** (e.g., backend servers with a secure `client_secret`) are trusted to assume owner scopes for ownerless events (e.g., during auto-provisioning) and during multi-organizer authorization. The actual user permission verification is deferred to the trusted client.
   - **Public Clients** (e.g., SPAs without a secret) are **never** implicitly trusted to assume owner scopes. They must rely on explicitly granted `EventMembership` roles assigned to the user authorizing the client.

2. **Strict Scope Verification (`get_effective_scopes`)**:
   - During the OAuth authorization flow (`/oauth/authorize`), the system checks the user's `EventMembership` for the specific `event_id` requested.
   - If the user is an `event_owner`, `super_admin`, or `owner`, they can grant all valid scopes.
   - If the user is a `room_coordinator`, they can only grant a subset of scopes (e.g., `rooms:read`, `sessions:manage`).
   - If the user lacks a membership, the authorization is rejected with a `403 Forbidden`, unless the client is a trusted confidential client managing an ownerless event or multi-organizer authorization.

3. **Preventing Event Existence Leaks (404/403 Consistency)**:
   - When an API endpoint (e.g., `/api/v1/events/{event_slug}`) is accessed with an OAuth token, the system validates the token's `event_id`.
   - If a token issued for Event A is used to access Event B, the system throws a `401 Unauthorized` (which causes a `404 Not Found` for the requested event logic) to ensure that the error response does not leak whether Event B actually exists to unauthorized tokens. This prevents enumeration attacks.

### Verification (Tests)
These constraints are enforced and verified by our test suite in `tests/test_oauth_multi_organizer.py`:
- `test_oauth_confidential_client_trust`: Confirms confidential clients can assume owner scopes for ownerless events.
- `test_oauth_public_client_rejection`: Confirms public clients are rejected with a 403 when trying to assume owner scopes.
- `test_token_reuse_wrong_organizer`: Confirms that reusing a token from Event A on Event B results in a rejection that does not leak the existence of Event B.
