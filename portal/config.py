from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore',
        populate_by_name=True,
    )

    host: str = '127.0.0.1'
    port: int = 5000
    # FLASK_DEBUG kept as legacy env var name for .env compatibility
    debug: bool = Field(default=True, validation_alias=AliasChoices('flask_debug', 'debug'))
    secret_key: str = 'change-me'
    booth_access_token: str = ''
    # BOOTH_WS_CORS_ORIGINS kept as legacy env var name for .env compatibility
    socket_cors_origins: str | list[str] = Field(
        default='*',
        validation_alias=AliasChoices('booth_ws_cors_origins', 'socket_cors_origins'),
    )
    default_jitsi_room: str = 'https://meet.jit.si/eventyay-stage-room'
    jitsi_domain: str = 'meet.jit.si'
    ingest_hls_root: Path = Field(default_factory=lambda: Path('./hls-output').resolve())
    hls_segment_seconds: int = 2
    hls_playlist_length: int = 8
    mediamtx_whip_base: str = 'http://localhost:8889'
    mediamtx_hls_base: str = 'http://localhost:8888'
    use_legacy_ingest: bool = False
    # JWT configuration — jwt_secret defaults to secret_key when empty
    jwt_secret: str = ''
    jwt_expiry_seconds: int = 86400

    @property
    def effective_jwt_secret(self) -> str:
        return self.jwt_secret or self.secret_key

    @field_validator('socket_cors_origins', mode='before')
    @classmethod
    def _parse_cors(cls, v: str | list) -> str | list[str]:
        if isinstance(v, list):
            return v
        value = str(v).strip()
        if value == '*':
            return '*'
        if not value:
            return ['http://127.0.0.1:5000', 'http://localhost:5000']
        return [item.strip() for item in value.split(',') if item.strip()]

    @field_validator('ingest_hls_root', mode='before')
    @classmethod
    def _resolve_path(cls, v: str | Path) -> Path:
        return Path(str(v)).resolve()


settings = Settings()
