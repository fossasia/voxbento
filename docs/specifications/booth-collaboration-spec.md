# Booth Collaboration Specification

This document defines the rules for booth participation: roles, active interpreter assignment, handoff protocol, and internal chat.

---

## Roles

Each participant in a booth has exactly one role, set at join time.

| Role | Can publish ingest audio | Can reassign active role | Can use booth chat |
|---|---|---|---|
| **Active Interpreter** | Yes — exclusively | Yes (can hand off to backup) | Yes |
| **Backup Interpreter** | No — standby only | No (can request; coordinator approves) | Yes |
| **Coordinator** | No | Yes — full authority | Yes |
| **Listener** | No | No | Yes (read-only by convention) |

Roles are set via the `role` parameter in the `booth:join` WebSocket message. The server validates the role against allowed values `['interpreter', 'coordinator', 'listener']`.

---

## Active interpreter enforcement

At any point in time, there is **at most one active interpreter** per booth (per language channel).

### Auto-assignment on join

When the first interpreter joins a booth, they are automatically set as the active interpreter:

```python
if participant.role == 'interpreter' and booth.active_interpreter_id is None:
    booth.active_interpreter_id = participant.participant_id
```

Subsequent interpreters join as backup (standby) regardless of join order.

### Manual assignment

The active role can be reassigned via the `booth:set-active` WebSocket message. Authorization rules:

1. A **coordinator** can reassign active from any participant to any interpreter.
2. The **current active interpreter** can hand off to any other interpreter (self-initiated handoff).
3. An interpreter can assign themselves active if no other active interpreter is set.
4. **No other combinations are permitted** — backup interpreters cannot promote themselves without coordinator or active interpreter approval.

Server enforcement (`BoothRegistry.set_active_interpreter`):

```python
requester_is_coordinator = requester.role == 'coordinator'
requester_is_active_interpreter = booth.active_interpreter_id == requester_id
requester_is_target = requester_id == target_id

if not (requester_is_coordinator or requester_is_active_interpreter or requester_is_target):
    raise PermissionError(...)
```

### Side effects of reassignment

When the active interpreter changes:

1. `booth.active_interpreter_id` is updated.
2. All participants' `mic_active` and `ingest_connected` flags are cleared except for the new active interpreter (who retains their existing state).
3. `booth.ingest_status` is recomputed.
4. If a previous active interpreter had an open ingest session, `ingest.disconnect(channel_id)` is called to terminate it server-side.
5. The updated state is broadcast to all booth participants via `booth:state`.

---

## Handoff protocol

A handoff is the process of transferring the active interpreter role from one person to another.

### Planned handoff (coordinator-initiated)

1. Coordinator identifies that rotation is needed (time, fatigue, terminology preference).
2. Coordinator selects the backup interpreter in the participant grid and clicks **Set Live**.
3. Server calls `set_active_interpreter(requester=coordinator, target=backup)`.
4. Previous active interpreter's ingest connection is terminated.
5. New active interpreter sees their participant card updated to active status.
6. New active interpreter clicks **Go Live** to start their ingest session.

### Self-initiated handoff (active interpreter to backup)

1. Active interpreter selects the backup interpreter in the participant grid.
2. Clicks **Set Live** (visible because they are the active interpreter).
3. Same server flow as coordinator-initiated.

### Emergency handoff (active interpreter disconnects)

1. Active interpreter's WebSocket connection drops.
2. WebSocket disconnect handler calls `booths.leave_participant(...)`.
3. `leave_participant` detects the leaving participant was active and calls `_pick_next_interpreter(booth)`.
4. `_pick_next_interpreter` returns the first available interpreter in the roster (FCFS).
5. `booth.handoff_state` is set to `'pending'`.
6. Updated state is broadcast to all booth participants.
7. The new active interpreter is expected to click **Go Live** promptly.

### Handoff state machine

```
idle
 │  (first interpreter joins with no active)
 ▼
active (normal operating state)
 │  (coordinator or active reassigns to backup)
 ▼
completed  →  active (new active interpreter)
 │  (active interpreter disconnects unexpectedly)
 ▼
pending  →  active (next interpreter accepts)
      └──►  idle (no more interpreters in booth)
```

---

## Booth chat

Internal booth chat is visible only to booth participants. It is not accessible to viewers or other booths.

### Rules

- Any participant can send a message.
- Messages are broadcast to all participants in the booth via WebSocket.
- The server retains the last 500 messages per booth (in memory).
- Messages are also persisted to `localStorage` in the browser for the duration of the session.
- Empty messages are rejected by the server.
- Message body is stripped of leading/trailing whitespace before storage.

### Chat message structure

```json
{
  "message_id": "uuid-hex",
  "sender_id": "participant-id",
  "sender_name": "Display Name",
  "body": "Message text",
  "sent_at": "2025-01-01T12:00:00+00:00"
}
```

### Use cases

- Coordinator announces rotation time: "Switch in 10 minutes."
- Backup interpreter flags a terminology question.
- Active interpreter signals they need relief: "Can someone take over? Struggling with the accent."
- Coordinator acknowledges: "Amira, you're live in 2 minutes."

---

## Participant state model

Each participant's card in the booth grid reflects their real-time state:

| State field | Possible values | Meaning |
|---|---|---|
| `role` | `interpreter` / `coordinator` / `listener` | Assigned role |
| `connected` | `true` / `false` | WebSocket connection alive |
| `mic_active` | `true` / `false` | Microphone stream active |
| `ingest_connected` | `true` / `false` | WebRTC ingest session active |
| Active badge | computed from `active_interpreter_id` | Whether this participant is currently live |

`mic_active` and `ingest_connected` can only be `true` for the active interpreter. The server enforces this in `update_participant_state`:

```python
if wants_publisher_state and booth.active_interpreter_id != participant_id:
    raise PermissionError('Only the active interpreter can mark mic or ingest active.')
```

---

## Coordinator display

The coordinator's view of the participant grid shows all participants with full state visibility, plus **Set Live** buttons on all non-active interpreter cards.

The coordinator does not see a **Go Live** button — coordinators cannot publish ingest audio.

---

## Listener role

A listener can join the booth (e.g., a supervisor, a relay interpreter from another language booth waiting for context). They see the participant grid and booth chat but cannot:

- Go live
- Reassign the active interpreter

---

## Multi-booth isolation

Each `booth_id` has a completely independent state in the `BoothRegistry`. Participants in `hall-a-fr` do not see participants in `hall-a-de`. There is no cross-booth communication.

---

## Known limitations (current phase)

- Booth state is in-memory. If the server restarts, all participants must rejoin.
- There is no authentication of participant identity beyond the booth access token. Any user with the token URL can join as any role.
- The backup interpreter cannot request a handoff — they must wait for coordinator or active interpreter to initiate.
- There is no typing indicator in booth chat.
