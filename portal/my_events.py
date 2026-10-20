"""Data for "My events": every event a person has a role in, once per event (#766)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from portal.database import (
    list_all_booths_for_events,
    list_booth_memberships_for_user,
    list_memberships_for_user,
    list_room_memberships_for_user,
)
from portal.models import Event

ROLE_LABELS = {
    "event_owner": "Event owner",
    "room_coordinator": "Room coordinator",
    "interpreter": "Interpreter",
}
_ROLE_ORDER = {"event_owner": 0, "room_coordinator": 1, "interpreter": 2}

MANAGE_EVENT = "Manage event"
OPEN_MISSION_CONTROL = "Open Mission Control"
ENTER_BOOTH = "Enter booth"


@dataclass(frozen=True)
class EventAction:
    label: str
    href: str
    detail: str = ""


@dataclass
class MyEvent:
    event: Event
    booth_count: int = 0
    languages: tuple[str, ...] = ()
    _roles: list[tuple[int, str]] = field(default_factory=list)
    _actions: list[EventAction] = field(default_factory=list)

    @property
    def roles(self) -> list[str]:
        return [label for _, label in sorted(set(self._roles))]

    @property
    def actions(self) -> list[EventAction]:
        """One button per destination; booth buttons name the booth when there are several."""
        unique: dict[str, EventAction] = {}
        for action in self._actions:
            unique.setdefault(action.href, action)
        actions = list(unique.values())
        booths = [a for a in actions if a.label == ENTER_BOOTH and a.detail]
        if len(booths) > 1:
            actions = [EventAction(f"{a.label} · {a.detail}", a.href) if a in booths else a for a in actions]
        order = {MANAGE_EVENT: 0, OPEN_MISSION_CONTROL: 1}
        return sorted(actions, key=lambda a: order.get(a.label, 2))

    def add_role(self, role: str, *where: str) -> None:
        label = " · ".join((ROLE_LABELS.get(role, role.replace("_", " ").capitalize()), *[w for w in where if w]))
        self._roles.append((_ROLE_ORDER.get(role, 9), label))

    def add_action(self, action: EventAction) -> None:
        self._actions.append(action)


def role_action(
    role: str, event: Event, *, room_id: int | None = None, language_code: str = "", detail: str = ""
) -> EventAction:
    """Where a person with this role works in the event."""
    if role == "event_owner":
        return EventAction(MANAGE_EVENT, f"/workspace/events/{event.id}/")
    if role == "room_coordinator":
        return EventAction(OPEN_MISSION_CONTROL, f"/mission-control/{event.slug}/")
    if room_id is not None and language_code:
        return EventAction(ENTER_BOOTH, f"/interpreter/{event.slug}/{room_id}/{language_code}", detail)
    # An event-wide interpreter picks a booth in the interpreter lobby.
    return EventAction(ENTER_BOOTH, "/interpreter")


async def list_my_events(session: AsyncSession, user_id: int) -> list[MyEvent]:
    """Every event the user has a role in, newest first, with each role in plain language."""
    by_event: dict[int, MyEvent] = {}

    def entry(event: Event | None) -> MyEvent | None:
        if event is None or event.deleted_at is not None:
            return None
        return by_event.setdefault(event.id, MyEvent(event=event))

    for m in await list_memberships_for_user(session, user_id):
        my_event = entry(m.event)
        if my_event is not None:
            my_event.add_role(m.role)
            my_event.add_action(role_action(m.role, m.event))

    for m in await list_room_memberships_for_user(session, user_id):
        room = m.room
        my_event = entry(room.event if room else None)
        if my_event is not None:
            my_event.add_role(m.role, room.display_name)
            my_event.add_action(role_action(m.role, room.event))

    for m in await list_booth_memberships_for_user(session, user_id):
        booth = m.booth
        my_event = entry(booth.event if booth else None)
        if my_event is not None:
            room_name = booth.room.display_name if booth.room else ""
            my_event.add_role(m.role, room_name, booth.language_name)
            my_event.add_action(
                role_action(
                    m.role,
                    booth.event,
                    room_id=booth.room_id,
                    language_code=booth.language_code,
                    detail=booth.language_name,
                )
            )

    booths_by_event = await list_all_booths_for_events(session, list(by_event))
    for event_id, my_event in by_event.items():
        event_booths = booths_by_event.get(event_id, [])
        my_event.booth_count = len(event_booths)
        my_event.languages = tuple(sorted({b.language_name for b in event_booths}, key=str.casefold))

    oldest = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(by_event.values(), key=lambda e: as_utc(e.event.created_at) or oldest, reverse=True)


def as_utc(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes; treat them as UTC so they compare.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
