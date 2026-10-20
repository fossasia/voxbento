<p align="center">
  <img src="https://github.com/user-attachments/assets/4521a23a-f99c-445d-96ee-dd98a4b66d5d" alt="VoxBento" width="420" />
</p>

<p align="center">
  Browser-first, real-time interpretation for live events.
</p>

<p align="center">
  <a href="https://voxbento.com"><img src="https://img.shields.io/badge/live-voxbento.com-2ea44f" alt="Documentation"></a>
  <a href="https://docs.voxbento.com"><img src="https://img.shields.io/badge/docs-docs.voxbento.com-1f6feb" alt="Documentation"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-2ea44f" alt="License: Apache 2.0"></a>
</p>

VoxBento is a real-time interpretation platform for live events. Interpreters monitor the floor in Jitsi and publish translated audio from the browser, while attendees listen with low-latency WebRTC playback.

**Quick links:** [Documentation](https://docs.voxbento.com) · [Contributing](CONTRIBUTING.md) · [VoxBento Local](https://voxbento.com/local)

### Why VoxBento

- **Browser-first workflow:** No native install needed for interpreter booths.
- **Low-latency delivery:** WebRTC-based broadcast path for live interpretation.
- **Built for operations:** Role-based booth coordination for organizers and interpreters.

---

## Signed-in pages

Every signed-in page shares one header: the VoxBento logo with the current area
(**Admin**, **My events** or **Account**), that area's page links, and a user menu
in the top right with the person's name. The menu always lists **My events**,
**Account** and **Logout**, plus **Developer API** for users with an approved
developer app and **Admin** for Admins. **My events** (`/workspace/`) lists every
event the person has a role in, with a button to where they work. **Account**
(`/account`) holds the person's display name, hosting and developer access, and
sign-in settings (change, set or remove a password).
Admins manage accounts at **Admin → Users**: search and filter everyone, and turn
hosting access, Admin and Active on or off right from the list.

## Hosting access

Only people with hosting access can create events. A signed-in user without it
selects **Request access** on their Account page and fills in a short form
(organization, event name and dates, languages, optional message). The team inbox
in `HOSTING_REQUESTS_EMAIL` (default `voxbento.dev@gmail.com`) is emailed about
each new request. Admins review requests under **Admin → Hosting access → Requests**:
**Grant access** gives the person hosting access straight away, **Decline** asks for
a reason that is emailed to them. A person has at most one open request at a time.

---

## Setup

For detailed API documentation, environment variables, and configuration, visit [docs.voxbento.com](https://docs.voxbento.com). Native development setup details can also be found in [CONTRIBUTING.md](CONTRIBUTING.md).

---

## VoxBento Local (Desktop App)

VoxBento Local is our sovereign, 100% on-device AI meeting intelligence and interpretation desktop console available for macOS (Apple Silicon & Intel), Windows, and Linux.

- **Download**: Visit [`/local`](https://voxbento.org/local) for platform-detected desktop packages (.dmg, .exe, .deb, .AppImage).
- **Source Code & Releases**: Built at [github.com/ArnavBallinCode/voxa](https://github.com/ArnavBallinCode/voxa).
- **Air-Gapped & Sovereign**: Whisper live transcription and Qwen 3.5 structured meeting minutes run completely on-device via Apple Metal and NVIDIA CUDA acceleration.
