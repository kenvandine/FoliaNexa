"""Live routing table for velocity-proxy. PLAN.md §7."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.auth import require_viewer
from folia_mgmt.db import get_session
from folia_mgmt.models import World, WorldPhase, WorldType

router = APIRouter(prefix="/routes", tags=["routes"])

# Worlds of these types are back-of-house — never exposed to the proxy's
# routing table even once "running".
_NON_ROUTABLE = {WorldType.staging, WorldType.infra}


class Route(BaseModel):
    world: str
    type: str
    address: str
    default: bool = False


class RoutesResponse(BaseModel):
    routes: list[Route]


@router.get("", response_model=RoutesResponse, dependencies=[Depends(require_viewer)])
def get_routes(session: Session = Depends(get_session)) -> RoutesResponse:
    worlds = session.exec(
        select(World).where(World.phase == WorldPhase.running, World.address.is_not(None))
    ).all()
    routes = [
        Route(
            world=w.name,
            type=w.type.value,
            address=w.address,
            # First overworld is the fallback landing point, matching the
            # example in PLAN.md §7. Worth promoting to an explicit
            # World.is_default flag if more than one ever needs to compete
            # for this.
            default=(w.type == WorldType.overworld),
        )
        for w in worlds
        if w.type not in _NON_ROUTABLE
    ]
    return RoutesResponse(routes=routes)
