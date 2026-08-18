"""Persistent state. Mirrors the data model in PLAN.md §2, §10, §11."""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    # Naive on purpose: SQLite (via SQLAlchemy) drops tzinfo on round-trip,
    # so an aware "now" compared against a value just loaded from the DB
    # raises TypeError. Every stored timestamp in this module goes through
    # this helper, so naive-vs-naive stays consistent everywhere.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class HostStatus(str, enum.Enum):
    online = "online"
    offline = "offline"
    draining = "draining"
    cordoned = "cordoned"


class WorldType(str, enum.Enum):
    overworld = "overworld"
    nether = "nether"
    end = "end"
    lobby = "lobby"
    minigame = "minigame"
    proxy = "proxy"
    staging = "staging"
    infra = "infra"


class WorldPhase(str, enum.Enum):
    pending = "pending"
    provisioning = "provisioning"
    running = "running"
    crashed = "crashed"
    restarting = "restarting"
    draining = "draining"
    deleted = "deleted"


class UserRole(str, enum.Enum):
    admin = "admin"
    operator = "operator"
    viewer = "viewer"


class AccessRequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"
    revoked = "revoked"  # was approved via Discord role-sync, then lost the role


class Host(SQLModel, table=True):
    """A trusted LXD remote. See PLAN.md §2 (Host) and §3 (trust model)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    address: str  # host:port of the LXD remote API
    project: str = "folia"
    cert_fingerprint: Optional[str] = None
    server_cert_pem: Optional[str] = None  # pinned at enrollment time, PLAN.md §3
    labels: dict = Field(default_factory=dict, sa_column=Column(JSON))
    capacity_cpu_cores: int
    capacity_memory_gb: int
    status: HostStatus = HostStatus.online
    created_at: datetime = Field(default_factory=utcnow)


class World(SQLModel, table=True):
    """A schedulable world: one container, one JVM. See PLAN.md §2 (World)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    type: WorldType
    engine: str = "folia"
    version: str = "1.21.4"
    plugins: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    datapacks: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # server.properties overrides, applied on top of the required baseline
    # keys folia_node.staging always writes itself (online-mode=false etc.)
    # — those aren't operator-settable, see _validate_properties. Synced to
    # a running world on every folia-nexa-node (re)start, not just first
    # boot — see GET /worlds/{name}/server-properties-manifest.
    properties: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))
    # Generated once per world (routers/worlds.py's _ensure_rcon_password),
    # never returned from any API response. Delivered to the world's own
    # container the same way the Velocity forwarding secret is — a
    # user.folia.* devlxd config key (scheduler.py's _node_config), never
    # over HTTP — see rcon.py's module docstring for why this needs its
    # own delivery channel instead of riding along in
    # server-properties-manifest, which is deliberately unauthenticated.
    rcon_password: Optional[str] = None

    cpu_cores: int
    memory_gb: int

    placement_labels: dict = Field(default_factory=dict, sa_column=Column(JSON))
    sticky_host: Optional[str] = None

    snapshot_schedule: Optional[str] = None  # e.g. "@hourly", None = no schedule
    snapshot_expiry: Optional[str] = None  # e.g. "24h"

    phase: WorldPhase = WorldPhase.pending
    host_name: Optional[str] = None
    container_name: Optional[str] = None
    address: Optional[str] = None  # resolved container_ip:25565 once running, PLAN.md §7

    whitelist_enabled: bool = False
    ops: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class User(SQLModel, table=True):
    """Operator account for the mgmt UI/API. See PLAN.md §11A."""

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    role: UserRole = UserRole.viewer
    created_at: datetime = Field(default_factory=utcnow)


class ApiToken(SQLModel, table=True):
    """Session (dashboard) or long-lived (CLI/CI) bearer token for a User."""

    id: Optional[int] = Field(default=None, primary_key=True)
    token_hash: str = Field(unique=True, index=True)
    user_id: int = Field(foreign_key="user.id")
    kind: str = "session"  # "session" | "api"
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: Optional[datetime] = None


