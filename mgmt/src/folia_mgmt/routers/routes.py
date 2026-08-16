"""Live routing table for folia-nexa-proxy. PLAN.md §7."""

from __future__ import annotations

import base64
import binascii
import struct

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.auth import require_operator, require_viewer, User
from folia_mgmt.config import Settings
from folia_mgmt.db import get_session
from folia_mgmt.deps import settings_dependency
from folia_mgmt.models import Host, ProxyDisplay, World, WorldPhase, WorldType, utcnow

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


class ForwardingSecretResponse(BaseModel):
    secret: str


@router.get(
    "/forwarding-secret", response_model=ForwardingSecretResponse, dependencies=[Depends(require_viewer)]
)
def get_forwarding_secret(settings: Settings = Depends(settings_dependency)) -> ForwardingSecretResponse:
    """The Velocity "modern" forwarding secret, shared with every world's
    paper-global.yml (scheduler.py's _build_instance_config) so backend
    servers trust identity forwarded by *this* proxy. Same viewer-role
    auth as the route table itself — the proxy's existing service account
    already has it."""
    return ForwardingSecretResponse(secret=settings.get_velocity_forwarding_secret())


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


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _validate_png_64x64(data: bytes) -> None:
    """folia-routes-sync hands icon_png_base64 straight to Velocity's
    Favicon, which requires an exact 64x64 PNG — reject anything else here
    rather than let a bad upload silently fail to render for players."""
    if data[:8] != _PNG_SIGNATURE or len(data) < 24 or data[12:16] != b"IHDR":
        raise ValueError("not a valid PNG file")
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != (64, 64):
        raise ValueError(f"icon must be exactly 64x64 pixels (got {width}x{height})")


class DisplayResponse(BaseModel):
    motd: str
    icon_base64: str | None = None


@router.get("/display", response_model=DisplayResponse, dependencies=[Depends(require_viewer)])
def get_display(session: Session = Depends(get_session)) -> DisplayResponse:
    """Polled by folia-routes-sync alongside the route table (PLAN.md §7)
    to set the proxy's server-list MOTD/icon live via ProxyPingEvent — no
    velocity.toml edit or proxy restart needed. Same viewer-role auth as
    the rest of this router."""
    display = session.get(ProxyDisplay, 1)
    if display is None:
        return DisplayResponse(motd=ProxyDisplay.model_fields["motd"].default)
    return DisplayResponse(motd=display.motd, icon_base64=display.icon_png_base64)


class UpdateDisplayRequest(BaseModel):
    motd: str
    icon_base64: str | None = None


@router.put("/display", response_model=DisplayResponse, dependencies=[Depends(require_operator)])
def update_display(
    body: UpdateDisplayRequest, user: User = Depends(require_operator), session: Session = Depends(get_session)
) -> DisplayResponse:
    icon_base64 = body.icon_base64
    if icon_base64:
        try:
            raw = base64.b64decode(icon_base64, validate=True)
        except binascii.Error as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "icon_base64 is not valid base64") from exc
        try:
            _validate_png_64x64(raw)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    display = session.get(ProxyDisplay, 1) or ProxyDisplay(id=1)
    display.motd = body.motd
    display.icon_png_base64 = icon_base64 or None
    display.updated_at = utcnow()
    session.add(display)
    session.commit()
    session.refresh(display)
    return DisplayResponse(motd=display.motd, icon_base64=display.icon_png_base64)
