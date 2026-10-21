from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from portal import meeting_source
from portal.auth import require_event_owner
from portal.config import settings
from portal.database import get_event_by_id, get_room_by_id, get_session
from portal.program_ingest import room_lock

router = APIRouter()


class MeetingChange(BaseModel):
    action: Literal["connect", "disconnect"]
    meeting_url: str | None = Field(default=None, max_length=500)
    confirm_source_change: bool = False


@router.get("/admin/events/{event_id}/rooms/{room_id}/meeting-source", dependencies=[Depends(require_event_owner)])
async def meeting_status(event_id: int, room_id: int) -> JSONResponse:
    async with get_session() as session:
        room = await get_room_by_id(session, room_id)
        if not room or room.event_id != event_id:
            raise HTTPException(404, "Room not found")
        return JSONResponse(meeting_source.status(room), headers={"Cache-Control": "no-store"})


@router.post("/admin/events/{event_id}/rooms/{room_id}/meeting-source", dependencies=[Depends(require_event_owner)])
async def meeting_change(request: Request, event_id: int, room_id: int, change: MeetingChange) -> JSONResponse:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != settings.public_base_url.rstrip("/"):
        raise HTTPException(403, "Origin mismatch")
    stopped = True
    async with room_lock(room_id):
        async with get_session() as session:
            room = await get_room_by_id(session, room_id)
            event = await get_event_by_id(session, event_id)
            if not room or room.event_id != event_id or not event:
                raise HTTPException(404, "Room not found")
            if change.action == "connect":
                await meeting_source.connect(room, event, change.meeting_url, change.confirm_source_change)
            else:
                stopped = await meeting_source.disconnect(room, event)
            body = meeting_source.status(room)
    if not stopped:
        body["cleanup_pending"] = True
    return JSONResponse(body, status_code=200 if stopped else 202, headers={"Cache-Control": "no-store"})