class JoinToken(SQLModel, table=True):
    """Short-lived, single-use token authorizing one host enrollment. PLAN.md §4."""

    id: Optional[int] = Field(default=None, primary_key=True)
    token_hash: str = Field(unique=True, index=True)
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    used_at: Optional[datetime] = None
    used_by_host: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return self.used_at is None and utcnow() < self.expires_at


class PlayerProfile(SQLModel, table=True):
    """A tracked player, keyed by Mojang UUID. Created/updated by the stats
    plugin's periodic reports (PLAN.md §7A) — see routers/stats.py."""

    id: Optional[int] = Field(default=None, primary_key=True)
    uuid: str = Field(unique=True, index=True)
    username: str
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)


class PlayerStat(SQLModel, table=True):
    """One counter for one player — e.g. stat_key="kills", value=42.

    Upserted (matched on player_uuid+stat_key, no DB-level unique
    constraint — enforced in routers/stats.py the same way every other
    upsert-by-lookup in this codebase works). `value` holds the current
    running total as reported by the plugin, not a delta — the plugin is
    the source of truth for its own counters, mgmt just mirrors the latest
    value. Known stat_keys: kills, deaths, blocks_mined,
    playtime_seconds_total, auraskills_power_level, axauctions_wealth —
    but this is intentionally open-ended so a future plugin build can
    report new stat_keys without a schema change.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    player_uuid: str = Field(index=True, foreign_key="playerprofile.uuid")
    stat_key: str = Field(index=True)
    value: float = 0
    updated_at: datetime = Field(default_factory=utcnow)


class PlayerPlaytimeDaily(SQLModel, table=True):
    """One row per (player, UTC calendar day) — backs a GitHub-style
    playtime heatmap on a player's profile page without needing a raw
    join/quit event log. `seconds` accumulates across however many reports
    land within that day (the plugin reports periodically, not once daily)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    player_uuid: str = Field(index=True, foreign_key="playerprofile.uuid")
    date: str = Field(index=True)  # "YYYY-MM-DD", UTC
    seconds: int = 0


class WorldPresence(SQLModel, table=True):
    """Live online-player snapshot for one world. PLAN.md §7A.

    Upserted wholesale on every report from folia-routes-sync's poll cycle
    (`FOLIA_ROUTES_POLL_SECONDS`, default 5s — see
    `FoliaRoutesSyncPlugin.pollAndReconcile`), which reads Velocity's own
    `RegisteredServer.getPlayersConnected()` — the one place in this
    cluster that actually knows who's connected to which backend right
    now (node's own health agent explicitly doesn't, see
    `node/src/folia_node/health.py`'s module docstring). Each report fully
    replaces the previous player list for the worlds it mentions; there's
    no join/quit event log, so a disconnected player just stops appearing
    on the next poll. `updated_at` lets a reader (routers/public_stats.py)
    treat a world that's stopped reporting (proxy down, world
    unregistered) as stale rather than showing a frozen player list
    forever."""

    world_name: str = Field(primary_key=True)
    players: list[dict] = Field(default_factory=list, sa_column=Column(JSON))  # [{"uuid": ..., "username": ...}]
    updated_at: datetime = Field(default_factory=utcnow)


class ProxyDisplay(SQLModel, table=True):
    """Singleton row (fixed id=1): the proxy's server-list MOTD and icon,
    editable from the dashboard and polled live by folia-routes-sync
    (PLAN.md §7) — no proxy restart needed. `motd` is a MiniMessage string
    (the same markup Velocity's own velocity.toml `motd` key accepts);
    `icon_png_base64` is a raw (no data-URL prefix) base64-encoded 64x64
    PNG, or None to leave the proxy's baked-in server-icon.png alone."""

    id: Optional[int] = Field(default=1, primary_key=True)
    motd: str = "<#09add3>A Velocity Server"
    icon_png_base64: Optional[str] = None
    updated_at: datetime = Field(default_factory=utcnow)


