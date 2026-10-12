from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    android_display_control_enabled: bool = False
    model_config = SettingsConfigDict(env_prefix="HOMEAI_", env_file=".env.local", extra="ignore")
    database_url: str = Field(default="postgresql+psycopg://homeai:homeai@127.0.0.1:5432/homeai", repr=False)
    state_dir: Path = Path("state")
    admin_dist: Path = Path("admin-web/dist")
    master_key_file: Path = Path("state/master.key")
    environment: str = "development"
    opa_url: str = "http://127.0.0.1:8181"
    nats_url: str = "nats://127.0.0.1:4222"
    event_stream: str = "HOMEAI"
    event_subject_prefix: str = "homeai.events"
    session_seconds: int = 900
    max_upload_bytes: int = 20 * 1024 * 1024
    media_image_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=256 * 1024 * 1024)
    media_file_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1, le=512 * 1024 * 1024)
    media_video_max_bytes: int = Field(default=200 * 1024 * 1024, ge=1, le=1024 * 1024 * 1024)
    media_video_max_seconds: int = Field(default=600, ge=1, le=7200)
    provider_timeout_seconds: int = 60
    identity_certificate_file: Path = Path('state/tls/server.crt')
    server_addresses: list[str] = []
    managed_https_port: int = Field(default=58448,ge=1024,le=65535)
    shell_policy_file: Path | None = None

    direct_enabled: bool = True
    encrypted_relay_enabled: bool = False
    direct_signalling_poll_seconds: float = Field(default=1.0, ge=0.5, le=5)
    direct_stun_urls: list[str] = Field(default_factory=list, max_length=2)

    apns_key_file: Path | None = None
    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_topic: str = ""
