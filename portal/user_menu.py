"""The shared header and user menu shown on every signed-in page (#766).

The header names the area the person is in ("Admin", "My events", "Account"),
and the user menu offers the same entries everywhere, filtered by who the
person is:

| Entry         | Who sees it                              |
|---------------|------------------------------------------|
| My events     | Everyone                                 |
| Account       | Everyone                                 |
| Developer API | Users with an approved developer app     |
| Admin         | Admins                                   |
| Logout        | Everyone                                 |
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from portal.auth import resolve_request_principal
from portal.database import get_session

AREA_EVENTS = "events"
AREA_ACCOUNT = "account"
AREA_DEVELOPER = "developer"
AREA_ADMIN = "admin"

AREA_LABELS = {
    AREA_EVENTS: "My events",
    AREA_ACCOUNT: "Account",
    AREA_DEVELOPER: "Developer API",
    AREA_ADMIN: "Admin",
}

MY_EVENTS_URL = "/workspace/"


@dataclass(frozen=True)
class MenuEntry:
    area: str
    label: str
    href: str
    icon: str


@dataclass(frozen=True)
class UserMenu:
    display_name: str
    email: str
    area: str
    entries: tuple[MenuEntry, ...]

    @property
    def area_label(self) -> str:
        return AREA_LABELS[self.area]

    @property
    def short_name(self) -> str:
        return short_user_label(self.display_name, self.email)


SHORT_LABEL_MAX = 11


def short_user_label(display_name: str, email: str) -> str:
    """Name on the menu button, as in Eventyay: first name, else the email's local part, at most 11 characters."""
    parts = (display_name or "").split()
    label = parts[0] if parts else (email or "").split("@")[0]
    return label if len(label) <= SHORT_LABEL_MAX else f"{label[:SHORT_LABEL_MAX]}…"


def area_for_request(request: Request, *, is_super_admin: bool) -> str:
    """Name the area a page belongs to from its path."""
    path = request.url.path
    if path.startswith("/account"):
        return AREA_ACCOUNT
    if path.startswith("/developer"):
        return AREA_DEVELOPER
    if getattr(request.state, "is_workspace", False) or path.startswith(("/workspace", "/mission-control")):
        return AREA_EVENTS
    # Room coordinators still reach a few legacy /admin/ room pages; for them
    # those pages are part of their events, not the Admin area.
    if path.startswith("/admin") and is_super_admin:
        return AREA_ADMIN
    return AREA_EVENTS


def menu_entries(*, is_admin: bool, has_developer_app: bool) -> tuple[MenuEntry, ...]:
    entries = [
        MenuEntry(AREA_EVENTS, "My events", MY_EVENTS_URL, "calendar"),
        MenuEntry(AREA_ACCOUNT, "Account", "/account", "user"),
    ]
    if has_developer_app:
        entries.append(MenuEntry(AREA_DEVELOPER, "Developer API", "/developer", "code"))
    if is_admin:
        entries.append(MenuEntry(AREA_ADMIN, "Admin", "/admin/", "shield"))
    return tuple(entries)


def user_menu_context(request: Request) -> dict[str, UserMenu | None]:
    """Jinja context processor: the menu for the signed-in person, if any.

    Reads the principal resolved earlier in the request (see
    ``resolve_user_menu``), so it never touches the database itself.
    """
    principal = getattr(request.state, "principal", None)
    user = principal.get("user") if principal else None
    if user is None:
        return {"user_menu": None}
    is_admin = bool(principal.get("is_super_admin"))
    return {
        "user_menu": UserMenu(
            display_name=user.display_name,
            email=user.email,
            area=area_for_request(request, is_super_admin=is_admin),
            entries=menu_entries(is_admin=is_admin, has_developer_app=bool(principal.get("has_developer_app"))),
        )
    }


async def resolve_user_menu(request: Request) -> None:
    """Dependency that loads the signed-in person once so templates can show the menu."""
    if getattr(request.state, "principal", None) is not None:
        return
    async with get_session() as session:
        await resolve_request_principal(request, session)
