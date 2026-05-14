from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def parse_cors_origins(raw_value: str) -> list[str] | str:
    value = raw_value.strip()
    if value == '*':
        return '*'
    if not value:
        return ['http://127.0.0.1:5000', 'http://localhost:5000']
    return [item.strip() for item in value.split(',') if item.strip()]


def parse_bool(raw_value: str, default: bool = False) -> bool:
    value = raw_value.strip().lower()
    if not value:
        return default
    return value in {'1', 'true', 'yes', 'on'}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    debug: bool
    secret_key: str
    booth_access_token: str
    socket_cors_origins: list[str] | str
    default_jitsi_room: str
    jitsi_domain: str
    ingest_hls_root: Path
    hls_segment_seconds: int
    hls_playlist_length: int


def load_settings() -> Settings:
    return Settings(
        host=os.getenv('HOST', '127.0.0.1'),
        port=int(os.getenv('PORT', '5000')),
        debug=parse_bool(os.getenv('FLASK_DEBUG', '1'), default=True),
        secret_key=os.getenv('SECRET_KEY', 'change-me'),
        booth_access_token=os.getenv('BOOTH_ACCESS_TOKEN', ''),
        socket_cors_origins=parse_cors_origins(os.getenv('BOOTH_WS_CORS_ORIGINS', '*')),
        default_jitsi_room=os.getenv('DEFAULT_JITSI_ROOM', 'https://meet.jit.si/eventyay-stage-room'),
        jitsi_domain=os.getenv('JITSI_DOMAIN', 'meet.jit.si'),
        ingest_hls_root=Path(os.getenv('INGEST_HLS_ROOT', './hls-output')).resolve(),
        hls_segment_seconds=int(os.getenv('HLS_SEGMENT_SECONDS', '2')),
        hls_playlist_length=int(os.getenv('HLS_PLAYLIST_LENGTH', '8')),
    )


settings = load_settings()
