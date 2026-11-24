from __future__ import annotations

"""Single source of truth for webhook event types emitted by VoxBento.

Webhooks allow third-party systems to subscribe to events in real time.
Subscriptions accept only registered event types defined here.
"""

# Emitted webhook event types
SESSION_STATUS_CHANGED = "session.status_changed"
BOOTH_TRANSCRIPTION_STARTED = "booth.transcription.started"
BOOTH_TRANSCRIPTION_STOPPED = "booth.transcription.stopped"
BOOTH_INTERPRETER_JOINED = "booth.interpreter.joined"

ALL_WEBHOOK_EVENTS: tuple[str, ...] = (
    SESSION_STATUS_CHANGED,
    BOOTH_TRANSCRIPTION_STARTED,
    BOOTH_TRANSCRIPTION_STOPPED,
    BOOTH_INTERPRETER_JOINED,
)

SUPPORTED_EVENT_TYPES: frozenset[str] = frozenset(ALL_WEBHOOK_EVENTS)


def validate_event_types(event_types: list[str]) -> list[str]:
    """Validate that event_types is non-empty and contains only supported events.

    Returns deduplicated and sorted list of valid event types.
    Raises ValueError with a clear message on validation failure.
    """
    if not event_types:
        raise ValueError("event_types cannot be empty")

    unsupported = sorted(set(event_types) - SUPPORTED_EVENT_TYPES)
    if unsupported:
        unsupported_str = ", ".join(unsupported)
        supported_str = ", ".join(sorted(SUPPORTED_EVENT_TYPES))
        raise ValueError(f"Unsupported event types: {unsupported_str}. Supported event types are: {supported_str}")

    return sorted(set(event_types))
