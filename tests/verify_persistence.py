"""Persistence verification script — run after docker compose down/up."""

import asyncio
import sys

sys.path.insert(0, "/app")


async def check():
    # Local import required to avoid circular dependency
    from portal.database import (
        get_session,
        list_booths_for_event,
        list_events,
        list_rooms_for_event,
        list_tokens_for_booth,
    )

    print("=== PERSISTENCE TEST AFTER docker compose down/up ===")
    print()
    async with get_session() as s:
        events = await list_events(s)
        print(f"Events: {len(events)}")
        for e in events:
            print(f"  - {e.slug} ({e.display_name})")
            rooms = await list_rooms_for_event(s, e.id)
            print(f"    Rooms: {len(rooms)}")
            for r in rooms:
                print(f"      - {r.display_name} (eventyay_id={r.eventyay_room_id})")
            booths = await list_booths_for_event(s, e.id)
            print(f"    Booths: {len(booths)}")
            for b in booths:
                print(f"      - {b.language_code} ({b.language_name}) -> mediamtx: {b.mediamtx_path}")
                tokens = await list_tokens_for_booth(s, b.id)
                for t in tokens:
                    print(
                        f"        Token: {t.token[:12]}... role={t.role} used={t.is_used} expired={t.is_expired} label={t.label}"
                    )
    print()
    print("=== PERSISTENCE VERIFIED ===")


asyncio.run(check())
