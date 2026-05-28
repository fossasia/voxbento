from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore',
        populate_by_name=True,
    )

    host: str = '127.0.0.1'
    port: int = 8000
    debug: bool = Field(default=True, validation_alias=AliasChoices('flask_debug', 'debug'))
    secret_key: str = 'change-me'
    booth_access_token: str = ''
    default_jitsi_room: str = 'https://meet.jit.si/eventyay-stage-room'
    jitsi_domain: str = 'meet.jit.si'
    mediamtx_whip_base: str = 'http://localhost:8889'
    mediamtx_hls_base: str = 'http://localhost:8888'
    # Internal URL for health checks (defaults to mediamtx_hls_base).
    # In Docker, set MEDIAMTX_INTERNAL_BASE=http://mediamtx:8888 so Python
    # can reach MediaMTX via Docker's internal network, while the browser
    # uses MEDIAMTX_HLS_BASE=http://localhost:8888 (the host-mapped port).
    mediamtx_internal_base: str = ''

    @property
    def effective_mediamtx_internal_base(self) -> str:
        return self.mediamtx_internal_base or self.mediamtx_hls_base
    # JWT configuration — jwt_secret defaults to secret_key when empty
    jwt_secret: str = ''
    jwt_expiry_seconds: int = 86400

    @property
    def effective_jwt_secret(self) -> str:
        return self.jwt_secret or self.secret_key


settings = Settings()
