"""Rules for the Admin Users pages (#765): hosting access, Admin, active and deletion.

The users list toggles, the user page buttons and the older endpoints all go
through these functions, so the same safeguards apply everywhere:
an Admin can't demote, deactivate or delete themselves, and the platform always
keeps at least one active Admin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from portal.database import close_open_hosting_request, count_active_admins, get_user_by_id
from portal.models import HOSTING_REQUEST_GRANTED, TOKEN_TYPE_HOSTING_INVITE, AuthToken, User, utc_now

logger = logging.getLogger(__name__)

FIELDS = ("hosting", "admin", "active", "verified")

ERROR_MESSAGES = {
    "not_found": "This account no longer exists.",
    "invalid": "Unknown change.",
    "self_admin": "You can't change your own Admin status.",
    "self_active": "You can't deactivate your own account.",
    "self_verified": "You can't unverify your own email address.",
    "self_delete": "You can't delete your own account.",
    "last_admin": "VoxBento needs at least one active Admin.",
    "admin_hosting": "Admins can always create events.",
    "inactive": "Reactivate this account first.",
    "no_account": "This person hasn't finished setting up their account. Send them an invitation instead.",
    "confirm_email": "Type the account's email address exactly to confirm.",
}


class UserAdminError(Exception):
    def __init__(self, code: str, status_code: int = 400) -> None:
        super().__init__(ERROR_MESSAGES[code])
        self.code = code
        self.message = ERROR_MESSAGES[code]
        self.status_code = status_code


@dataclass(frozen=True)
class AccessChange:
    user_id: int
    email: str
    display_name: str
    field: str
    value: bool
    changed: bool

    @property
    def notice(self) -> str:
        return f"{self.field}_{'on' if self.value else 'off'}"

    @property
    def message(self) -> str:
        name = self.display_name
        return {
            "hosting_on": f"{name} can now create events.",
            "hosting_off": f"{name} can no longer create events. Their existing events are kept.",
            "admin_on": f"{name} is now an Admin.",
            "admin_off": f"{name} is no longer an Admin.",
            "active_on": f"{name}'s account was reactivated.",
            "active_off": f"{name}'s account was deactivated and signed out.",
            "verified_on": f"{name}'s email was marked as verified.",
            "verified_off": f"{name}'s email was marked as not verified.",
        }[self.notice]


async def is_last_active_admin(session: AsyncSession, user: User) -> bool:
    return bool(user.is_admin and user.is_active) and await count_active_admins(session) <= 1


async def delete_pending_hosting_invites(session: AsyncSession, user_id: int) -> None:
    tokens = (
        (
            await session.execute(
                select(AuthToken).where(
                    AuthToken.user_id == user_id,
                    AuthToken.token_type == TOKEN_TYPE_HOSTING_INVITE,
                    AuthToken.used_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for token in tokens:
        await session.delete(token)


async def set_user_access(
    session: AsyncSession, *, actor_id: int | None, user_id: int, field: str, value: bool
) -> AccessChange:
    """Turn hosting access, Admin or the account itself on or off."""
    if field not in FIELDS:
        raise UserAdminError("invalid")
    user = await get_user_by_id(session, user_id)
    if user is None:
        raise UserAdminError("not_found", 404)

    current = {
        "hosting": user.can_host,
        "admin": user.is_admin,
        "active": user.is_active,
        "verified": user.email_verified,
    }[field]
    changed = bool(current) != value

    if changed:
        if field == "hosting":
            if user.is_admin:
                raise UserAdminError("admin_hosting")
            if value and not user.is_active:
                raise UserAdminError("inactive")
            if value and not user.password_hash:
                raise UserAdminError("no_account")
            user.can_host = value
            user.hosting_revoked_at = None if value else utc_now()
            user.hosting_requested_at = None
            if value:
                await close_open_hosting_request(
                    session, user.id, status=HOSTING_REQUEST_GRANTED, decided_by_user_id=actor_id
                )
            await delete_pending_hosting_invites(session, user.id)
        elif field == "admin":
            if user.id == actor_id:
                raise UserAdminError("self_admin", 403)
            if value and not user.is_active:
                raise UserAdminError("inactive")
            if not value and await is_last_active_admin(session, user):
                raise UserAdminError("last_admin")
            user.is_admin = value
            if not value:
                user.sessions_valid_after = utc_now()
        elif field == "verified":
            if user.id == actor_id and not value:
                raise UserAdminError("self_verified", 403)
            if not value and await is_last_active_admin(session, user):
                raise UserAdminError("last_admin")
            user.email_verified = value
        else:
            if user.id == actor_id:
                raise UserAdminError("self_active", 403)
            if not value and await is_last_active_admin(session, user):
                raise UserAdminError("last_admin")
            user.is_active = value
            if not value:
                user.sessions_valid_after = utc_now()
        await session.flush()
        logger.info("Admin %s set %s=%s for user %d", actor_id, field, value, user.id)

    return AccessChange(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        field=field,
        value=value,
        changed=changed,
    )


async def delete_user_account(session: AsyncSession, *, actor_id: int | None, user_id: int, confirm_email: str) -> str:
    """Delete an account and all its roles; returns the deleted email."""
    user = await get_user_by_id(session, user_id)
    if user is None:
        raise UserAdminError("not_found", 404)
    if user.id == actor_id:
        raise UserAdminError("self_delete", 403)
    if confirm_email.strip().lower() != user.email:
        raise UserAdminError("confirm_email")
    if await is_last_active_admin(session, user):
        raise UserAdminError("last_admin")
    email = user.email
    await session.delete(user)
    await session.flush()
    logger.info("Admin %s deleted user %d", actor_id, user_id)
    return email
