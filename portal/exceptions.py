"""Application-level exceptions for VoxBento portal."""

from __future__ import annotations


class RedirectRequired(Exception):
    """Raised when an auth or permission check requires redirecting the client."""

    def __init__(self, url: str, status_code: int = 303) -> None:
        self.url = url
        self.status_code = status_code
        super().__init__(f"Redirect to {url} with status {status_code}")
