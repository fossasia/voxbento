import httpx

from portal.transcription.constants import ALLOWED_MODELS, ProviderEnum
from portal.transcription.providers.base import ProviderConfig, get_api_key
from portal.transcription.worker import (
    active_processes,
    active_workers,
    start_transcription_worker,
    stop_transcription_worker,
)

shared_http_client: httpx.AsyncClient | None = None

__all__ = [
    "ProviderEnum",
    "ALLOWED_MODELS",
    "ProviderConfig",
    "get_api_key",
    "active_workers",
    "active_processes",
    "start_transcription_worker",
    "stop_transcription_worker",
    "shared_http_client",
]
