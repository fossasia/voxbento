from __future__ import annotations

from urllib.parse import urlparse

from pydantic import Field
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
    debug: bool = Field(default=True)
    secret_key: str = 'change-me'
    booth_access_token: str = ''
    default_jitsi_room: str = 'eventyay-stage-room'
    jitsi_domain: str = 'localhost:8080'
    # Full Jitsi base URL including scheme. When empty, defaults to
    # http://{jitsi_domain}. Set to https://... for production HTTPS.
    jitsi_base_url: str = ''
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

    @property
    def effective_jitsi_base_url(self) -> str:
        return self.jitsi_base_url or f'http://{self.jitsi_domain}'

    @property
    def effective_jitsi_domain(self) -> str:
        """Hostname (and port) derived from effective_jitsi_base_url.

        Always consistent with the pre-filled Jitsi URL so that the JS
        validation in joinMonitoringFeed() does not reject its own input.
        """
        return urlparse(self.effective_jitsi_base_url).netloc
    # JWT configuration — jwt_secret defaults to secret_key when empty
    jwt_secret: str = ''
    jwt_expiry_seconds: int = 86400

    @property
    def effective_jwt_secret(self) -> str:
        return self.jwt_secret or self.secret_key


settings = Settings()
