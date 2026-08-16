"""Live routing table for folia-nexa-proxy. PLAN.md §7."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.auth import require_viewer
from folia_mgmt.db import get_session
from folia_mgmt.models import Host, World, WorldPhase, WorldType

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
    domains: dict[str, str] = {}


def _pick_default(worlds: list[World]) -> str | None:
    """A `lobby` world is the landing point when one's running — that's
    the whole point of having one (PLAN.md §14B: players land there and
    pick a game, rather than dropping straight into an overworld). Falls
    back to an `overworld` for clusters that don't run a lobby world at
    all. Either way, ties break on name for a stable, deterministic pick
    rather than depending on DB row order — worth promoting to an
    explicit `World.is_default` flag if that's ever not enough."""
    lobbies = sorted((w.name for w in worlds if w.type == WorldType.lobby))
    if lobbies:
        return lobbies[0]
    overworlds = sorted((w.name for w in worlds if w.type == WorldType.overworld))
    return overworlds[0] if overworlds else None


def _domain_routes(routable: list[World], hosts: list[Host]) -> dict[str, str]:
    """Domain -> world name, one entry per (host, domain) pair, for hosts
    that declare public hostnames (PLAN.md §7C). Reuses `_pick_default`
    scoped to just that host's routable worlds, so a domain always lands on
    that host's own lobby (or overworld) — the same hub logic §14B already
    uses globally, just applied once per host instead of once per cluster.
    A host with no running lobby/overworld is silently omitted; there's
    nothing sensible to route its domain(s) to yet."""
    result: dict[str, str] = {}
    for host in hosts:
        if not host.domains:
            continue
        host_worlds = [w for w in routable if w.host_name == host.name]
        default = _pick_default(host_worlds)
        if default is None:
            continue
        for domain in host.domains:
            result[domain] = default
    return result


@router.get("", response_model=RoutesResponse, dependencies=[Depends(require_viewer)])
def get_routes(session: Session = Depends(get_session)) -> RoutesResponse:
    worlds = session.exec(
        select(World).where(World.phase == WorldPhase.running, World.address.is_not(None))
    ).all()
    routable = [w for w in worlds if w.type not in _NON_ROUTABLE]
    default_world = _pick_default(routable)
    routes = [
        Route(world=w.name, type=w.type.value, address=w.address, default=(w.name == default_world))
        for w in routable
    ]
    hosts = session.exec(select(Host)).all()
    return RoutesResponse(routes=routes, domains=_domain_routes(routable, hosts))
