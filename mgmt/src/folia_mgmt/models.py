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


class Host(SQLModel, table=True):
    """A trusted LXD remote. See PLAN.md §2 (Host) and §3 (trust model)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    address: str  # host:port of the LXD remote API
    project: str = "folia"
    cert_fingerprint: Optional[str] = None
    server_cert_pem: Optional[str] = None  # pinned at enrollment time, PLAN.md §3
    labels: dict = Field(default_factory=dict, sa_column=Column(JSON))
    # Public hostnames that route to this host's default world (lobby, else
    # overworld) via the proxy's virtual-host lookup, PLAN.md §7C. Stored
    # lowercased; uniqueness across hosts is enforced in routers/hosts.py,
    # not at the DB level.
    domains: list[str] = Field(default_factory=list, sa_column=Column(JSON))
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


class AccessRequest(SQLModel, table=True):
    """A Discord-authenticated request to join the network. PLAN.md §11C."""

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
