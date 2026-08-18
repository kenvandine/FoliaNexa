"""Public, unauthenticated read API for the player-hub portal (`portal/`).
PLAN.md §7A.

Deliberately unauthenticated, same rationale as the existing
`/worlds/{name}/plugins-manifest` endpoint: everything returned here is
already meant to be public (leaderboards, player profiles). This is the
first mgmt surface designed to take real internet traffic rather than
LAN/operator traffic, so every route here goes through an in-process TTL
cache and a basic per-IP rate limit — see `Settings.public_api_cache_seconds`
/ `public_api_rate_limit_per_minute`. The VPS edge's Caddy is expected to
add a second layer of rate limiting in front of this; this one is mgmt's
own defense in depth, not a substitute.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Callable, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.avatar import get_player_avatar
from folia_mgmt.config import Settings
from folia_mgmt.db import get_session
from folia_mgmt.deps import settings_dependency
from folia_mgmt.models import (
    PlayerPlaytimeDaily,
    PlayerProfile,
    PlayerStat,
    World,
    WorldPhase,
    WorldPresence,
    WorldType,
    utcnow,
)

# Same "back-of-house, never player-facing" set as routers/routes.py's
# _NON_ROUTABLE — a staging/infra world should never show up as an
# "online" world on the public portal either, duplicated locally rather
# than importing routes.py's private constant across router modules.
_NON_ROUTABLE = {WorldType.staging, WorldType.infra}

# Longer-lived than the leaderboard/profile cache — skins change rarely
# (a player editing their Mojang skin), so there's no reason to re-fetch
# and re-render on the same ~short TTL as stats that update every minute.
AVATAR_CACHE_SECONDS = 3600

T = TypeVar("T")


class TTLCache:
    """Tiny in-process cache — fine at this scale (SQLite, one process, a
    handful of leaderboard/profile queries), no need for Redis. Lives on
    `app.state` (one instance per FastAPI app, created in
    `main.create_app()`) rather than at module scope, so it doesn't leak
    across separately-created app instances — tests each get a fresh one
    the same way they get a fresh DB."""

    def __init__(self) -> None:
        self._store: dict[tuple, tuple[float, object]] = {}

    def get_or_set(self, key: tuple, ttl: float, compute: Callable[[], T]) -> T:
        now = time.monotonic()
        cached = self._store.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]  # type: ignore[return-value]
        value = compute()
        self._store[key] = (now + ttl, value)
        return value


class RateLimiter:
    """Fixed-window per-IP counter. Good enough to blunt a single bad actor
    or a runaway script; not a substitute for real edge rate limiting.
    Same per-app-instance lifecycle as TTLCache, for the same reason."""

    def __init__(self) -> None:
        self._counters: dict[str, tuple[int, float]] = {}

    def check(self, ip: str, limit_per_minute: int) -> None:
        if limit_per_minute <= 0:
            return
        now = time.monotonic()
        count, window_start = self._counters.get(ip, (0, now))
        if now - window_start >= 60:
            count, window_start = 0, now
        count += 1
        self._counters[ip] = (count, window_start)
        if count > limit_per_minute:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded, try again shortly")


def _get_cache(request: Request) -> TTLCache:
    return request.app.state.public_stats_cache


def _enforce_rate_limit(request: Request, settings: Settings = Depends(settings_dependency)) -> None:
    limiter: RateLimiter = request.app.state.public_stats_rate_limiter
    client_ip = request.client.host if request.client else "unknown"
    limiter.check(client_ip, settings.public_api_rate_limit_per_minute)


router = APIRouter(prefix="/public", tags=["public-stats"], dependencies=[Depends(_enforce_rate_limit)])


class LeaderboardEntry(BaseModel):
    uuid: str
    username: str
    value: float


class LeaderboardResponse(BaseModel):
    stat: str
    entries: list[LeaderboardEntry]


@router.get("/leaderboards", response_model=LeaderboardResponse)
def leaderboards(
    stat: str,
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dependency),
    cache: TTLCache = Depends(_get_cache),
) -> LeaderboardResponse:
    def compute() -> LeaderboardResponse:
        rows = session.exec(
            select(PlayerStat, PlayerProfile)
            .where(PlayerStat.stat_key == stat, PlayerStat.player_uuid == PlayerProfile.uuid)
            .order_by(PlayerStat.value.desc())
            .limit(limit)
        ).all()
        return LeaderboardResponse(
            stat=stat,
            entries=[
                LeaderboardEntry(uuid=profile.uuid, username=profile.username, value=player_stat.value)
                for player_stat, profile in rows
            ],
        )

    return cache.get_or_set(("leaderboards", stat, limit), settings.public_api_cache_seconds, compute)


class OnlinePlayer(BaseModel):
    uuid: str
    username: str


class WorldOnline(BaseModel):
    world: str
    type: str
    player_count: int
    players: list[OnlinePlayer]


class WorldsOnlineResponse(BaseModel):
    worlds: list[WorldOnline]
    total_players: int


@router.get("/worlds", response_model=WorldsOnlineResponse)
def worlds_online(
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dependency),
    cache: TTLCache = Depends(_get_cache),
) -> WorldsOnlineResponse:
    """Who's online, per world — fed by folia-routes-sync's presence
    reports (routers/presence.py), same near-real-time-but-not-exact
    caveat as the rest of this file's data (PLAN.md §7A: no WebSocket
    live-push layer). A world with no fresh WorldPresence row (never
    reported, or its last report is older than
    `public_presence_stale_seconds`) is listed with zero players rather
    than omitted, so an empty world still shows up as "0 online" instead
    of silently vanishing from the page."""

    def compute() -> WorldsOnlineResponse:
        worlds = session.exec(select(World).where(World.phase == WorldPhase.running)).all()
        routable = sorted((w for w in worlds if w.type not in _NON_ROUTABLE), key=lambda w: w.name)
        stale_cutoff = utcnow() - timedelta(seconds=settings.public_presence_stale_seconds)

        entries = []
        total_players = 0
        for w in routable:
            presence = session.get(WorldPresence, w.name)
            players: list[OnlinePlayer] = []
            if presence is not None and presence.updated_at >= stale_cutoff:
                players = [OnlinePlayer(**p) for p in presence.players]
            entries.append(WorldOnline(world=w.name, type=w.type.value, player_count=len(players), players=players))
            total_players += len(players)

        return WorldsOnlineResponse(worlds=entries, total_players=total_players)

    return cache.get_or_set(("worlds-online",), settings.public_api_cache_seconds, compute)


class PlayerSummary(BaseModel):
    uuid: str
    username: str
    last_seen: datetime


class PlayersResponse(BaseModel):
    players: list[PlayerSummary]


@router.get("/players", response_model=PlayersResponse)
def list_players(
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dependency),
    cache: TTLCache = Depends(_get_cache),
) -> PlayersResponse:
    def compute() -> PlayersResponse:
        rows = session.exec(select(PlayerProfile).order_by(PlayerProfile.last_seen.desc()).limit(limit)).all()
        return PlayersResponse(
            players=[PlayerSummary(uuid=p.uuid, username=p.username, last_seen=p.last_seen) for p in rows]
        )

    return cache.get_or_set(("players", limit), settings.public_api_cache_seconds, compute)


class PlaytimeDayEntry(BaseModel):
    date: str
    seconds: int


class PlayerProfileResponse(BaseModel):
    uuid: str
    username: str
    first_seen: datetime
    last_seen: datetime
    stats: dict[str, float]
    playtime_daily: list[PlaytimeDayEntry]


@router.get("/players/{uuid}", response_model=PlayerProfileResponse)
def get_player(
    uuid: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dependency),
    cache: TTLCache = Depends(_get_cache),
) -> PlayerProfileResponse:
    def compute() -> PlayerProfileResponse:
        profile = session.exec(select(PlayerProfile).where(PlayerProfile.uuid == uuid)).first()
        if profile is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such player '{uuid}'")
        stats = session.exec(select(PlayerStat).where(PlayerStat.player_uuid == uuid)).all()
        daily = session.exec(
            select(PlayerPlaytimeDaily)
            .where(PlayerPlaytimeDaily.player_uuid == uuid)
            .order_by(PlayerPlaytimeDaily.date)
        ).all()
        return PlayerProfileResponse(
            uuid=profile.uuid,
            username=profile.username,
            first_seen=profile.first_seen,
            last_seen=profile.last_seen,
            stats={s.stat_key: s.value for s in stats},
            playtime_daily=[PlaytimeDayEntry(date=d.date, seconds=d.seconds) for d in daily],
        )

    # Not cached on the 404 path — get_or_set only stores a result once
    # compute() returns normally, so a player who shows up moments later
    # isn't stuck behind a stale miss for the rest of the TTL window.
    return cache.get_or_set(("player", uuid), settings.public_api_cache_seconds, compute)


@router.get("/players/{uuid}/avatar")
def get_player_avatar_route(
    uuid: str,
    size: int = Query(default=64, ge=8, le=256),
    cache: TTLCache = Depends(_get_cache),
) -> Response:
    # get_player_avatar never raises (falls back to a placeholder PNG
    # internally) — see avatar.py — so there's no error path to handle
    # here beyond what TTLCache.get_or_set already does.
    png_bytes = cache.get_or_set(
        ("avatar", uuid, size), AVATAR_CACHE_SECONDS, lambda: get_player_avatar(uuid, size=size)
    )
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": f"public, max-age={AVATAR_CACHE_SECONDS}"},
    )
