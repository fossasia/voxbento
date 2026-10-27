"""An event's team in one place (#757 follow-up): everyone with a role, and invite links.

Event owners and Admins add and remove people here instead of on the event,
room and booth pages. Which roles can be given where comes from portal.roles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from portal.booth_identity import make_booth_id
from portal.config import settings
from portal.database import (
    create_invite_token,
    create_user,
    get_booth_by_id,
    get_user_by_email,
    list_booths_for_event,
    list_memberships_for_event,
    list_rooms_for_event,
    set_booth_membership,
    set_event_membership,
    set_room_membership,
)
from portal.models import (
    BoothMembership,
    DBBooth,
    Event,
    EventMembership,
    InviteToken,
    Room,
    RoomMembership,
    User,
    utc_now,
)
from portal.notifications import queue_notification
from portal.roles import (
    BOOTH_ROLES,
    EVENT_OWNER,
    EVENT_ROLES,
    INTERPRETER,
    ROLE_RANK,
    ROLES,
    ROOM_COORDINATOR,
    ROOM_ROLES,
    assignment_error,
    role_label,
)
from portal.websockets.manager import manager
from portal.websockets.revocation import revoke_membership_push

# What each role means, shown next to the role picker.
ROLE_HELP = {
    EVENT_OWNER: "Runs the event: rooms, booths, team, keys and settings.",
    ROOM_COORDINATOR: "Runs the live session from Mission Control: chooses the active interpreter, starts transcription.",
    INTERPRETER: "Interprets in a booth: goes live and uses the booth chat.",
}

LEVELS = ("event", "room", "booth")
MAX_INVITE_HOURS = 24 * 90


class TeamError(Exception):
    """A team change that was refused, with a message for the page."""


@dataclass(frozen=True)
class Place:
    """Where a role applies: the whole event, one room or one booth."""

    level: str
    target_id: int
    label: str

    @property
    def key(self) -> str:
        return f"{self.level}:{self.target_id}"


@dataclass(frozen=True)
class Assignment:
    level: str
    membership_id: int
    role: str
    place: Place
    since: datetime | None

    @property
    def label(self) -> str:
        if self.level == "event":
            return role_label(self.role)
        return f"{role_label(self.role)} · {self.place.label}"


@dataclass
class TeamMember:
    user: User
    assignments: list[Assignment] = field(default_factory=list)

    @property
    def roles(self) -> set[str]:
        return {a.role for a in self.assignments}

    @property
    def invitation_pending(self) -> bool:
        """They haven't signed in yet (account created when they were added)."""
        return not self.user.email_verified and not self.user.password_hash

    @property
    def sort_key(self) -> tuple:
        top = max((ROLE_RANK.get(r, 0) for r in self.roles), default=0)
        return (-top, self.user.display_name.casefold(), self.user.email)


@dataclass(frozen=True)
class InviteLink:
    token: InviteToken
    booth_label: str

    @property
    def role_label(self) -> str:
        return role_label(self.token.role)


@dataclass
class Team:
    members: list[TeamMember]
    places: dict[str, Place]
    room_places: list[Place]
    booth_places: list[Place]
    invite_links: list[InviteLink]

    def count(self, role: str) -> int:
        return sum(1 for m in self.members if role in m.roles)


def booth_label(booth: DBBooth) -> str:
    room = booth.room.display_name if booth.room else ""
    return " · ".join(part for part in (room, booth.language_name) if part)


