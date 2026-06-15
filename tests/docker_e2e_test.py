#!/usr/bin/env python
"""Docker end-to-end database test.

This script runs INSIDE the portal container against the real SQLite
database to verify all CRUD operations, relationships, cascade deletes,
token lifecycle, and persistence.

Usage (from host):
    docker compose exec portal uv run python tests/docker_e2e_test.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

# Ensure portal package is importable
sys.path.insert(0, "/app")


async def main() -> None:
    # Local import required to avoid circular dependency
    from portal.database import (
        configure,
        create_booth,
        create_event,
        create_invite_token,
        create_room,
        delete_booth,
        delete_event,
        delete_room,
        dispose,
        get_booth_by_id,
        get_event_by_id,
        get_event_by_slug,
        get_invite_token,
        get_room_by_id,
        get_session,
        init_db,
        list_booths_for_event,
        list_booths_for_room,
        list_events,
        list_rooms_for_event,
        list_tokens_for_booth,
        redeem_invite_token,
    )

    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = ""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  ✅ {name}")
        else:
            failed += 1
            print(f"  ❌ {name}: {detail}")

    print("\n" + "=" * 60)
    print("  DOCKER END-TO-END DATABASE TEST")
    print("=" * 60)

    # ── 1. Event creation ────────────────────────────────────────
    print("\n── 1. Creating events ──")
    async with get_session() as s:
        pycon = await create_event(s, slug="pycon2026", display_name="PyCon US 2026")
        check("Event pycon2026 created", pycon.id is not None)
        check("Event slug correct", pycon.slug == "pycon2026")
        check("Event display_name correct", pycon.display_name == "PyCon US 2026")
        check("Event created_at set", pycon.created_at is not None)

    async with get_session() as s:
        fossasia = await create_event(s, slug="fossasia2026", display_name="FOSSASIA Summit 2026")
        check("Event fossasia2026 created", fossasia.id is not None)

    async with get_session() as s:
        devconf = await create_event(s, slug="devconf-cz-2026", display_name="DevConf.CZ 2026")
        check("Event devconf-cz-2026 created", devconf.id is not None)

    # ── 2. Event listing + lookup ────────────────────────────────
    print("\n── 2. Event listing & lookup ──")
    async with get_session() as s:
        events = await list_events(s)
        check("3 events listed", len(events) == 3, f"got {len(events)}")

        found = await get_event_by_slug(s, "pycon2026")
        check("get_event_by_slug works", found is not None and found.slug == "pycon2026")

        found_id = await get_event_by_id(s, pycon.id)
        check("get_event_by_id works", found_id is not None and found_id.id == pycon.id)

        missing = await get_event_by_slug(s, "nonexistent")
        check("Missing slug returns None", missing is None)

    # ── 3. Event slug validation ─────────────────────────────────
    print("\n── 3. Event slug validation ──")
    async with get_session() as s:
        try:
            await create_event(s, slug="bad_slug", display_name="Bad")
            check("Underscore slug rejected", False, "no error raised")
        except ValueError:
            check("Underscore slug rejected", True)

        try:
            await create_event(s, slug="", display_name="Empty")
            check("Empty slug rejected", False, "no error raised")
        except ValueError:
            check("Empty slug rejected", True)

    # Uppercase slugs get normalised to lowercase (not rejected)
    async with get_session() as s:
        caps_event = await create_event(s, slug="HAS-CAPS", display_name="Caps")
        check("Uppercase slug normalised to lowercase", caps_event.slug == "has-caps", f"got {caps_event.slug}")
        caps_id = caps_event.id
    async with get_session() as s:
        await delete_event(s, caps_id)
        check("Cleanup: uppercase event deleted", True)

    # ── 4. Room creation ─────────────────────────────────────────
    print("\n── 4. Creating rooms ──")
    async with get_session() as s:
        pycon_main = await create_room(s, event_id=pycon.id, display_name="Main Hall")
        check("Room Main Hall created", pycon_main.id is not None)
        check("Room event_id correct", pycon_main.event_id == pycon.id)

    async with get_session() as s:
        pycon_workshop = await create_room(s, event_id=pycon.id, display_name="Workshop Room")
        check("Room Workshop Room created", pycon_workshop.id is not None)

    async with get_session() as s:
        pycon_lightning = await create_room(s, event_id=pycon.id, display_name="Lightning Talks")
        check("Room Lightning Talks created", pycon_lightning.id is not None)

    async with get_session() as s:
        foss_keynote = await create_room(
            s,
            event_id=fossasia.id,
            display_name="Keynote Stage",
            eventyay_room_id="eventyay-room-42",
        )
        check("Room with eventyay_room_id created", foss_keynote.eventyay_room_id == "eventyay-room-42")

    async with get_session() as s:
        foss_track_a = await create_room(s, event_id=fossasia.id, display_name="Track A")
        check("Room Track A created", foss_track_a.id is not None)

    async with get_session() as s:
        dev_main = await create_room(s, event_id=devconf.id, display_name="Grand Ballroom")
        check("Room Grand Ballroom created", dev_main.id is not None)

    # ── 5. Room listing ──────────────────────────────────────────
    print("\n── 5. Room listing ──")
    async with get_session() as s:
        pycon_rooms = await list_rooms_for_event(s, pycon.id)
        check("PyCon has 3 rooms", len(pycon_rooms) == 3, f"got {len(pycon_rooms)}")

        foss_rooms = await list_rooms_for_event(s, fossasia.id)
        check("FOSSASIA has 2 rooms", len(foss_rooms) == 2, f"got {len(foss_rooms)}")

        dev_rooms = await list_rooms_for_event(s, devconf.id)
        check("DevConf has 1 room", len(dev_rooms) == 1, f"got {len(dev_rooms)}")

    # ── 6. Booth creation ────────────────────────────────────────
    print("\n── 6. Creating booths ──")
    booth_ids = {}
    languages = [
        (pycon.id, pycon_main.id, "en", "English"),
        (pycon.id, pycon_main.id, "fr", "French"),
        (pycon.id, pycon_main.id, "es", "Spanish"),
        (pycon.id, pycon_workshop.id, "de", "German"),
        (pycon.id, pycon_workshop.id, "ja", "Japanese"),
        (pycon.id, pycon_lightning.id, "zh", "Chinese"),
        (fossasia.id, foss_keynote.id, "en", "English"),
        (fossasia.id, foss_keynote.id, "zh", "Chinese"),
        (fossasia.id, foss_track_a.id, "ko", "Korean"),
        (devconf.id, dev_main.id, "cs", "Czech"),
        (devconf.id, dev_main.id, "en", "English"),
    ]
    for ev_id, room_id, lang_code, lang_name in languages:
        async with get_session() as s:
            booth = await create_booth(
                s,
                event_id=ev_id,
                room_id=room_id,
                language_code=lang_code,
                language_name=lang_name,
            )
            key = f"{ev_id}-{lang_code}"
            booth_ids[key] = booth.id
            check(f"Booth {lang_code} in event {ev_id} created", booth.id is not None)

    # ── 7. Booth listing ─────────────────────────────────────────
    print("\n── 7. Booth listing ──")
    async with get_session() as s:
        pycon_booths = await list_booths_for_event(s, pycon.id)
        check("PyCon has 6 booths", len(pycon_booths) == 6, f"got {len(pycon_booths)}")

        foss_booths = await list_booths_for_event(s, fossasia.id)
        check("FOSSASIA has 3 booths", len(foss_booths) == 3, f"got {len(foss_booths)}")

        dev_booths = await list_booths_for_event(s, devconf.id)
        check("DevConf has 2 booths", len(dev_booths) == 2, f"got {len(dev_booths)}")

        main_booths = await list_booths_for_room(s, pycon_main.id)
        check("Main Hall has 3 booths", len(main_booths) == 3, f"got {len(main_booths)}")

        ws_booths = await list_booths_for_room(s, pycon_workshop.id)
        check("Workshop Room has 2 booths", len(ws_booths) == 2, f"got {len(ws_booths)}")

    # ── 8. MediaMTX path derivation ──────────────────────────────
    print("\n── 8. MediaMTX path derivation ──")
    async with get_session() as s:
        # Get pycon english booth
        pycon_en_id = booth_ids[f"{pycon.id}-en"]
        booth_en = await get_booth_by_id(s, pycon_en_id)
        check("mediamtx_path = pycon2026/en", booth_en.mediamtx_path == "pycon2026/en", f"got {booth_en.mediamtx_path}")

        foss_zh_id = booth_ids[f"{fossasia.id}-zh"]
        booth_zh = await get_booth_by_id(s, foss_zh_id)
        check(
            "mediamtx_path = fossasia2026/zh",
            booth_zh.mediamtx_path == "fossasia2026/zh",
            f"got {booth_zh.mediamtx_path}",
        )

        dev_cs_id = booth_ids[f"{devconf.id}-cs"]
        booth_cs = await get_booth_by_id(s, dev_cs_id)
        check(
            "mediamtx_path = devconf-cz-2026/cs",
            booth_cs.mediamtx_path == "devconf-cz-2026/cs",
            f"got {booth_cs.mediamtx_path}",
        )

    # ── 9. Booth language validation ─────────────────────────────
    print("\n── 9. Booth language validation ──")
    async with get_session() as s:
        try:
            await create_booth(
                s,
                event_id=pycon.id,
                room_id=pycon_main.id,
                language_code="xyz",
                language_name="Bad",
            )
            check("Invalid language code rejected", False, "no error raised")
        except ValueError:
            check("Invalid language code rejected", True)

    # ── 10. Invite token creation ────────────────────────────────
    print("\n── 10. Creating invite tokens ──")
    token_values = {}
    pycon_en_id = booth_ids[f"{pycon.id}-en"]
    foss_zh_id = booth_ids[f"{fossasia.id}-zh"]

    # Multiple tokens for same booth, different roles
    for role, label, booth_id, created_by in [
        ("interpreter", "Alice Interpreter", pycon_en_id, "admin@pycon.org"),
        ("room_coordinator", "Bob Coordinator", pycon_en_id, "admin@pycon.org"),
        ("Charlie Listener", pycon_en_id, None),
        ("event_owner", "Dave Admin", pycon_en_id, "super@pycon.org"),
        ("super_admin", "Eve Super", foss_zh_id, None),
        ("interpreter", "Fiona Interpreter", foss_zh_id, "admin@fossasia.org"),
    ]:
        async with get_session() as s:
            tok = await create_invite_token(
                s,
                booth_id=booth_id,
                role=role,
                label=label,
                created_by=created_by,
            )
            token_values[label] = tok.token
            check(f"Token {label} ({role}) created", len(tok.token) == 64)

    # Token with expiry
    async with get_session() as s:
        future = datetime.now(tz=timezone.utc) + timedelta(hours=24)
        tok_exp = await create_invite_token(
            s,
            booth_id=pycon_en_id,
            role="room_coordinator",
            label="Expiring token",
            expires_at=future,
        )
        token_values["Expiring token"] = tok_exp.token
        check("Token with expiry created", tok_exp.is_expired is False)

    # Already-expired token
    async with get_session() as s:
        past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        tok_past = await create_invite_token(
            s,
            booth_id=pycon_en_id,
            role="room_coordinator",
            label="Past token",
            expires_at=past,
        )
        token_values["Past token"] = tok_past.token
        check("Expired token is_expired=True", tok_past.is_expired is True)

    # ── 11. Token listing ────────────────────────────────────────
    print("\n── 11. Token listing ──")
    async with get_session() as s:
        en_tokens = await list_tokens_for_booth(s, pycon_en_id)
        check("PyCon EN booth has 6 tokens", len(en_tokens) == 6, f"got {len(en_tokens)}")

        zh_tokens = await list_tokens_for_booth(s, foss_zh_id)
        check("FOSSASIA ZH booth has 2 tokens", len(zh_tokens) == 2, f"got {len(zh_tokens)}")

    # ── 12. Token role validation ────────────────────────────────
    print("\n── 12. Token role validation ──")
    async with get_session() as s:
        try:
            await create_invite_token(s, booth_id=pycon_en_id, role="hacker")
            check("Invalid role rejected", False, "no error raised")
        except ValueError:
            check("Invalid role rejected", True)

    # ── 13. Token redemption lifecycle ───────────────────────────
    print("\n── 13. Token redemption lifecycle ──")
    # Redeem Alice's token
    async with get_session() as s:
        redeemed = await redeem_invite_token(s, token_values["Alice Interpreter"])
        check("Token redeemed successfully", redeemed is not None and redeemed.is_used is True)
        check("used_at set after redemption", redeemed.used_at is not None)

    # Try to redeem again → error
    async with get_session() as s:
        try:
            await redeem_invite_token(s, token_values["Alice Interpreter"])
            check("Double-redeem rejected", False, "no error raised")
        except ValueError as e:
            check("Double-redeem rejected", "already been used" in str(e))

    # Try to redeem expired token → error
    async with get_session() as s:
        try:
            await redeem_invite_token(s, token_values["Past token"])
            check("Expired token redemption rejected", False, "no error raised")
        except ValueError as e:
            check("Expired token redemption rejected", "expired" in str(e))

    # Redeem nonexistent token → None
    async with get_session() as s:
        result = await redeem_invite_token(s, "a" * 64)
        check("Nonexistent token returns None", result is None)

    # ── 14. Token joinedload (booth → event) ─────────────────────
    print("\n── 14. Token joinedload ──")
    async with get_session() as s:
        tok = await get_invite_token(s, token_values["Bob Coordinator"])
        check("Token booth loaded", tok.booth is not None)
        check("Token booth.event loaded", tok.booth.event is not None)
        check("Token booth.event.slug = pycon2026", tok.booth.event.slug == "pycon2026", f"got {tok.booth.event.slug}")

    # ── 15. Cascade delete: booth → tokens ───────────────────────
    print("\n── 15. Cascade deletes ──")
    # Create a throwaway booth with a token, then delete the booth
    async with get_session() as s:
        throwaway_booth = await create_booth(
            s,
            event_id=devconf.id,
            room_id=dev_main.id,
            language_code="de",
            language_name="German",
        )
        throwaway_tok = await create_invite_token(
            s,
            booth_id=throwaway_booth.id,
            role="room_coordinator",
        )
        tb_id = throwaway_booth.id
        tt_token = throwaway_tok.token

    async with get_session() as s:
        deleted = await delete_booth(s, tb_id)
        check("Booth deleted", deleted is True)

    async with get_session() as s:
        check("Cascade: token gone", await get_invite_token(s, tt_token) is None)

    # Cascade delete: room → booths → tokens
    async with get_session() as s:
        test_room = await create_room(s, event_id=devconf.id, display_name="Temp Room")
        test_booth = await create_booth(
            s,
            event_id=devconf.id,
            room_id=test_room.id,
            language_code="pl",
            language_name="Polish",
        )
        test_tok = await create_invite_token(
            s,
            booth_id=test_booth.id,
            role="interpreter",
        )
        tr_id = test_room.id
        tb2_id = test_booth.id
        tt2_token = test_tok.token

    async with get_session() as s:
        deleted = await delete_room(s, tr_id)
        check("Room deleted", deleted is True)

    async with get_session() as s:
        check("Cascade: booth gone", await get_booth_by_id(s, tb2_id) is None)
        check("Cascade: token gone", await get_invite_token(s, tt2_token) is None)

    # Cascade delete: event → rooms → booths → tokens
    # Delete devconf event (has 1 room left, 2 booths)
    async with get_session() as s:
        deleted = await delete_event(s, devconf.id)
        check("Event devconf deleted", deleted is True)

    async with get_session() as s:
        check("Cascade: room gone", await get_room_by_id(s, dev_main.id) is None)
        # devconf booth ids
        dev_cs_id = booth_ids[f"{devconf.id}-cs"]
        dev_en_id = booth_ids[f"{devconf.id}-en"]
        check("Cascade: booth cs gone", await get_booth_by_id(s, dev_cs_id) is None)
        check("Cascade: booth en gone", await get_booth_by_id(s, dev_en_id) is None)

    # ── 16. Delete nonexistent entity ────────────────────────────
    print("\n── 16. Delete nonexistent ──")
    async with get_session() as s:
        check("Delete missing event returns False", await delete_event(s, 99999) is False)
        check("Delete missing room returns False", await delete_room(s, 99999) is False)
        check("Delete missing booth returns False", await delete_booth(s, 99999) is False)

    # ── 17. Verify remaining data integrity ──────────────────────
    print("\n── 17. Final data integrity ──")
    async with get_session() as s:
        events = await list_events(s)
        check("2 events remain (pycon, fossasia)", len(events) == 2, f"got {len(events)}")

        pycon_rooms = await list_rooms_for_event(s, pycon.id)
        check("PyCon still has 3 rooms", len(pycon_rooms) == 3, f"got {len(pycon_rooms)}")

        pycon_booths = await list_booths_for_event(s, pycon.id)
        check("PyCon still has 6 booths", len(pycon_booths) == 6, f"got {len(pycon_booths)}")

        foss_rooms = await list_rooms_for_event(s, fossasia.id)
        check("FOSSASIA still has 2 rooms", len(foss_rooms) == 2, f"got {len(foss_rooms)}")

        foss_booths = await list_booths_for_event(s, fossasia.id)
        check("FOSSASIA still has 3 booths", len(foss_booths) == 3, f"got {len(foss_booths)}")

    # ── SUMMARY ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  RESULTS: {passed} passed, {failed} failed")
    print("=" * 60)

    if failed:
        sys.exit(1)
    print("\n  🎉 ALL TESTS PASSED — Database is fully operational!\n")


if __name__ == "__main__":
    asyncio.run(main())
