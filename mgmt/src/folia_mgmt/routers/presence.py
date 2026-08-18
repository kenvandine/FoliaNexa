"""Live per-world player presence, reported by folia-routes-sync (the
Velocity proxy plugin) from `RegisteredServer.getPlayersConnected()` — the
one place in this cluster that actually knows who's connected to which
backend right now (node's own health agent explicitly doesn't yet, see
`node/src/folia_node/health.py`'s module docstring). PLAN.md §7A.

Reported on the proxy's existing poll cycle (`FOLIA_ROUTES_POLL_SECONDS`,
default 5s, `FoliaRoutesSyncPlugin.pollAndReconcile`) as a full snapshot —
each report replaces the previous player list for every world it
mentions, there's no delta/join-quit tracking. Same viewer-role auth as
`chat/report` — the proxy's service account only ever holds viewer
(CLAUDE.md Phase 7), and this is a report, not a privileged config write.
Public reads live in public_stats.py, same split as chat/stats above.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from folia_mgmt.auth import require_viewer
from folia_mgmt.db import get_session
from folia_mgmt.models import WorldPresence, utcnow

router = APIRouter(prefix="/presence", tags=["presence"])


class PresencePlayer(BaseModel):
    uuid: str
    username: str


class WorldPresenceReport(BaseModel):
    world: str
    players: list[PresencePlayer] = []


class ReportPresenceRequest(BaseModel):
    worlds: list[WorldPresenceReport]


@router.post("/report", dependencies=[Depends(require_viewer)])
def report_presence(body: ReportPresenceRequest, session: Session = Depends(get_session)) -> dict[str, int]:
    for report in body.worlds:
        presence = session.get(WorldPresence, report.world) or WorldPresence(world_name=report.world)
        presence.players = [p.model_dump() for p in report.players]
        presence.updated_at = utcnow()
        session.add(presence)
    session.commit()
    return {"worlds_updated": len(body.worlds)}