async def load_team(session: AsyncSession, event_id: int) -> Team:
    rooms = await list_rooms_for_event(session, event_id)
    booths = await list_booths_for_event(session, event_id)
    room_places = [Place("room", r.id, r.display_name) for r in sorted(rooms, key=lambda r: r.display_name.casefold())]
    booth_places = [Place("booth", b.id, booth_label(b)) for b in sorted(booths, key=lambda b: booth_label(b).casefold())]
    places = {p.key: p for p in (Place("event", event_id, "Whole event"), *room_places, *booth_places)}

    members: dict[int, TeamMember] = {}

    def add(user: User, level: str, membership_id: int, role: str, target_id: int, since: datetime | None) -> None:
        place = places.get(f"{level}:{target_id}")
        if place is None:
            return
        members.setdefault(user.id, TeamMember(user)).assignments.append(
            Assignment(level, membership_id, role, place, since)
        )

    for m in await list_memberships_for_event(session, event_id):
        if m.user is not None:
            add(m.user, "event", m.id, m.role, event_id, m.created_at)
    room_ids = [p.target_id for p in room_places]
    if room_ids:
        rms = (
            await session.execute(
                select(RoomMembership).options(joinedload(RoomMembership.user)).where(RoomMembership.room_id.in_(room_ids))
            )
        ).scalars().all()
        for m in rms:
            add(m.user, "room", m.id, m.role, m.room_id, m.created_at)
    booth_ids = [p.target_id for p in booth_places]
    if booth_ids:
        bms = (
            await session.execute(
                select(BoothMembership).options(joinedload(BoothMembership.user)).where(BoothMembership.booth_id.in_(booth_ids))
            )
        ).scalars().all()
        for m in bms:
            add(m.user, "booth", m.id, m.role, m.booth_id, m.created_at)

    for member in members.values():
        member.assignments.sort(key=lambda a: (-ROLE_RANK.get(a.role, 0), LEVELS.index(a.level), a.place.label))

    links: list[InviteLink] = []
    if booth_ids:
        tokens = (
            await session.execute(
                select(InviteToken)
                .where(InviteToken.booth_id.in_(booth_ids), InviteToken.used_at.is_(None))
                .order_by(InviteToken.created_at.desc())
            )
        ).scalars().all()
        labels = {p.target_id: p.label for p in booth_places}
        links = [InviteLink(t, labels.get(t.booth_id, "")) for t in tokens if not t.is_expired]

    return Team(
        members=sorted(members.values(), key=lambda m: m.sort_key),
        places=places,
        room_places=room_places,
        booth_places=booth_places,
        invite_links=links,
    )


def parse_place(value: str, team: Team) -> Place:
    place = team.places.get(value or "")
    if place is None:
        raise TeamError("Choose where this role applies.")
    return place


def check_assignment(role: str, place: Place) -> None:
    if error := assignment_error(role, place.level):
        raise TeamError(error)


async def add_member(session: AsyncSession, *, event_id: int, email: str, role: str, place: Place) -> tuple[User, bool]:
    """Give *role* at *place* to the person with *email*; returns (user, newly_added)."""
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1] or len(email) > 320:
        raise TeamError("Enter a valid email address.")
    check_assignment(role, place)

    user = await get_user_by_email(session, email)
    if user is None:
        user = await create_user(session, email=email, display_name=email.split("@")[0], email_verified=False)
    existing = await find_assignment(session, user.id, place)
    if existing is not None and existing.role == role:
        return user, False
    if existing is not None and place.level == "event" and existing.role == EVENT_OWNER:
        await ensure_another_owner(session, event_id)
    if place.level == "event":
        await set_event_membership(session, user_id=user.id, event_id=event_id, role=role)
    elif place.level == "room":
        await set_room_membership(session, user_id=user.id, room_id=place.target_id, role=role)
    else:
        await set_booth_membership(session, user_id=user.id, booth_id=place.target_id, role=role)
    if existing is not None:
        await revoke_membership_push(session, user_id=user.id, level=place.level, target_id=place.target_id)
    return user, True


async def find_assignment(session: AsyncSession, user_id: int, place: Place):
    model, column = {
        "event": (EventMembership, EventMembership.event_id),
        "room": (RoomMembership, RoomMembership.room_id),
        "booth": (BoothMembership, BoothMembership.booth_id),
    }[place.level]
    stmt = select(model).where(model.user_id == user_id, column == place.target_id)
    return (await session.execute(stmt)).scalars().first()


