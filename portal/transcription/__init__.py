from portal.transcription.constants import ProviderEnum, ALLOWED_MODELS
from portal.transcription.providers.base import ProviderConfig, get_api_key
from portal.transcription.worker import (
    active_workers,
    active_processes,
    start_transcription_worker,
    stop_transcription_worker,
)
import httpx
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