class MinecraftVersionConfig(SQLModel, table=True):
    """Singleton row (fixed id=1): the single Minecraft engine+version
    every world in this cluster runs. PLAN.md §9 — deliberately cluster-
    wide, not per-world: every world shares one Velocity proxy and
    (PLAN.md §14B) one lobby that routes players between them, so they
    all need to speak the same protocol version — a client on a newer
    version simply cannot connect to an older backend at all, regardless
    of which world it targets.

    scheduler.py's _node_config resolves every world's jar-url/jar-
    engine/jar-version from this row, not from World.engine/World.version
    — those per-world columns still exist (display/history only now; see
    routers/cluster.py's migrate action, which updates them to match
    after actually rolling a version out) but are never read to decide
    what a world actually runs.
    """

    id: Optional[int] = Field(default=1, primary_key=True)
    engine: str = "folia"
    version: str = "26.2"
    updated_at: datetime = Field(default_factory=utcnow)


class ChatBridgeConfig(SQLModel, table=True):
    """Singleton row (fixed id=1): Discord chat-bridge configuration.
    PLAN.md §16 — see routers/chat.py's module docstring for the full
    outbound/inbound design (why this exists instead of installing
    DiscordSRV per-world: DiscordSRV has no concept of "which world" in a
    multi-server Velocity network, this does).

    world_webhook_urls: world name -> Discord webhook URL, for that
    world's own channel. server_wide_webhook_url: one webhook that gets
    every world's chat, regardless of world_webhook_urls. A message can
    go to both (its own world's channel and the combined one) — outbound
    delivery POSTs to whichever of the two are configured for that world,
    not either/or.

    inbound_channels: Discord channel id -> world name, or "*" for a
    channel whose messages broadcast to every connected player network-
    wide rather than just one world's players. Keyed by channel id (a
    Discord snowflake string) since that's what folia-nexa-bot's
    on_message handler actually has; nothing here needs the channel's
    human-readable name.
    """

    id: Optional[int] = Field(default=1, primary_key=True)
    server_wide_webhook_url: Optional[str] = None
    world_webhook_urls: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))
    inbound_channels: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=utcnow)


class AccessRequest(SQLModel, table=True):
    """A Discord-authenticated request to join the network, or a manually
    added Minecraft-only allowlist entry. PLAN.md §11C."""

    id: Optional[int] = Field(default=None, primary_key=True)
    discord_user_id: str = Field(index=True)
    discord_username: str
    minecraft_username: Optional[str] = None
    minecraft_uuid: Optional[str] = None
    status: AccessRequestStatus = AccessRequestStatus.pending
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: Optional[datetime] = None
    decided_by: Optional[int] = Field(default=None, foreign_key="user.id")
    deny_reason: Optional[str] = None
    auto_managed: bool = Field(default=True)
    # True: this row's status is policy-driven (Discord role-sync,
    # OAuth/bot auto-approve) and safe for POST /access-requests/role-sync
    # to keep reconciling automatically. False: an operator explicitly
    # approved/denied it via the manual endpoints, or added it directly
    # to the manual (non-Discord) allowlist — sticky until they act again,
    # role-sync always skips it.


class DiscordAccessGateConfig(SQLModel, table=True):
    """Singleton row (fixed id=1): whether the dynamic Discord-role
    allowlist (PLAN.md §11C) is on, and which guild/role gates it.
    Cluster-wide, not per-world — the proxy is the single access gate
    every world shares. Editable from the dashboard; folia-nexa-bot polls
    this instead of reading a static env var, so a toggle here takes
    effect without a bot restart."""

    id: Optional[int] = Field(default=1, primary_key=True)
    enabled: bool = False
    guild_id: Optional[str] = None
    role_id: Optional[str] = None
    updated_at: datetime = Field(default_factory=utcnow)
