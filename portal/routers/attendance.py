"""Interpreter attendance page and CSV export for event owners and Admins (#1215)."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select

from portal.attendance import (
    AttendanceRow,
    booth_key,
    days_with_visits,
    filter_rows,
    format_duration,
    rows_for_event,
    summarize,
)
from portal.auth import get_access_flags, require_event_owner
from portal.database import get_session, list_booths_for_event, list_rooms_for_event
from portal.globals import booths
from portal.models import Event, InterpreterAttendance, InviteToken, User
from portal.room_setup import language_name
from portal.routers.admin import templates

router = APIRouter()
templates.env.filters["duration"] = format_duration

_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_text(value: str) -> str:
    """Prefix a leading formula character so spreadsheets treat the cell as text."""
    return "'" + value if value.lstrip().startswith(_CSV_FORMULA_PREFIXES) else value


@dataclass
class _EventAttendance:
    event: Event
    rows: list[AttendanceRow]
    users: dict[int, tuple[str, str]]
    invite_labels: dict[str, str]
    booth_names: dict[tuple[int | None, str], str]
    now: datetime


async def _load(event_id: int) -> _EventAttendance:
    async with get_session() as session:
        event = await session.get(Event, event_id)
        if event is None or event.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Event not found.")
        result = await session.execute(
            select(InterpreterAttendance).where(InterpreterAttendance.event_id == event_id)
        )
        db_rows = list(result.scalars())
        now = datetime.now(tz=timezone.utc)
        rows = rows_for_event(db_rows, booths.attendance.open_visits(event.slug), now)

        user_ids = {row.user_id for row in rows if row.user_id is not None}
        users: dict[int, tuple[str, str]] = {}
        if user_ids:
            found = await session.execute(select(User.id, User.display_name, User.email).where(User.id.in_(user_ids)))
            users = {uid: (name or email, email) for uid, name, email in found.all()}

        public_ids = {row.invite_public_id for row in rows if row.invite_public_id}
        invite_labels: dict[str, str] = {}
        if public_ids:
            found = await session.execute(
                select(InviteToken.public_id, InviteToken.label).where(InviteToken.public_id.in_(public_ids))
            )
            invite_labels = {pid: label for pid, label in found.all() if label}

        rooms = {room.id: room.display_name for room in await list_rooms_for_event(session, event_id, limit=None)}
        languages = {
            (booth.room_id, booth.language_code): booth.language_name
            for booth in await list_booths_for_event(session, event_id, limit=None)
        }

    booth_names: dict[tuple[int | None, str], str] = {}
    for row in rows:
        key = (row.room_id, row.language_code)
        if key not in booth_names:
            language = languages.get(key) or language_name(row.language_code) or row.language_code.upper()
            room = rooms.get(row.room_id) if row.room_id is not None else None
            booth_names[key] = f"{room} · {language}" if room else language
    return _EventAttendance(event, rows, users, invite_labels, booth_names, now)


# Set by attendance.js so days start at the viewer's midnight, not UTC's.
TZ_COOKIE = "vb_tz"


def _viewer_tz(request: Request) -> tzinfo:
    # The browser percent-encodes the value ("Asia%2FKolkata") and Starlette keeps it encoded.
    name = unquote(request.cookies.get(TZ_COOKIE, ""))
    if name and len(name) <= 64:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone.utc


@dataclass
class _Filters:
    day: date | None
    booth: str | None
    tz: tzinfo

    def query(self) -> str:
        parts = []
        if self.day:
            parts.append(f"day={self.day.isoformat()}")
        if self.booth:
            parts.append(f"booth={self.booth}")
        return ("?" + "&".join(parts)) if parts else ""


def _filters(request: Request, data: _EventAttendance, day: str | None, booth: str | None) -> _Filters:
    tz = _viewer_tz(request)
    chosen_day = None
    if day:
        try:
            chosen_day = date.fromisoformat(day)
        except ValueError:
            chosen_day = None
    booth_keys = {booth_key(*key) for key in data.booth_names}
    return _Filters(day=chosen_day, booth=booth if booth in booth_keys else None, tz=tz)


@router.get("/admin/events/{event_id}/attendance/", dependencies=[Depends(require_event_owner)])
async def event_attendance(request: Request, event_id: int, day: str | None = None, booth: str | None = None):
    data = await _load(event_id)
    chosen = _filters(request, data, day, booth)
    # Day tabs reflect the booth filter, and the booth list reflects the day, so every choice shows something.
    days = days_with_visits(filter_rows(data.rows, chosen.tz, booth=chosen.booth), chosen.tz)
    if chosen.day not in days:
        chosen.day = None
    rows = filter_rows(data.rows, chosen.tz, day=chosen.day, booth=chosen.booth)
    people = summarize(rows, data.now, users=data.users, invite_labels=data.invite_labels)
    booth_options = sorted(
        ((booth_key(*key), name) for key, name in data.booth_names.items()),
        key=lambda option: option[1].casefold(),
    )
    admin_flags = await get_access_flags(request, event_id=event_id)
    return templates.TemplateResponse(
        request=request,
        name="admin/attendance.html",
        context={
            "event": data.event,
            "people": people,
            "has_any": bool(data.rows),
            "booth_names": data.booth_names,
            "booth_options": booth_options,
            "days": days,
            "today": data.now.astimezone(chosen.tz).date(),
            "filters": chosen,
            "here_now": sum(1 for person in people if person.is_here),
            "total_present": sum(person.present_seconds for person in people),
            "total_interpreting": sum(person.interpreting_seconds for person in people),
            **admin_flags,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/admin/events/{event_id}/attendance/export", dependencies=[Depends(require_event_owner)])
async def event_attendance_export(
    request: Request, event_id: int, day: str | None = None, booth: str | None = None
) -> Response:
    """Every visit as a CSV row, in UTC, for payroll and reporting. Honours the page's filters."""
    data = await _load(event_id)
    chosen = _filters(request, data, day, booth)
    rows = filter_rows(data.rows, chosen.tz, day=chosen.day, booth=chosen.booth)
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "Interpreter",
            "Account or invite link",
            "Booth",
            "Joined (UTC)",
            "Left (UTC)",
            "Minutes in booth",
            "Minutes interpreting",
        )
    )
    for person in summarize(rows, data.now, users=data.users, invite_labels=data.invite_labels):
        for visit in sorted(person.visits, key=lambda v: v.joined_at):
            writer.writerow(
                (
                    _csv_text(person.name),
                    _csv_text(person.detail),
                    _csv_text(data.booth_names.get((visit.room_id, visit.language_code), visit.language_code)),
                    f"{visit.joined_at.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}",
                    f"{visit.left_at.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}" if visit.left_at else "",
                    f"{visit.present_seconds / 60:.1f}",
                    f"{visit.interpreting_seconds / 60:.1f}",
                )
            )
    suffix = f"-{chosen.day.isoformat()}" if chosen.day else ""
    filename = f"{data.event.slug}-interpreter-attendance{suffix}.csv"
    return Response(
        content="\ufeff" + output.getvalue(),  # BOM so spreadsheet apps read UTF-8 names correctly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )
