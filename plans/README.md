# Implementation plans

Start here to understand what the work is for, what already works, and what to do
next. The individual plans retain the detailed files, commands and tests needed
by contributors.

## YouTube captions and translation: the goal

An organiser is already streaming an event on YouTube. They want VoxBento to hear
the event, write captions, translate them and offer translated speech to listeners.
They should be able to connect their YouTube account, choose the broadcast and
follow instructions that fit their streaming setup.

There are two separate jobs:

1. **Get the event's sound into VoxBento.** The organiser sends a copy from their
   streaming software, sends through VoxBento on the way to YouTube, or shares a
   browser tab's sound. Connecting a YouTube account alone does not send audio.
2. **Choose where people receive the results.** VoxBento's listener page provides
   captions, translations and translated speech. Plan 006 adds captions inside
   YouTube's own **CC** button. Translated speech stays on VoxBento's listener page.

VoxBento does not download a broadcast from a pasted YouTube link. A link identifies
which broadcast to connect; sound still arrives through one of the three routes
below. Read the [design brief](youtube-intake-design.md) for the rationale.

Tracking issue: [#1087](https://github.com/fossasia/voxbento/issues/1087).
Parent epic: [#746](https://github.com/fossasia/voxbento/issues/746).
The shared room setup also belongs to [#751](https://github.com/fossasia/voxbento/issues/751).

## What each plan is trying to achieve

| Plan | In plain English | What the organiser gets |
|---|---|---|
| [002: Check the assumptions](002-youtube-intake-spike.md) | Try the browser, streaming tools and YouTube with a real test broadcast before relying on them. | Evidence that the advertised setup actually works, with unsupported tools clearly identified. |
| [003: Share a browser tab's sound](003-browser-tab-audio-source.md) | Let an organiser choose a playing browser tab as the sound source. | A short route from the tab to VoxBento, without sharing it into a meeting first. |
| [004: Send VoxBento an extra copy](004-rtmps-destination-without-relay.md) | Add VoxBento as another destination in streaming software while the normal YouTube output continues. | Captions and translation without making YouTube depend on VoxBento staying online. |
| [005: Connect a YouTube account](005-connect-youtube-account.md) | Ask permission to see the channel's broadcasts, let the owner choose one, and fill in its private YouTube destination automatically when forwarding. | Less copying of sensitive stream keys and a clear view of which broadcast the room uses. |
| [006: Put captions in YouTube's CC button](006-youtube-native-captions.md) | Send finished caption text back to the chosen broadcast. | Viewers can read one caption language without leaving YouTube. |
| [007: Put the choices into one guided setup](007-from-youtube-guided-flow.md) | Ask how the organiser streams, then show the relevant instructions and checks. | One room-page journey instead of several disconnected controls. |

Plan numbers are stable identifiers, not the order in which a user clicks things.
For example, plan 003 is the browser-tab work; it is not the account-connection work.

## Current status — reviewed 12 November 2026

**The complete feature is still unfinished. None of the three open implementation
PRs below is ready to merge at the reviewed heads.** This documentation update does
not implement their fixes or certify a live deployment.

| Plan | Status | Evidence and remaining work |
|---|---|---|
| 002 | IN PROGRESS | [Results](002-results.md) merged in [#1133](https://github.com/fossasia/voxbento/pull/1133). Steps 1, 3 and 4 have measurements; step 2 still needs real third-party streaming-tool tests. |
| 003 | DONE | Tab capture merged in [#1097](https://github.com/fossasia/voxbento/pull/1097); [#1231](https://github.com/fossasia/voxbento/pull/1231) later preserved the saved sync delay when starting/reconnecting. |
| 004 | IN PROGRESS — rehearsal remaining | Implementation and invariant approval merged in [#1102](https://github.com/fossasia/voxbento/pull/1102); cleanup follow-up merged in [#1152](https://github.com/fossasia/voxbento/pull/1152). The streaming-platform rehearsal is still pending. |
| 005 | IN PROGRESS — changes requested | Backend [#1107](https://github.com/fossasia/voxbento/pull/1107) and room controls/docs [#1108](https://github.com/fossasia/voxbento/pull/1108) are open. Fix the findings below and finish the live application rehearsal. |
| 006 | IN PROGRESS — part 1 merged | [#1153](https://github.com/fossasia/voxbento/pull/1153) merged the caption delivery engine and connections from the caption/translation pipeline. It sends nothing until configured. Part 2 still needs the controls, saved choice, startup/recovery and shutdown handling. |
| 007 | IN PROGRESS — partial proposal, changes requested | [#1125](https://github.com/fossasia/voxbento/pull/1125) combines the source-selection fix for [#1119](https://github.com/fossasia/voxbento/issues/1119) with an incomplete guide. Finish it against 005/006, or separate the narrow fix. |

Earlier drafts: #1090 was superseded by #1097; #1091/#1096 were superseded by #1133.
Do not reopen their old work or recreate the merged foundations. Existing forwarding
to YouTube came from #804; the browser sender from #756/#947; the shared room-page
choices from #1039. Deleted-event cleanup landed in #1016 and #1152.

### What is holding the open PRs up?

These are findings on specific reviewed versions, not permanent claims about later
commits. Recheck the review and current head before starting work.

| PR and reviewed head | Problem in ordinary terms | Required result |
|---|---|---|
| [#1107, `534cf342`](https://github.com/fossasia/voxbento/pull/1107#pullrequestreview-5872114981) | Trying to reconnect can cancel a working Google connection if Google briefly fails. A new settings test also fails the current password-like secret checks. | A failed reconnect leaves existing/shared access working; the settings test uses a valid test secret; a real broadcast-to-relay journey is demonstrated. |
| [#1108, `aef5c43a`](https://github.com/fossasia/voxbento/pull/1108#pullrequestreview-5872115423) | After the owner removes a forwarding destination, a routine status refresh can add it back. The branch also needs updating against current dev and the final backend. | Removing the destination keeps forwarding stopped; carry the backend fixes into this PR, resolve conflicts and demonstrate the real successful flow. |
| [#1125, `8b6f2b40`](https://github.com/fossasia/voxbento/pull/1125#pullrequestreview-5872115846) | Setup buttons lack the request token current dev requires; the extra-destination route chooses the browser transport; the new key is lost; forwarding instructions leave the owner without usable credentials. | Each button completes the advertised setup, uses the correct protected request/transport, shows the newly created key intentionally, and includes working destination controls. |

The earlier database migration collision is **fixed** in the reviewed 005 branches:
revision 042 follows 041. Fresh install, upgrade from 041 and downgrade/re-upgrade
passed. Keep that migration in the backend PR once; choose any later migration
number from the actual merged head rather than copying the old examples.

## The three ways sound can reach VoxBento

| Your setup | What you do | What happens if VoxBento goes down? |
|---|---|---|
| Streaming software with a second output | Keep its YouTube destination; add VoxBento as another protected streaming destination (plan 004). | The independent YouTube output keeps going. |
| An encoder with one output, such as a simple OBS setup | Send to VoxBento, which forwards to YouTube (existing relay, connected by plan 005). Paste VoxBento's newly created server/key into the encoder. | The YouTube stream can also stop. The guide must say this before switching. |
| A stream playing in your browser | Open the VoxBento sender, choose browser-tab sound, select the playing tab and share its audio (plan 003). | YouTube keeps playing; VoxBento's captions/translation can stop. Both browser tabs must stay open. |

A **stream key** is the private credential that permits publishing a stream.
**Forwarding** (also called a relay) means VoxBento receives the stream and sends it
on to YouTube. **RTMPS** is the encrypted streaming connection used by encoders;
**WHIP** is the browser-compatible publishing connection. The application must not
issue a credential for one and tell the organiser to use it with the other.

## Remaining work, step by step

1. **Get the outstanding streaming-tool evidence (002/004).** A maintainer provides
   a test room and confirms the public encrypted streaming endpoint is available.
   Someone with access to the relevant streaming tools sends speech to YouTube and
   VoxBento at once. Record the tool/version, whether it accepts the server/key,
   whether captions appear, and whether YouTube stays live after the VoxBento key
   is revoked. Add measured results to [002-results.md](002-results.md) in a new PR;
   #1133 is already merged. At least one supported platform must have a successful
   verdict; keep untested platforms labelled pending rather than claiming support.
   Paid StreamYard/Restream access was an outstanding constraint in the discussion.
2. **Finish the account backend (005, #1107).** Fix failed-reconnect revocation,
   test existing/shared connections, and correct the failing settings fixture.
   Keep the migration following the current database history. Run the regression
   checks and request a fresh review of the new head.
3. **Finish the room controls (005, #1108).** Update the branch with the final
   backend and current dev. Make an explicit Remove action stay removed; a status
   refresh must not restart forwarding. Test choosing a broadcast before enabling
   forwarding as well as choosing one afterwards. Merge the backend before the UI
   layer, with the migration introduced only once.
4. **Prove the account connection with a real channel (005).** Use an unlisted test
   broadcast: sign in, choose it, confirm the room is linked, send an encoder stream
   through VoxBento and see YouTube receive it. Show that the private YouTube key
   never appears on the page or in the recording. Test Disconnect and explain the
   shared-account revocation behavior. Record who ran it and the outcome. The spike
   proved API fields exist; it did not prove this application's complete journey.
5. **Finish the YouTube caption controls (006 part 2).** Reuse the merged delivery
   engine. Let the owner turn captions off or choose the original language or one
   enabled translation. Save the choice, check the broadcast's caption settings,
   show whether text is being delivered, and stop delivery when the connection or
   broadcast is removed, the event is deleted, or the feature is turned off. Make
   it recover after restart without needing an owner to keep the room page open.
6. **Prove captions inside YouTube (006).** Speak into an unlisted broadcast, enable
   its CC button and check actual text, including a selected translation. Measure
   the delay. Show clear guidance for unsupported broadcast settings. YouTube
   caption setup must happen before going live unless that broadcast already had
   HTTP-post captions enabled. Verify that failures do not slow VoxBento captions.
7. **Finish the single guided room-page flow (007/#751).** Ask how the organiser
   streams, show just that route, and reuse the existing mode/key/destination
   controls. Fix or separate #1125. Test all three routes, source-switch warnings,
   permission failures, reloads and keyboard navigation. Include the listener link
   for translated speech and the explicit YouTube outage warning for forwarding.
8. **Prepare the public rollout and final rehearsal.** A named operator registers
   the deployment's Google client and completes the applicable Google verification
   before public use. Record the final caption-language/default decision, operator
   setup and supported-tool list. Walk through the full journey for each route,
   including reconnect, stopping, revoking keys and recovery. Then assess #1087 and
   #751 against their remaining acceptance criteria; merged parts alone do not
   close either issue.

Steps 1 and 2 can proceed independently. Step 3 builds on the final backend; step 4
validates the combined account work before it is called complete. Plan 006's control
integration needs plan 005; the complete plan 007 guide needs 003–006. Operator
registration and contributor preparation can proceed while those fixes are made.

Last recorded contributors: @ManojLamani on 005, @hazelmayank on spike evidence and
006, @samc4e on 007. These are existing claims, not new assignments. Maintainers
coordinate test access and any unclaimed rollout work through #1087.

## How contributors should use these files

1. Read the plain-English summary and current completion steps at the top of the
   relevant plan. The technical sections were originally written against
   `250aefde` on 1 November 2026 and include historical code excerpts.
2. Run its drift check and compare with current dev and the linked PR head.
   Completed plans are references for maintenance; do not implement them again.
   Refresh stale excerpts before changing pending work.
3. Check existing claims on #1087, then comment with the plan number when taking
   new work. Preserve the distinction between code merged and rehearsal complete.
4. Follow the current completion steps and verification requirements. Report a
   STOP condition rather than silently changing the design.
5. Update this index in the same PR when delivery status changes. Mark a plan DONE
   only after both its implementation and required real-world checks are complete.

Status values: TODO | IN PROGRESS | DONE | BLOCKED (reason) | REJECTED (rationale).
An open PR with changes requested is IN PROGRESS. A merged engine with its controls
still missing is also IN PROGRESS.

## Boundaries to preserve

- Sound reaches VoxBento from an explicit shared tab or the organiser's own feed;
  the server never downloads or embeds YouTube media to extract audio.
- One selected source owns a room's sound at a time. Switching sources is explicit.
- Private Google credentials, caption-posting addresses and YouTube stream keys
  stay protected and out of pages, logs, recordings and browser storage.
- Newly created **VoxBento** encoder keys can be shown once for intentional copying;
  a later status request must not return them. The **YouTube** key read from Google
  stays server-side.
- Independent extra output and forwarding have different outage consequences.
- SRT and the broader gateway-hardening work in #750 are outside these plans.

### Considered and rejected

- Downloading or scraping a pasted YouTube watch link on the server.
- Having the floor bot open a YouTube page in a hidden browser.
- Embedding YouTube in VoxBento and extracting the player's audio.
- Muting an embedded YouTube player and replacing its audio with VoxBento speech.
- Creating/rebinding the organiser's YouTube stream without an explicit product
  decision. Reading the already-bound broadcast settings is sufficient here.

The [design brief](youtube-intake-design.md) records the original research and
technical rationale. The [measurement report](002-results.md) distinguishes
observed results from pending tests.
