"""Runtime configuration. See PLAN.md §8A — state lives under $SNAP_COMMON/mgmt."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOLIA_MGMT_")

    # $SNAP_COMMON/mgmt when running as the snap; a local dir otherwise.
    state_dir: Path = Path.home() / ".local" / "share" / "folia-nexa-mgmt"

    listen_host: str = "0.0.0.0"
    listen_port: int = 8443

    # Where folia-nexa-node downloads jars from. PLAN.md §9's example:
    # https://artifacts.internal/folia/1.21.4/folia.jar. Plugin *manifests*
    # (not the plugin jars themselves) come from this mgmt instance's own
    # API instead — see public_url below and PLAN.md §14's plugin catalog.
    artifacts_base_url: str = "https://artifacts.internal"

    # This mgmt instance's own network-reachable address, e.g.
    # https://mgmt.internal:8443 — every world with plugins needs to reach
    # GET /api/v1/worlds/{name}/plugins-manifest here (unauthenticated;
    # see routers/worlds.py). Unset means worlds can't declare plugins —
    # enforced at world-creation time, not silently ignored.
    public_url: str | None = None

    # Curated plugin catalog (PLAN.md §14). Bundled with the snap by
    # default; $SNAP_COMMON/plugin-catalog-override.yaml lets an operator
    # add/override entries (e.g. their own in-house plugins) without a new
    # mgmt release — see plugin_catalog.py.
    plugin_catalog_path: Path = Path(__file__).resolve().parent / "catalog.yaml"

    # Curated data pack catalog (e.g. Matcha) — same override mechanism as
    # plugin_catalog_path above, see datapack_catalog.py.
    datapack_catalog_path: Path = Path(__file__).resolve().parent / "datapacks.yaml"

    # Must match folia-nexa-node's FOLIA_NODE_HEALTH_PORT (node/snapcraft.yaml).
    node_health_port: int = 8123
    node_health_timeout_seconds: float = 3.0

    # LXD host trust / enrollment (PLAN.md §3, §4)
    join_token_ttl_seconds: int = 15 * 60

    # Discord OAuth2 (PLAN.md §11C) — unset disables the integration.
    discord_client_id: str | None = None
    discord_client_secret: str | None = None
    discord_redirect_uri: str | None = None
    discord_guild_id: str | None = None
    discord_auto_approve_role_id: str | None = None

    @property
    def discord_configured(self) -> bool:
        return bool(self.discord_client_id and self.discord_client_secret and self.discord_guild_id)

    # LuckPerms shared MySQL/MariaDB backend (PLAN.md §11B) — provisioned
    # by the operator directly (folia-nexa-node only knows how to run a
    # Folia/Paper JVM, not arbitrary services like a database server), then
    # pointed at here so mgmt can keep every LuckPerms-enabled world's
    # config.yml in sync with it. Unset host disables the sync.
    luckperms_mysql_host: str | None = None
    luckperms_mysql_port: int = 3306
    luckperms_mysql_database: str = "luckperms"
    luckperms_mysql_user: str = "luckperms"
    luckperms_mysql_password: str | None = None
    luckperms_table_prefix: str = "luckperms_"

    @property
    def luckperms_configured(self) -> bool:
        return bool(self.luckperms_mysql_host and self.luckperms_mysql_password)

    # Public player-hub API (PLAN.md §7A) — GET /api/v1/public/* is
    # deliberately unauthenticated, the same way plugins-manifest already
    # is, since it only ever returns already-public leaderboard/profile
    # data. Unlike every other mgmt route, this one is meant to be reached
    # by anonymous internet traffic (via the VPS edge's api.<domain>
    # reverse proxy) — the in-process cache and per-IP rate limit below are
    # mgmt's own defense in depth; the VPS's Caddy is expected to add
    # another layer in front of that.
    public_api_cache_seconds: float = 30.0
    public_api_rate_limit_per_minute: int = 60

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