async def remove_assignment(session: AsyncSession, *, event_id: int, level: str, membership_id: int) -> str:
    """Remove one role from someone in this event; returns their email. Never the last Event owner."""
    model = {"event": EventMembership, "room": RoomMembership, "booth": BoothMembership}.get(level)
    if model is None:
        raise TeamError("Unknown role assignment.")
    membership = await session.get(model, membership_id)
    if membership is None or not await belongs_to_event(session, level, membership, event_id):
        raise TeamError("That person is no longer on this team.")
    if level == "event" and membership.role == EVENT_OWNER:
        await ensure_another_owner(session, event_id)
    user = await session.get(User, membership.user_id)
    user_id = membership.user_id
    target_id = (
        membership.event_id
        if level == "event"
        else (membership.room_id if level == "room" else membership.booth_id)
    )
    where = await _place_name(session, level, membership, event_id)
    await session.delete(membership)
    await session.flush()
    await revoke_membership_push(session, user_id=user_id, level=level, target_id=target_id)
    if user is not None:
        await queue_notification(
            session,
            "role_removed",
            user.email,
            {"context_name": where, "login_url": f"{settings.public_base_url.rstrip('/')}/workspace/"},
            idempotency_key=f"role-removed:{level}:{membership_id}",
        )
    return user.email if user else ""


async def _place_name(session: AsyncSession, level: str, membership, event_id: int) -> str:
    """Where a role applied, in words: "Summit", "Main Hall at Summit" or "Main Hall · Deutsch at Summit"."""
    event = await session.get(Event, event_id)
    event_name = event.display_name if event else "the event"
    if level == "room":
        room = await session.get(Room, membership.room_id)
        return f"{room.display_name} at {event_name}" if room else event_name
    if level == "booth":
        booth = await session.get(DBBooth, membership.booth_id)
        if booth is not None:
            room = await session.get(Room, booth.room_id)
            room_name = f"{room.display_name} · " if room else ""
            return f"{room_name}{booth.language_name} at {event_name}"
    return event_name


async def ensure_another_owner(session: AsyncSession, event_id: int) -> None:
    """Refuse a change that would leave the event without an Event owner."""
    owners = await session.scalar(
        select(func.count(EventMembership.id)).where(
            EventMembership.event_id == event_id, EventMembership.role == EVENT_OWNER
        )
    )
    if (owners or 0) <= 1:
        raise TeamError("An event needs at least one Event owner. Add another owner first.")


async def belongs_to_event(session: AsyncSession, level: str, membership, event_id: int) -> bool:
    if level == "event":
        return membership.event_id == event_id
    if level == "room":
        room = await session.get(Room, membership.room_id)
        return room is not None and room.event_id == event_id
    booth = await session.get(DBBooth, membership.booth_id)
    return booth is not None and booth.event_id == event_id


async def create_invite_link(
    session: AsyncSession, *, team: Team, booth_key: str, role: str, label: str, expires_hours: str
) -> InviteToken:
    place = parse_place(booth_key, team)
    if place.level != "booth":
        raise TeamError("Invite links are for a booth. Choose a booth.")
    check_assignment(role, place)
    expires_at = None
    if expires_hours.strip():
        try:
            hours = int(expires_hours)
        except ValueError:
            raise TeamError("Enter the expiry in whole hours.") from None
        if not 1 <= hours <= MAX_INVITE_HOURS:
            raise TeamError(f"Links can last between 1 and {MAX_INVITE_HOURS} hours.")
        expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    return await create_invite_token(
        session, booth_id=place.target_id, role=role, label=label.strip()[:200], expires_at=expires_at
    )


async def revoke_invite_link(session: AsyncSession, *, team: Team, token: str) -> None:
    import asyncio

    link = next((link for link in team.invite_links if link.token.token == token), None)
    if link is None:
        raise TeamError("That invite link no longer exists.")
    tok = link.token

    now = utc_now()
    tok.revoked_at = now
    if tok.used_at is None:
        tok.used_at = now
    await session.flush()

    public_id = tok.public_id
    booth_id: str | None = None
    if tok.booth_id:
        booth = await get_booth_by_id(session, tok.booth_id)
        if booth and booth.event:
            booth_id = make_booth_id(booth.event.slug, booth.room_id, booth.language_code)

    try:
        from portal.auth import invalidate_token_revocation_cache

        invalidate_token_revocation_cache(public_id)
    except Exception:
        pass

    await asyncio.shield(manager.close_token_sessions(public_id, booth_id=booth_id))


ROLE_CHOICES = tuple(
    (role, role_label(role), ROLE_HELP[role], sorted(level for level, roles in
     (("event", EVENT_ROLES), ("room", ROOM_ROLES), ("booth", BOOTH_ROLES)) if role in roles))
    for role in ROLES
    if role in EVENT_ROLES
)
