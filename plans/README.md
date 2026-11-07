# Implementation plans

Handoff plans written so a contributor or coding agent with no prior context can
implement a feature step by step. Each plan lists files in scope, current-code
excerpts, ordered steps with verification commands, a test plan, machine-checkable
done criteria and STOP conditions.

How to use a plan:

1. Read the plan fully, then run its **drift check**. If the code it quotes has
   changed, refresh the plan (or ask) before implementing.
2. Claim it by commenting on the tracking issue with the plan number, and set its
   row below to `IN PROGRESS (@you)` in your PR.
3. Follow the steps in order; stop and report on any STOP condition instead of improvising.
4. In your implementation PR, set the row to `DONE` (or `BLOCKED: reason`).

Status values: TODO | IN PROGRESS | DONE | BLOCKED (reason) | REJECTED (rationale)

## YouTube intake (#1087)

Goal: organisers streaming on YouTube get captions, translation and TTS without the
Jitsi screen-share + floor-bot workaround and without copying stream keys
(Sign in with YouTube → pick the broadcast → one tailored instruction). Read
[the design brief](youtube-intake-design.md) first. Parent epic: #746; plan 007
coordinates with #751. Plans were written against `dev` at `250aefde`.

| Plan | Title | Priority | Effort | Depends on | Status |
|---|---|---|---|---|---|
| [002](002-youtube-intake-spike.md) | Verify the four YouTube-intake assumptions (spike) | P1 | S | — | IN PROGRESS (@hazelmayank, @SanketKokate712) |
| [003](003-browser-tab-audio-source.md) | Browser-tab audio source in the program sender | P1 | S | 002 (step 1) | DONE |
| [004](004-rtmps-destination-without-relay.md) | RTMPS as an independent second destination | P1 | M | 002 (step 2); invariant-3 approval | TODO |
| [005](005-connect-youtube-account.md) | Sign in with YouTube and pick a broadcast | P1 | L (2 PRs) | 002 (step 3) | TODO |
| [006](006-youtube-native-captions.md) | Post captions to YouTube's native caption track | P2 | M | 005; 002 (step 4) | TODO |
| [007](007-from-youtube-guided-flow.md) | Guided "From a YouTube stream" room-audio choice | P2 | M | 003, 004, 005, 006; #751 | TODO |

Dependency notes: 003, 004 and 005 are independent of each other once their spike
step passes; 003 and 005 can start immediately, while 004 must also wait for maintainer
approval of the invariant-3 wording update before implementation. 006 needs 005's OAuth
client and room columns. 007 only composes existing pieces. Plan numbering starts at 002;
numbers are stable because plans reference each other.

### Considered and rejected

- Server-side yt-dlp/streamlink "paste a link" puller: violates YouTube API developer policies III.E.1, is blocked from datacenter IPs, contradicts invariant 3 and #746 non-goals.
- Floor bot opening the YouTube watch/embed page: same blocking; automated access and background-player rules.
- Embedding the YouTube player in VoxBento and capturing its audio: "separate, isolate, or modify the audio … components" is prohibited.
- Listener page playing YouTube muted with our TTS: "apply alternate audio tracks to videos" is prohibited.
- VoxBento creating and binding its own YouTube `liveStream`: silently changes the organiser's YouTube setup; reading the bound stream is enough.
