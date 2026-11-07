# Meeting source (managed Jitsi floor agent)

An event owner can make a room's online meeting the floor source from the room
page. VoxBento starts a managed agent that joins the meeting as a muted,
listen-only participant (**VoxBento FloorBot**), publishes the meeting audio to
the room's canonical floor path, and feeds the same transcription, translation
and TTS pipeline as program ingest. No shell access, container command or
separate bot-start step is needed during an event.

Only the self-hosted Jitsi integration is supported. Zoom, Teams, Google Meet
and Webex need provider-specific agents and are not implied by this feature.

## Owner workflow

1. Open **Workspace → Events → (event) → Rooms → (room)**.
2. In **Room audio**, choose **From the online meeting**, keep **Jitsi Meet (self-hosted)**, confirm the meeting
   URL and click **Connect meeting**.
3. Watch the status: **Starting → Joined → Receiving audio → Processing**.
4. To move a connected agent to another meeting, edit the URL and click
   **Switch meeting**; there is no need to disconnect first.
5. Click **Disconnect** to remove the agent from the meeting and stop floor
   processing.

If program ingest owns the room's floor, the button reads **Switch to meeting**.
VoxBento asks for confirmation before it disconnects the encoder, revokes its
token and connects the meeting. Selecting the meeting never replaces an encoder
implicitly. Enabling program ingest likewise stops the meeting agent.

Meeting URLs must use `https://`. Plain `http://` is accepted only for the bundled
Jitsi (the configured Jitsi host or `localhost`), which the agent reaches over the
private network anyway. The URL entered here is saved as the room's Jitsi URL, which
interpreters also use for floor monitoring. The URL can carry a Jitsi `jwt` query parameter; it is
never logged, and status responses never include it.
Changing the URL here or in Room Settings while the agent is connected moves the
agent to the new meeting within a few seconds. A corrected URL also gets a fresh
reconnect budget, so fixing a wrong URL recovers a room that shows **Needs attention**.

## States

| State | Meaning |
|---|---|
| Starting | The agent is launching its browser or joining the meeting. |
| Joined | The agent is in the meeting; audio has not reached the media server yet. |
| Receiving audio | Audio reaches the floor path; the transcription worker is starting. |
| Processing | Decoded audio is being transcribed. |
| Reconnecting | The agent failed or exited and is restarted with back-off. |
| Needs attention | Automatic recovery stopped, or room settings block processing. The message names the fix. |
| Stopped | The meeting agent is not connected. |

## Ownership and recovery

- `rooms.meeting_agent_enabled` (migration 027) is the durable desired state.
  The floor-bot `/status` endpoint is the observed state. `portal/meeting_source.py`
  reconciles them every two seconds under the per-room lock shared with program ingest.
- There is at most one agent process per room. Repeated **Connect** clicks
  adopt the running agent instead of joining a second participant, and the floor
  worker is keyed by the canonical floor booth ID.
- A failed agent restarts after 2, 5, 10, 20 and 30 seconds, up to
  `MEETING_AGENT_MAX_RESTARTS` (default 5) attempts per incident. Sixty seconds
  of healthy processing ends the incident and refills the budget. When the
  budget is exhausted, the room shows **Needs attention** until the owner
  connects again.
- If the agent is in the meeting but no audio reaches MediaMTX for
  `MEETING_AGENT_NO_AUDIO_SECS` (default 45), the agent is restarted.
- The floor bot reports a SHA-256 fingerprint of the URL it joined (never the URL
  itself), so the portal can tell whether a running agent is in the configured meeting.
- After a portal restart, reconciliation adopts agents the database still wants
  (rejoining any whose fingerprint shows an outdated meeting),
  restarts missing ones and stops agents for rooms that no longer want one. While
  no room wants an agent, this sweep still runs every 30 seconds, so an agent left
  behind by a source takeover during a floor-bot outage is removed. A
  disconnect that cannot reach the floor-bot service returns `202` with
  `cleanup_pending: true`; the desired state is already cleared and the stale
  agent is stopped on the next successful reconcile.

## Deployment

- The `floor-bot` service must be reachable from the portal at `FLOOR_BOT_BASE`
  (default `http://floor-bot:8080`) and must not be exposed publicly.
- The bundled Jitsi is joined through `JITSI_INTERNAL_BASE`. A meeting on another
  Jitsi deployment is joined at the URL the owner enters.
- Apply migration 027 with `uv run alembic upgrade head`. It adds one boolean
  column with a `false` default; existing rooms stay stopped until an owner connects.
- Rebuild the `floor-bot` image so the agent reports join and audio failures and
  its meeting fingerprint. An older image still works, but every failure is reported
  as an unexpected exit, and an agent adopted after a portal restart is assumed to
  be in the configured meeting.

## Troubleshooting

| Message | Check |
|---|---|
| The meeting agent service is unreachable | `docker compose ps floor-bot`, then `FLOOR_BOT_BASE`. |
| The agent could not join the meeting | Open the URL in a browser. Disable the lobby and password for the floor room, or put a valid `jwt` in the URL. |
| The meeting URL redirects to a different room | The server redirected the agent to another room. Enter the URL the meeting finally opens at; the agent refuses cross-room redirects so it never listens to the wrong meeting. |
| The room URL did not open a Jitsi meeting | The URL points at a non-Jitsi page. Use the meeting URL on the self-hosted Jitsi server. |
| The agent was removed from the meeting | A moderator kicked the agent or the meeting ended. Reconnect when the meeting is live. |
| No meeting audio reached the media server | Check `docker compose logs floor-bot` for FFmpeg errors and `docker compose logs mediamtx` for the `<event>/<room>/floor` path. |
| Floor transcription is disabled / Select the floor source language | Fix the room's Floor Audio Transcription settings; the agent stays in the meeting. |

Floor-bot logs redact `jwt`, `token` and `password` values in query, JSON and
`key: value` forms, and never print the join URL. A redirect is accepted only if it
stays on the same host and room; it may upgrade `http` to `https`, never downgrade.

## Rehearsal

Before an event, in the production room:

1. Connect the meeting and confirm **Processing** within a minute of someone speaking.
2. Confirm floor captions on `/listener/<event>`, plus translation and TTS in at
   least two languages if enabled.
3. Remove **VoxBento FloorBot** from the meeting as a moderator. Confirm
   **Reconnecting**, then **Processing** again.
4. Restart the portal (`docker compose restart portal`). Confirm the same agent is
   adopted (one FloorBot participant) and captions resume.
5. Click **Disconnect** and confirm the participant leaves the meeting.
6. If the room also uses program ingest, switch sources both ways and confirm
   each switch asks for confirmation.

Rollback: click **Disconnect** in each room. To roll back the schema, back up the
database and run `uv run alembic downgrade 026`.
