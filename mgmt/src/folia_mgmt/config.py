"""Runtime configuration. See PLAN.md §8A — state lives under $SNAP_COMMON/mgmt."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOLIA_MGMT_")

    # $SNAP_COMMON/mgmt when running as the snap; a local dir otherwise.
    state_dir: Path = Path.home() / ".local" / "share" / "folia-smp-mgmt"

    listen_host: str = "0.0.0.0"
    listen_port: int = 8443

    # Where folia-smp-node downloads jars/plugin manifests from. PLAN.md §9's
    # example: https://artifacts.internal/folia/1.21.4/folia.jar
    artifacts_base_url: str = "https://artifacts.internal"

    # LXD host trust / enrollment (PLAN.md §3, §4)
    join_token_ttl_seconds: int = 15 * 60

    # Discord OAuth2 (PLAN.md §11C) — unset disables the integration.
    discord_client_id: str | None = None
    discord_client_secret: str | None = None
    discord_redirect_uri: str | None = None
    discord_guild_id: str | None = None
    discord_auto_approve_role_id: str | None = None
    discord_bot_token: str | None = None

    @property
    def discord_configured(self) -> bool:
        return bool(self.discord_client_id and self.discord_client_secret and self.discord_guild_id)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "mgmt.db"

    @property
    def certs_dir(self) -> Path:
        return self.state_dir / "certs"


def get_settings() -> Settings:
    settings = Settings()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.certs_dir.mkdir(parents=True, exist_ok=True)
    return settings
