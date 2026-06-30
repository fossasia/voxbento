from __future__ import annotations

from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = Field(default=False)
    secret_key: str = "change-me"
    api_key_encryption_key: str | None = Field(default=None)
    booth_access_token: str = ""
    default_jitsi_room: str = "eventyay-stage-room"
    jitsi_domain: str = "jitsi.voxbento.com"
    # Full Jitsi base URL including scheme. When empty, defaults to
    # http://{jitsi_domain}. Set to https://... for production HTTPS.
    jitsi_base_url: str = ""
    jitsi_internal_base: str = ""
    mediamtx_whip_base: str = "http://localhost:8889"
    # MediaMTX Control API (port 9997). Used to dynamically create named
    # paths with alwaysAvailable so WHEP readers survive publisher handoffs.
    mediamtx_api_base: str = "http://localhost:9997"
    mediamtx_rtsp_base: str = "rtsp://mediamtx:8554"
    # Optional internal MediaMTX base URL. When empty, falls back to
    # mediamtx_api_base. docker-compose sets this to http://mediamtx:8888.
    mediamtx_internal_base: str = ""
    floor_bot_base: str = "http://floor-bot:8080"

    @property
    def effective_mediamtx_internal_base(self) -> str:
        return self.mediamtx_internal_base or self.mediamtx_api_base

    @property
    def effective_jitsi_base_url(self) -> str:
        return self.jitsi_base_url or f"http://{self.jitsi_domain}"

    @property
    def effective_jitsi_internal_base(self) -> str:
        return self.jitsi_internal_base or self.effective_jitsi_base_url

    @property
    def effective_jitsi_domain(self) -> str:
        """Hostname (and port) derived from effective_jitsi_base_url.

        Always consistent with the pre-filled Jitsi URL so that the JS
        validation in joinMonitoringFeed() does not reject its own input.
        """
        return urlparse(self.effective_jitsi_base_url).netloc

    # JWT configuration — jwt_secret defaults to secret_key when empty
    jwt_secret: str = ""
    jwt_expiry_seconds: int = 86400

    # Database — SQLite for dev, PostgreSQL for prod
    database_url: str = "sqlite+aiosqlite:///./interpretation.db"

    # Admin panel — simple password guard (Phase 3 Step 7 will add proper auth)
    admin_password: str = ""

    @property
    def effective_jwt_secret(self) -> str:
        return self.jwt_secret or self.secret_key

    def validate_production_secrets(self) -> None:
        """Refuse to start with a known-weak default secret outside debug mode.

        Called once at application startup. No-op in debug mode so local
        development works without configuring a SECRET_KEY.
        """
        if self.debug:
            return
        weak_defaults = {"change-me", "", "secret", "your-secret-key"}
        if self.effective_jwt_secret in weak_defaults:
            raise RuntimeError(
                "SECRET_KEY (or JWT_SECRET) is set to a known-weak default value. "
                "Set a strong random value before running in production. "
                "Generate one with: openssl rand -hex 32"
            )

    # Transcription Settings
    nvidia_function_id: str = ""

    # Supertonic TTS — optional sidecar URL for the Voice Builder import API.
    # Leave empty to use in-process TTS (no sidecar needed).
    supertonic_base_url: str = ""

    # Supertonic synthesis quality/speed trade-off. Fewer diffusion steps =
    # faster (lower real-time factor) at a small quality cost. 4 keeps CPU
    # synthesis at/below real-time so audio never backs up; 8 is reference
    # quality but slower than real-time on CPU.
    supertonic_total_steps: int = 4
    # ONNX Runtime intra-op threads for synthesis. 0 = let ONNX Runtime pick
    # (uses all physical cores).
    supertonic_intra_op_threads: int = 0


settings = Settings()
