"""Errors raised by transcription providers."""


class TranscriptionAuthError(Exception):
    """A provider rejected the configured API key.

    Retrying cannot help, so the worker stops the session and reports the
    reason on the ``booth.transcription.stopped`` webhook.
    """

    error_code = "auth_failed"

    def __init__(self, provider: str, detail: str, status_code: int | None = None):
        self.provider = provider
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)
