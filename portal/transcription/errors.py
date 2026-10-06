"""Errors raised by transcription providers."""

import re

MAX_DETAIL_CHARS = 300


def _bounded_detail(detail: str) -> str:
    text = re.sub(r"\s+", " ", str(detail)).strip()
    if len(text) > MAX_DETAIL_CHARS:
        text = text[:MAX_DETAIL_CHARS].rstrip() + "…"
    return text


class TranscriptionAuthError(Exception):
    """A provider rejected the configured API key.

    Retrying cannot help, so the worker stops the session and reports the
    reason on the ``booth.transcription.stopped`` webhook.
    """

    error_code = "auth_failed"

    def __init__(self, provider: str, detail: str, status_code: int | None = None):
        self.provider = provider
        self.detail = _bounded_detail(detail)
        self.status_code = status_code
        super().__init__(self.detail)
