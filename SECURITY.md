# Security Policy

## Reporting a vulnerability

Please **do not** report security vulnerabilities through public GitHub issues,
pull requests or discussions.

Instead, email the maintainers directly. Include as much of the following as you can:

- The affected component (for example the portal, booth WebSocket, MediaMTX auth hook,
  Program Stream Ingest or the YouTube relay) and the commit or branch you tested.
- Steps to reproduce, with a proof of concept if you have one.
- The impact: what an attacker can do and which role, if any, they need first.
- Any suggested fix or mitigation.

We aim to acknowledge reports within a few working days, keep you updated while we
investigate, and credit you in the fix unless you prefer to stay anonymous. Please give
us a reasonable chance to release a fix before disclosing the issue publicly.

## Supported versions

VoxBento has no versioned releases yet. Security fixes land on `dev` and are then
promoted to `main`. Only the latest `main` is supported; self-hosted deployments should
track it.

## Scope

In scope: the code and configuration in this repository, including

- Authentication and sessions: JWTs, invite and listener tokens, password and magic-link sign-in.
- Authorization: the Admin / event owner / room coordinator / interpreter role checks,
  and access to events, rooms and booths.
- Media access: WHIP publish tickets, the `/internal/media-auth` hook, WHEP and caption access.
- Program Stream Ingest and the YouTube RTMPS relay.
- Storage of third-party API keys and stream credentials.
- The shipped `docker-compose*.yml` and `mediamtx.yml` defaults.

Out of scope:

- Vulnerabilities in upstream projects (Jitsi Meet, MediaMTX, FFmpeg, third-party
  transcription, translation or TTS providers). Please report those upstream; tell us
  too if VoxBento's use of them makes the issue worse.
- Deployments that ignore the hardening guide, for example keeping the default
  `SECRET_KEY` or Jitsi passwords, or exposing internal ports such as the MediaMTX
  Control API (9997) to the internet.
- Volumetric denial of service, social engineering and physical attacks.
- Automated scanner output without a demonstrated impact.

## Running VoxBento securely

Operators should follow the security guide in
[`website/docs/configuration/security.mdx`](website/docs/configuration/security.mdx)
before exposing an instance to the internet.
