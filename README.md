# Voxbento

Voxbento is a real-time interpretation platform for live events. It provides a browser-first, zero-install experience for simultaneous interpreters, allowing them to monitor the main floor video via Jitsi and broadcast translated audio to attendees with low latency.

**Official Documentation:** [docs.voxbento.com](https://docs.voxbento.com)

---

## Setup

For detailed API documentation, environment variables, and configuration, visit [docs.voxbento.com](https://docs.voxbento.com). Native development setup details can also be found in [CONTRIBUTING.md](CONTRIBUTING.md).

---

## VoxBento Local (Desktop App)

VoxBento Local is our sovereign, 100% on-device AI meeting intelligence and interpretation desktop console available for macOS (Apple Silicon & Intel), Windows, and Linux.

- **Download**: Visit [`/local`](https://voxbento.org/local) for platform-detected desktop packages (.dmg, .exe, .deb, .AppImage).
- **Source Code & Releases**: Built at [github.com/ArnavBallinCode/voxa](https://github.com/ArnavBallinCode/voxa).
- **Air-Gapped & Sovereign**: Whisper live transcription and Qwen 3.5 structured meeting minutes run completely on-device via Apple Metal and NVIDIA CUDA acceleration.
# Meeting source

Event owners can connect a room's self-hosted Jitsi meeting from the room page:
confirm the meeting URL and click **Connect meeting**. VoxBento runs a managed,
listen-only agent, reports its lifecycle (starting, joined, receiving audio,
processing, reconnecting, needs attention, stopped), restarts it within a bounded
policy and starts floor captions, translation and TTS once audio arrives. It never
replaces program ingest without confirmation. Apply migration 027 and rebuild the
`floor-bot` image. See [workflow, recovery, troubleshooting and rehearsal](docs/meeting-source.md).


