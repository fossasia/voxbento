"""Event authorization for registered-user WebSocket sessions.

``user_event_authorized`` is exercised here rather than in
``test_fastapi_app.py`` because it drives the database directly. That module
runs its requests through a ``TestClient`` portal, and touching the shared
in-memory SQLite connection from a second event loop in the same module
deadlocks the run — the suite hung on CI for hours instead of failing. This
module starts no portal, so its fixture, its seeding and its assertions all
share one loop.
"""

from __future__ import annotations

import pytest


@pytest.fixture
async def seeded_db():
    """Configure the module-level engine and yield the seeded IDs.

    Returns ``(member_id, outsider_id)``: one user with an event membership and
    one with none.
    """
    from portal.database import (
        configure,
        create_event,
        create_user,
        dispose,
        get_session,
        init_db,
        set_event_membership,
    )

    configure("sqlite+aiosqlite://")
    await init_db()

    async with get_session() as session:
        member = await create_user(session, email="member@test.com", display_name="Member")
        outsider = await create_user(session, email="outsider@test.com", display_name="Outsider")
        event = await create_event(session, slug="scoped-event", display_name="Scoped")
        await create_event(session, slug="other-event", display_name="Other")
        await session.flush()
        await set_event_membership(session, user_id=member.id, event_id=event.id, role="interpreter")
        ids = (member.id, outsider.id)

    yield ids

    await dispose()


@pytest.mark.anyio
async def test_a_member_reaches_only_their_own_events_booths(seeded_db):
    """A user_token names a user but no event, so memberships decide access."""
    from portal.auth import user_event_authorized

    member_id, _ = seeded_db

    # Both booth shapes of the member's own event are allowed.
    assert await user_event_authorized(member_id, "scoped-event-1-fr")
    assert await user_event_authorized(member_id, "scoped-event-1-ai-fr")
    assert await user_event_authorized(member_id, "scoped-event-1-floor")

    # A different event is not, even though the user is a member somewhere.
    assert not await user_event_authorized(member_id, "other-event-1-fr")


@pytest.mark.anyio
async def test_a_user_without_any_membership_reaches_nothing(seeded_db):
    from portal.auth import user_event_authorized

    _, outsider_id = seeded_db

    assert not await user_event_authorized(outsider_id, "scoped-event-1-fr")
    assert not await user_event_authorized(outsider_id, "scoped-event-1-ai-fr")


@pytest.mark.anyio
async def test_unparseable_booth_ids_and_unknown_events_fail_closed(seeded_db):
    from portal.auth import user_event_authorized

    member_id, _ = seeded_db

    assert not await user_event_authorized(member_id, "not-a-booth")
    assert not await user_event_authorized(member_id, "no-such-event-1-fr")
    assert not await user_event_authorized("not-a-user-id", "scoped-event-1-fr")
