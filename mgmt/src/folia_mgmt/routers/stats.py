"""Player stat ingestion from the in-house stats plugin. PLAN.md §7A.

The plugin (softdepends on AuraSkills/AxAuctions for a couple of extra
stat_keys — see its own repo, catalog id `FoliaNexaStats`) batches counters
locally and POSTs here periodically via Bukkit's AsyncScheduler, never per
event and never from a game-tick thread (docs/plugin-dev/02-plugin-
architecture.md). Public reads of this data live in public_stats.py —
kept as a separate router/prefix so the two can have very different auth
and caching behavior.

## Two report shapes, same endpoint

`FoliaNexaStats` is `default_for_all_worlds: true`, so every world a
player has ever visited reports independently for them. An earlier
design had each report carry `stats: {...}` — what the plugin believed
was the player's whole current total — which mgmt just mirrored on every
report, with no per-world attribution in storage at all. That broke the
moment a player was tracked by more than one world at once (the common
case, not an edge case): two worlds' independent "this is the real
total" reports just clobbered each other every cycle, confirmed live as
visibly flickering public stats.

The fix has two parts, both handled in this one endpoint so an operator
never has to upgrade every world's plugin in lockstep:

- **New-format reports** (`stat_deltas`/`gauges`, `world` set) — the
  current plugin build. `stat_deltas` is how much a counter increased
  since that plugin's last *successfully delivered* report, summed
  (`+=`) into a `PlayerStat` row scoped to `(uuid, world, stat_key)` —
  see `PlayerStat`'s own docstring for why per-world storage is what
  actually fixes the clobbering. `gauges` are point-in-time readings
  (AuraSkills power level) rather than counters, so those are stored
  under the fixed `world_name=""` bucket and overwritten (`=`), not
  world-scoped or summed — see `PlayerStat`'s docstring for why that
  still reads back correctly through the same SUM() public_stats.py
  uses for everything else.
- **Legacy reports** (`stats` set, the old absolute-total shape) — from
  a world whose plugin hasn't been upgraded yet. Written into the
  `world_name="__legacy__"` bucket with the old overwrite (`=`)
  semantics, unchanged from before this file grew a world dimension.
  Worlds still on this path can still clobber *each other* the old way,
  but never touch an upgraded world's own clean row — so the combined
  public total converges to fully correct as each world upgrades,
  rather than requiring them all to upgrade at once.

`playtime_daily` doesn't need any of this — it was already reported (and
stored, `+=`) as a delta-since-last-report in both formats.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.auth import require_operator
from folia_mgmt.db import get_session
from folia_mgmt.models import PlayerPlaytimeDaily, PlayerProfile, PlayerStat, utcnow

router = APIRouter(prefix="/stats", tags=["stats"])

LEGACY_WORLD_NAME = "__legacy__"
GAUGE_WORLD_NAME = ""


class PlayerStatsReport(BaseModel):
    uuid: str
    username: str
    # Legacy shape (pre-per-world plugin builds): each value is the
    # plugin's belief about the player's whole current total for that
    # stat_key. Only ever sent by a not-yet-upgraded plugin — see this
    # module's docstring.
    stats: dict[str, float] = {}
    # Current shape: how much each stat_key increased since the last
    # successfully-delivered report, summed into mgmt's running total for
    # this specific world (see ReportStatsRequest.world).
    stat_deltas: dict[str, float] = {}
    # Point-in-time readings (e.g. "auraskills_power_level") — not
    # cumulative and not world-scoped, so these overwrite rather than sum.
    gauges: dict[str, float] = {}
    # date ("YYYY-MM-DD", UTC) -> seconds played *since the last report*,
    # added to that day's running total rather than replacing it.
    playtime_daily: dict[str, int] = {}


class ReportStatsRequest(BaseModel):
    # Which world this whole request's reports came from — one JVM/world
    # per request, so this lives at the top level, not per-player. Unset
    # for a legacy (pre-per-world) plugin build, which doesn't know this
    # field exists.
    world: str | None = None
    players: list[PlayerStatsReport]


def _upsert_stat(session: Session, uuid: str, world_name: str, stat_key: str, *, delta: float | None = None, value: float | None = None) -> None:
    stat = session.exec(
        select(PlayerStat).where(
            PlayerStat.player_uuid == uuid, PlayerStat.world_name == world_name, PlayerStat.stat_key == stat_key
        )
    ).first()
    if delta is not None:
        if stat is None:
            stat = PlayerStat(player_uuid=uuid, world_name=world_name, stat_key=stat_key, value=delta)
        else:
            stat.value += delta
    else:
        if stat is None:
            stat = PlayerStat(player_uuid=uuid, world_name=world_name, stat_key=stat_key, value=value)
        else:
            stat.value = value
    stat.updated_at = utcnow()
    session.add(stat)


@router.post("/report", dependencies=[Depends(require_operator)])
def report_stats(body: ReportStatsRequest, session: Session = Depends(get_session)) -> dict[str, int]:
    # A new-format plugin always sends `world` (falling back to its own
    # sentinel if the env var it reads it from is somehow unset — see the
    # plugin's own docs); this is only None for a genuinely legacy
    # request, which never populates stat_deltas/gauges in the first
    # place, so this fallback is never actually reached for those.
    world_name = body.world or LEGACY_WORLD_NAME

    for report in body.players:
        profile = session.exec(select(PlayerProfile).where(PlayerProfile.uuid == report.uuid)).first()
        if profile is None:
            profile = PlayerProfile(uuid=report.uuid, username=report.username)
        else:
            profile.username = report.username
        profile.last_seen = utcnow()
        session.add(profile)

        for stat_key, value in report.stats.items():
            _upsert_stat(session, report.uuid, LEGACY_WORLD_NAME, stat_key, value=value)

        for stat_key, delta in report.stat_deltas.items():
            _upsert_stat(session, report.uuid, world_name, stat_key, delta=delta)

        for stat_key, value in report.gauges.items():
            _upsert_stat(session, report.uuid, GAUGE_WORLD_NAME, stat_key, value=value)

        for date, seconds in report.playtime_daily.items():
            daily = session.exec(
                select(PlayerPlaytimeDaily).where(
                    PlayerPlaytimeDaily.player_uuid == report.uuid, PlayerPlaytimeDaily.date == date
                )
            ).first()
            if daily is None:
                daily = PlayerPlaytimeDaily(player_uuid=report.uuid, date=date, seconds=seconds)
            else:
                daily.seconds += seconds
            session.add(daily)

    session.commit()
    return {"players_updated": len(body.players)}
