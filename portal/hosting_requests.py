"""Validation and formatting for hosting access requests (#768).

Signed-in users without hosting access submit a request from their Account
page; Admins grant or decline it from the Hosting access page.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

ORGANIZATION_MAX = 200
EVENT_NAME_MAX = 200
LANGUAGES_MAX = 500
MESSAGE_MAX = 2000
DECLINE_REASON_MAX = 1000

_LINE_BREAKS = re.compile(r"[\r\n]+")


@dataclass(frozen=True)
class HostingRequestInput:
    organization: str
    event_name: str
    event_starts_on: date | None
    event_ends_on: date | None
    languages: str
    message: str


def single_line(value: object) -> str:
    """Collapse line breaks so a value is safe in one-line contexts such as email subjects."""
    if not isinstance(value, str):
        return ""
    return _LINE_BREAKS.sub(" ", value).strip()


def multi_line(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").strip()


def parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_hosting_request_form(form: Mapping[str, object]) -> tuple[HostingRequestInput, dict[str, str]]:
    """Return the cleaned request and a field -> message map of validation errors."""
    organization = single_line(form.get("organization"))
    event_name = single_line(form.get("event_name"))
    languages = single_line(form.get("languages"))
    message = multi_line(form.get("message"))
    raw_starts = single_line(form.get("event_starts_on"))
    raw_ends = single_line(form.get("event_ends_on"))
    event_starts_on = parse_date(raw_starts)
    event_ends_on = parse_date(raw_ends)

    errors: dict[str, str] = {}
    if not organization:
        errors["organization"] = "Enter your organization."
    elif len(organization) > ORGANIZATION_MAX:
        errors["organization"] = f"Use at most {ORGANIZATION_MAX} characters."
    if not event_name:
        errors["event_name"] = "Enter the name of your event."
    elif len(event_name) > EVENT_NAME_MAX:
        errors["event_name"] = f"Use at most {EVENT_NAME_MAX} characters."
    if not languages:
        errors["languages"] = "Enter the languages you need."
    elif len(languages) > LANGUAGES_MAX:
        errors["languages"] = f"Use at most {LANGUAGES_MAX} characters."
    if len(message) > MESSAGE_MAX:
        errors["message"] = f"Use at most {MESSAGE_MAX} characters."
    if raw_starts and event_starts_on is None:
        errors["event_starts_on"] = "Enter a valid date."
    if raw_ends and event_ends_on is None:
        errors["event_ends_on"] = "Enter a valid date."
    if event_starts_on and event_ends_on and event_ends_on < event_starts_on:
        errors["event_ends_on"] = "The event can't end before it starts."

    cleaned = HostingRequestInput(
        organization=organization,
        event_name=event_name,
        event_starts_on=event_starts_on,
        event_ends_on=event_ends_on,
        languages=languages,
        message=message,
    )
    return cleaned, errors


def format_event_dates(starts_on: date | None, ends_on: date | None) -> str:
    """Render a request's event dates, e.g. "Oct 11, 2026 – Oct 13, 2026"."""
    if starts_on and ends_on:
        if starts_on == ends_on:
            return f"{starts_on:%b %d, %Y}"
        return f"{starts_on:%b %d, %Y} – {ends_on:%b %d, %Y}"
    if starts_on:
        return f"From {starts_on:%b %d, %Y}"
    if ends_on:
        return f"Until {ends_on:%b %d, %Y}"
    return "Not given"
