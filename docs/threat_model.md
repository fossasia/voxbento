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
   - If a token issued for Event A is used to access Event B, `_verify_token_rbac` raises `404 Not Found` with the body `{"detail": "Event not found"}` — byte-for-byte the same response an unknown slug or a soft-deleted event produces. This holds for **every** client type: public clients are not more trusted than confidential ones, so they must not get a distinguishable `403`. A caller therefore cannot tell "no such event" from "not yours", which is what prevents enumeration attacks.
   - `403 Forbidden` is reserved for the case where the token *is* scoped to the requested event but the underlying user has since lost access — a deactivated account (`User.is_active == False`) or a removed `EventMembership`. Such a caller already holds a token naming that event, so returning `403` leaks nothing new.
   - Because the unauthorized and nonexistent cases are identical, endpoints resolve existence **before** RBAC (the ordering used throughout `portal/routers/api_v1.py`); no information is gained either way.

### Verification (Tests)
These constraints are enforced and verified by our test suite in `tests/test_oauth_multi_organizer.py`:
- `test_oauth_confidential_client_trust`: Confirms confidential clients can assume owner scopes for ownerless events.
- `test_oauth_public_client_rejection`: Confirms public clients are rejected with a 403 when trying to assume owner scopes.
- `test_token_reuse_wrong_organizer`: Confirms that reusing a token from Event A on Event B results in a rejection that does not leak the existence of Event B.

`tests/test_api_v1_get_event.py` covers the same policy from the resource side on `GET /api/v1/events/{slug}`:
- `test_unknown_slug_and_wrong_event_token_are_indistinguishable`: an unknown slug and an existing-but-wrong-event slug return the identical status and body, with a public client token.
- `test_deactivated_user_token_is_rejected`: a still-unexpired token stops working once its user is deactivated.
