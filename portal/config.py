from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


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
    floor_bot_base: str = "http://floor-bot:8080"
    meeting_agent_max_restarts: int = 5  # Automatic agent restarts per incident before reporting degraded.
    meeting_agent_no_audio_secs: float = 45.0
    mediamtx_auth_hook_secret: str = ""
    program_ingest_enabled: bool = False  # Enable only with the authenticated MediaMTX configuration.
    program_ingest_max_rooms: int = 10
    program_ingest_disconnect_grace_secs: float = 15.0
    program_ingest_stall_secs: float = 30.0
    program_sender_session_ttl_secs: int = 150  # Browser sender publish window; long enough to negotiate, short enough to expire.
    # RTMPS ingest with YouTube relay (#749). Off unless the operator deploys docker-compose.relay.yml.
    program_relay_enabled: bool = False
    # RTMPS contribution ingest without relaying (VoxBento as an extra destination).
    # Relay deployments imply it; see rtmps_ingest_enabled.
    program_rtmps_enabled: bool = False
    program_rtmps_public_base: str = ""  # e.g. rtmps://voxbento.com:1936 (MediaMTX RTMPS listener)
    program_relay_base: str = "http://program-relay:8090"
    program_relay_control_secret: str = ""
    program_relay_max: int = 4
    program_relay_allowed_hosts: str = "a.rtmps.youtube.com,b.rtmps.youtube.com"

    @property
    def rtmps_ingest_enabled(self) -> bool:
        return self.program_ingest_enabled and (self.program_rtmps_enabled or self.program_relay_enabled)

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
    listener_token_expiry_seconds: int = 14400  # 4 hours
    # Embed iframe tokens have a shorter lifetime because the token sits in a
    # publicly visible iframe src= attribute, making it more easily copied.
    embed_token_expiry_seconds: int = 1800  # 30 minutes
    # Space-separated CSP frame-ancestors value for the /embed/ route.
    # Input is comma-separated for human readability and converted to spaces internally.
    # Empty string = frame-ancestors * (any site may embed).
    embed_allowed_origins: str = ""

    # Database — SQLite for dev, PostgreSQL for prod
    database_url: str = "sqlite+aiosqlite:///./interpretation.db"

    # Booth recovery (portal/booth_recovery.py). A participant whose booth socket
    # drops keeps their place, including the active slot, for this long so a
    # network blip does not take the live interpreter off air. 0 removes them at once.
    booth_reconnect_grace_seconds: float = 20.0
    # Save each booth's Go Live switch and active interpreter, and restore them
    # when the portal restarts, so a deploy or crash mid-event keeps booths live.
    booth_state_recovery_enabled: bool = True
    # How long a restored active interpreter keeps the slot waiting to reconnect.
    booth_restart_grace_seconds: float = 60.0
    # Saved state is not restored when its last save is older than this. Live booths are saved on
    # every change and at least every minute, so the age is the downtime plus up to a minute before it.
    booth_state_max_age_seconds: float = 600.0

    @property
    def effective_jwt_secret(self) -> str:
        return self.jwt_secret or self.secret_key

    def validate_production_secrets(self) -> None:
        """Refuse to start with a known-weak default secret outside debug mode.

        Called once at application startup. No-op in debug mode so local
        development works without configuring a SECRET_KEY.

        Note for upgrades: Existing production deployments that previously ran
        with a weak or short secret must configure a 32+ character random
        JWT_SECRET (or SECRET_KEY if JWT_SECRET is unset) before starting.
        If stored credentials were tied to the old secret, reissue them after
        rotation.
        """
        if self.debug:
            return
        weak_defaults = {
            "change-me",
            "",
            "secret",
            "your-secret-key",
            "admin",
            "password",
            "123456",
            "qwerty",
            "letmein",
            "changeme",
            "test",
            "1234567890",
        }
        effective_secret = (self.effective_jwt_secret or "").strip()
        if not effective_secret or effective_secret in weak_defaults:
            raise RuntimeError(
                "SECRET_KEY (or JWT_SECRET) is set to a known-weak default value. "
                "Set a strong random value before running in production. "
                "Generate one with: openssl rand -hex 32"
            )
        if len(effective_secret) < 32:
            raise RuntimeError(
                "SECRET_KEY (or JWT_SECRET) is too short for production use. "
                "It must be at least 32 characters. "
                "Generate one with: openssl rand -hex 32"
            )
        if len(set(effective_secret)) < 8:
            raise RuntimeError(
                "SECRET_KEY (or JWT_SECRET) lacks sufficient character diversity. "
                "Set a strong random value before running in production. "
                "Generate one with: openssl rand -hex 32"
            )
        if not self.mediamtx_auth_hook_secret:
            raise RuntimeError(
                "MEDIAMTX_AUTH_HOOK_SECRET is not set. Configure the same random value for "
                "the portal and MediaMTX. Generate one with: openssl rand -hex 32"
            )
        booth_access_token = self.booth_access_token.strip()
        if booth_access_token in {"", "change-me", "secret", "your-secret-key"} or len(booth_access_token) < 32:
            raise RuntimeError(
                "BOOTH_ACCESS_TOKEN is not configured with a strong value. "
                "Set a secret access token before exposing legacy API endpoints in production. "
                "Generate one with: openssl rand -hex 32"
            )
        if self.program_relay_enabled and len(self.program_relay_control_secret) < 32:
            raise RuntimeError(
                "PROGRAM_RELAY_ENABLED requires PROGRAM_RELAY_CONTROL_SECRET (at least 32 characters), "
                "shared with the program-relay service. Generate one with: openssl rand -hex 32"
            )
        # Every outgoing email is encrypted at rest in the outbox, so sign-in links,
        # invitations and password resets cannot be sent without this key.
        encryption_keys = [
            key.strip() for key in (self.api_key_encryption_key or "").split(",") if key.strip()
        ]
        weak_encryption_keys = {"change-this-encryption-key-in-production", "your-secure-32-char-key-here"}
        if not encryption_keys or any(key in weak_encryption_keys or len(key) < 32 for key in encryption_keys):
            raise RuntimeError(
                "API_KEY_ENCRYPTION_KEY is not set. It encrypts stored provider keys and queued email, "
                "so sign-in and invitation emails cannot be sent without it. "
                "Generate one with: openssl rand -hex 32 (to rotate, list the new key first, comma-separated)"
            )

    # Transcription Settings
    nvidia_function_id: str = ""

    # Self-hosted translation: an OpenAI-compatible server (Ollama, llama.cpp, vLLM) run by
    # the operator. Only the server sets this URL, so event owners can't aim requests at
    # internal addresses. Models is a comma-separated allow list; the first is the default.
    self_hosted_llm_base_url: str = ""
    self_hosted_llm_api_key: str = Field(default="", repr=False)
    self_hosted_llm_models: str = ""

    # Supertonic synthesis quality/speed trade-off. Fewer diffusion steps =
    # faster (lower real-time factor) at a small quality cost. 4 keeps CPU
    # synthesis at/below real-time so audio never backs up; 8 is reference
    # quality but slower than real-time on CPU.
    supertonic_total_steps: int = 4
    # ONNX Runtime intra-op threads for synthesis. 0 = let ONNX Runtime pick
    # (uses all physical cores).
    supertonic_intra_op_threads: int = 0

    # Email Settings
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "noreply@voxbento.com"
    smtp_from_name: str = "VoxBento"
    # Team inbox notified of each new hosting access request.
    hosting_requests_email: str = "voxbento.dev@gmail.com"
    public_base_url: str = "https://voxbento.com"
    # Opt-in so existing deployments work before a Turnstile widget is provisioned.
    registration_turnstile_enabled: bool = False
    turnstile_site_key: str = ""
    turnstile_secret_key: str = Field(default="", repr=False)
    registration_requests_per_minute: int = Field(default=20, ge=1)
    # Webhook Settings
    max_webhook_subscriptions: int = 50
    # Caption WebSocket Settings
    max_anonymous_caption_listeners_per_booth: int = 500

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: str) -> str:
        """Reject query strings and fragments from canonical public URLs."""
        parsed = urlparse(value)
        if parsed.query or parsed.fragment:
            raise ValueError("public_base_url must not include a query string or fragment")
        return value

    @field_validator("self_hosted_llm_base_url")
    @classmethod
    def validate_self_hosted_llm_base_url(cls, value: str) -> str:
        """Accept only a plain http(s) base URL; the key belongs in SELF_HOSTED_LLM_API_KEY."""
        value = value.strip().rstrip("/")
        if not value:
            return ""
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("self_hosted_llm_base_url must be an http(s) URL such as http://localhost:11434/v1")
        if not _is_ip(parsed.hostname) and not re.fullmatch(r"[A-Za-z0-9._-]+", parsed.hostname):
            raise ValueError("self_hosted_llm_base_url has an invalid host name")
        # Reading the port raises ValueError for one that isn't a number in 0-65535.
        if parsed.port == 0:
            raise ValueError("self_hosted_llm_base_url port must be between 1 and 65535")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("self_hosted_llm_base_url must not include credentials, a query string or a fragment")
        if parsed.path.endswith("/chat/completions"):
            raise ValueError("self_hosted_llm_base_url is the base URL; leave out /chat/completions")
        return value

    @field_validator("self_hosted_llm_models")
    @classmethod
    def validate_self_hosted_llm_models(cls, value: str) -> str:
        """Model names are stored on rooms and booths, whose columns hold 100 characters."""
        if any(len(m.strip()) > 100 for m in value.split(",")):
            raise ValueError("each SELF_HOSTED_LLM_MODELS name must be 100 characters or fewer")
        return value

    @property
    def self_hosted_llm_model_list(self) -> tuple[str, ...]:
        """The allowed self-hosted models, in the order the operator listed them."""
        return tuple(dict.fromkeys(m.strip() for m in self.self_hosted_llm_models.split(",") if m.strip()))

    @property
    def self_hosted_llm_enabled(self) -> bool:
        """Self-hosted translation is offered only with both a server URL and at least one model."""
        return bool(self.self_hosted_llm_base_url and self.self_hosted_llm_model_list)

    def self_hosted_llm_allows(self, model: str | None) -> bool:
        """Whether rooms and booths may use ``model`` on the self-hosted server right now."""
        return self.self_hosted_llm_enabled and model in self.self_hosted_llm_model_list

    def self_hosted_llm_model_or_default(self, model: str | None) -> str | None:
        """``model`` when allowed, else the default (first listed) model, else None when not configured."""
        if self.self_hosted_llm_allows(model):
            return model
        return self.self_hosted_llm_model_list[0] if self.self_hosted_llm_enabled else None


settings = Settings()
