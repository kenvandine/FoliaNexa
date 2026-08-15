"""Player stat ingestion from the in-house stats plugin. PLAN.md §7A.

The plugin (softdepends on AuraSkills/AxAuctions for a couple of extra
stat_keys — see its own repo, catalog id `FoliaNexaStats`) batches counters
locally and POSTs here periodically via Bukkit's AsyncScheduler, never per
event and never from a game-tick thread (docs/plugin-dev/02-plugin-
architecture.md). Public reads of this data live in public_stats.py —
kept as a separate router/prefix so the two can have very different auth
and caching behavior.
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
    # Current running totals, keyed by stat_key (e.g. "kills": 42) — the
    # plugin is the source of truth for its own counters; mgmt just mirrors
    # the latest value on each report rather than trying to reconcile deltas.
    stats: dict[str, float] = {}
    # date ("YYYY-MM-DD", UTC) -> seconds played *since the last report*,
    # added to that day's running total rather than replacing it.
    playtime_daily: dict[str, int] = {}


class ReportStatsRequest(BaseModel):
    players: list[PlayerStatsReport]


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

        for stat_key, value in report.stats.items():
            stat = session.exec(
                select(PlayerStat).where(
                    PlayerStat.player_uuid == report.uuid, PlayerStat.stat_key == stat_key
                )
            ).first()
            if stat is None:
                stat = PlayerStat(player_uuid=report.uuid, stat_key=stat_key, value=value)
            else:
                stat.value = value
            stat.updated_at = utcnow()
            session.add(stat)

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
