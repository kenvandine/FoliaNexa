"""Player stat ingestion from the in-house stats plugin. PLAN.md §7A.

The plugin (softdepends on AuraSkills/AxAuctions for a couple of extra
stat_keys — see its own repo, catalog id `FoliaNexaStats`) batches counters
locally and POSTs here periodically via Bukkit's AsyncScheduler, never per
event and never from a game-tick thread (docs/plugin-dev/02-plugin-
architecture.md). Public reads of this data live in public_stats.py —
kept as a separate router/prefix so the two can have very different auth
and caching behavior.

`stat_deltas` are summed into a running total here rather than treated as
absolute totals (`stat.value += delta`, not `=`) — an earlier design had
each report carry what the plugin believed was the player's whole total,
which mgmt just mirrored on every report. That broke the moment a player
was tracked by more than one world at once: `FoliaNexaStats` is
`default_for_all_worlds: true`, so that's the common case (any hub-and-
spoke cluster), not an edge case — two worlds' independent "this is the
real total" reports just clobbered each other every cycle, confirmed live
as visibly flickering public stats (kills/deaths/blocks_mined/
playtime_seconds_total swinging between two different worlds' numbers on
repeated requests). Summing deltas is correct regardless of how many
worlds are simultaneously reporting for the same player — the same shape
`playtime_daily` below already had all along (`+=`, never `=`).

`gauges` are the opposite: a point-in-time reading (AuraSkills power
level, AxAuctions wealth) that isn't cumulative at all, so those are
still overwritten with the latest value on every report, same as before.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.auth import require_operator
from folia_mgmt.db import get_session
from folia_mgmt.models import PlayerPlaytimeDaily, PlayerProfile, PlayerStat, utcnow

router = APIRouter(prefix="/stats", tags=["stats"])


class PlayerStatsReport(BaseModel):
    uuid: str
    username: str
    # How much each stat_key (e.g. "kills") increased since the plugin's
    # last successfully-delivered report — summed into mgmt's running
    # total, never treated as the whole truth on its own. See this
    # module's docstring for why deltas, not absolute totals.
    stat_deltas: dict[str, float] = {}
    # Point-in-time readings (e.g. "auraskills_power_level") — not
    # cumulative, so these overwrite rather than sum.
    gauges: dict[str, float] = {}
    # date ("YYYY-MM-DD", UTC) -> seconds played *since the last report*,
    # added to that day's running total rather than replacing it.
    playtime_daily: dict[str, int] = {}


class ReportStatsRequest(BaseModel):
    players: list[PlayerStatsReport]


def _upsert_stat(session: Session, uuid: str, stat_key: str, *, delta: float | None = None, value: float | None = None) -> None:
    stat = session.exec(
        select(PlayerStat).where(PlayerStat.player_uuid == uuid, PlayerStat.stat_key == stat_key)
    ).first()
    if delta is not None:
        if stat is None:
            stat = PlayerStat(player_uuid=uuid, stat_key=stat_key, value=delta)
        else:
            stat.value += delta
    else:
        if stat is None:
            stat = PlayerStat(player_uuid=uuid, stat_key=stat_key, value=value)
        else:
            stat.value = value
    stat.updated_at = utcnow()
    session.add(stat)


@router.post("/report", dependencies=[Depends(require_operator)])
def report_stats(body: ReportStatsRequest, session: Session = Depends(get_session)) -> dict[str, int]:
    for report in body.players:
        profile = session.exec(select(PlayerProfile).where(PlayerProfile.uuid == report.uuid)).first()
        if profile is None:
            profile = PlayerProfile(uuid=report.uuid, username=report.username)
        else:
            profile.username = report.username
        profile.last_seen = utcnow()
        session.add(profile)

        for stat_key, delta in report.stat_deltas.items():
            _upsert_stat(session, report.uuid, stat_key, delta=delta)

        for stat_key, value in report.gauges.items():
            _upsert_stat(session, report.uuid, stat_key, value=value)

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
