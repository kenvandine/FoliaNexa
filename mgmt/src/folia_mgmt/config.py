"""Runtime configuration. See PLAN.md §8A — state lives under $SNAP_COMMON/mgmt."""

from __future__ import annotations

import secrets
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
    # The default is a deliberately unreachable placeholder, not a real
    # host — every deployment must set this to a real, world-reachable
    # static file host or every world placement will fail at the node
    # agent's jar download with a DNS error. For the snap, set it with
    # `snap set folia-nexa-mgmt artifacts-base-url=<url>` (see
    # snapcraft.yaml/run-mgmt-daemon.sh) rather than hand-rolling a
    # systemd environment override.
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

    # World backups (PLAN.md §6A) — a plain tar.gz of the world save +
    # plugins/ (jars included, not just data, so a restore brings back
    # the exact plugin versions that were running at backup time), fetched
    # from folia-nexa-node's own /backup endpoint over plain HTTP rather
    # than through LXD's storage layer at all. See world_backups.py.
    # Deliberately generous, separate from node_health_timeout_seconds
    # above (that one's for fast /healthz-style polls) — a real world
    # save can legitimately take a while to stream, and a too-short
    # timeout here would repeat the exact mistake LXD's own operation-wait
    # poll made this same session (see lxd_client.py's
    # LONG_OPERATION_TIMEOUT comment): mgmt giving up and reporting a
    # false failure while the transfer is still genuinely in progress.
    world_backup_fetch_timeout_seconds: float = 600.0

    # LXD container-level snapshot backups (LXDClient.snapshot_container/
    # restore_snapshot, and the ad-hoc POST /{name}/snapshot and POST
    # /{name}/restore/{snapshot_name} endpoints) — kept in the codebase as
    # a feature for whenever a host's storage pool has native
    # copy-on-write snapshot support (ZFS/btrfs/LVM), but off by default.
    # The tracked "time machine" backup feature (scheduled + manual,
    # dashboard restore) never depends on this flag at all — it always
    # uses the file-level world_backups.py path. Independent of this
    # flag, LXDClient itself unconditionally refuses to snapshot/restore
    # an instance on a "dir"-backed storage pool (see
    # get_storage_driver_for_instance) — a "dir" pool has no native
    # snapshot support and freezes the whole container for a full rootfs
    # copy, which wedged a live world this same session even with this
    # flag's equivalent left implicitly "on". Flipping this flag on a
    # dir-backed host will not bypass that check.
    lxd_snapshot_backups_enabled: bool = False

    # LXD host trust / enrollment (PLAN.md §3, §4)
    join_token_ttl_seconds: int = 15 * 60

    # Discord OAuth2 app credentials (PLAN.md §11C) — unset disables the
    # integration. Which guild/role gates auto-approval is *not* here —
    # that's DiscordAccessGateConfig, a DB-backed singleton editable from
    # the dashboard (see routers/access_requests.py) so it can be changed
    # without a redeploy and is shared with folia-nexa-bot's role-sync.
    discord_client_id: str | None = None
    discord_client_secret: str | None = None
    discord_redirect_uri: str | None = None

    @property
    def discord_configured(self) -> bool:
        return bool(self.discord_client_id and self.discord_client_secret)

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

    # A world's WorldPresence row (routers/presence.py) is treated as
    # stale — shown as empty rather than a frozen player list — once it's
    # older than this. folia-routes-sync reports on its own
    # FOLIA_ROUTES_POLL_SECONDS cadence (default 5s), so this default (12x
    # that) tolerates a handful of missed polls before assuming the proxy
    # itself, or that one world, has gone dark.
    public_presence_stale_seconds: float = 60.0

    # Velocity "modern" forwarding secret, shared between folia-nexa-proxy
    # and every world's paper-global.yml (see PLAN.md §7 and
    # docs.papermc.io/velocity/player-information-forwarding). mgmt is the
    # single source of truth — it already talks to both the proxy (routes
    # polling) and every world (via node's devlxd config, scheduler.py) —
    # generated once on first access and persisted, not operator-supplied,
    # since nothing about its value is meaningful to a human.
    @property
    def velocity_forwarding_secret_path(self) -> Path:
        return self.state_dir / "velocity-forwarding-secret"

    def get_velocity_forwarding_secret(self) -> str:
        path = self.velocity_forwarding_secret_path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_urlsafe(32))
        return path.read_text().strip()

    @property
    def db_path(self) -> Path:
        return self.state_dir / "mgmt.db"

    @property
    def certs_dir(self) -> Path:
        return self.state_dir / "certs"

    # Operator-uploaded plugin jars (e.g. commercial plugins with no
    # public download URL to point the catalog at) — see plugin_upload.py.
    # Each upload gets its own random-token subdirectory, mounted
    # unauthenticated at /plugin-jars/ (main.py) so folia-nexa-node can
    # fetch it the same deliberately-credential-less way it already
    # fetches every other catalog download_url (routers/worlds.py's
    # get_plugins_manifest) — the per-upload token is what keeps this
    # "private" rather than world-discoverable, not an auth check.
    @property
    def plugin_uploads_dir(self) -> Path:
        return self.state_dir / "plugin-jars"

    # World backup tarballs (world_backups.py) — one file per backup at
    # world-backups/<world_name>/<label>.tar.gz. Lives on *this* mgmt
    # host's own disk, never the world's own LXD host/container — mgmt
    # actively pulls the tarball over HTTP and stores it here. This flat,
    # deterministic layout is deliberately rsync-friendly: an operator
    # can point a cron/systemd-timer rsync job at this directory from a
    # separate backup host as a disaster-recovery measure for a lost mgmt
    # node, with no code involved on either side — see CLAUDE.md's World
    # backups entry for the full recovery procedure.
    @property
    def world_backups_dir(self) -> Path:
        return self.state_dir / "world-backups"


def get_settings() -> Settings:
    settings = Settings()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.certs_dir.mkdir(parents=True, exist_ok=True)
    settings.plugin_uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.world_backups_dir.mkdir(parents=True, exist_ok=True)
    return settings
