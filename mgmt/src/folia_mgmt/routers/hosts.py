"""Host trust, enrollment, and listing. PLAN.md §3, §4, §10."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.auth import (
    User,
    consume_join_token,
    create_join_token,
    get_bearer_token_value,
    require_admin,
    require_operator,
    require_viewer,
)
from folia_mgmt.config import Settings
from folia_mgmt.db import get_session
from folia_mgmt.deps import get_lxd_client, settings_dependency
from folia_mgmt.lxd_client import LXDClient, LXDError
from folia_mgmt.models import Host, HostStatus, World, WorldPhase, utcnow

router = APIRouter(prefix="/hosts", tags=["hosts"])


class JoinTokenResponse(BaseModel):
    token: str
    expires_in_seconds: int


@router.post("/join-token", response_model=JoinTokenResponse)
def create_host_join_token(
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dependency),
) -> JoinTokenResponse:
    ttl = timedelta(seconds=settings.join_token_ttl_seconds)
    token = create_join_token(session, user, ttl)
    return JoinTokenResponse(token=token, expires_in_seconds=settings.join_token_ttl_seconds)


class EnrollCapacity(BaseModel):
    cpu_cores: int
    memory_gb: int


class EnrollRequest(BaseModel):
    name: str
    address: str
    project: str = "folia"
    lxd_trust_token: str
    capacity: EnrollCapacity
    labels: dict[str, str] = {}


class HostResponse(BaseModel):
    name: str
    address: str
    project: str
    status: str
    labels: dict[str, str]
    capacity_cpu_cores: int
    capacity_memory_gb: int
    allocated_cpu_cores: int
    allocated_memory_gb: int


def _to_response(host: Host, session: Session) -> HostResponse:
    placed = session.exec(
        select(World).where(
            World.host_name == host.name,
            World.phase.in_([WorldPhase.provisioning, WorldPhase.running]),
        )
    ).all()
    return HostResponse(
        name=host.name,
        address=host.address,
        project=host.project,
        status=host.status.value,
        labels=host.labels,
        capacity_cpu_cores=host.capacity_cpu_cores,
        capacity_memory_gb=host.capacity_memory_gb,
        allocated_cpu_cores=sum(w.cpu_cores for w in placed),
        allocated_memory_gb=sum(w.memory_gb for w in placed),
    )


@router.post("/enroll", response_model=HostResponse)
def enroll_host(
    body: EnrollRequest,
    raw_join_token: str = Depends(get_bearer_token_value),
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> HostResponse:
    """Consumed by tools/folia-host-join.sh — see PLAN.md §4.

    Auth here is the join token itself (Bearer), not an operator session/API
    token: whoever holds a valid join token is, by definition, authorized to
    enroll exactly one host.
    """
    join = consume_join_token(session, raw_join_token)

    if session.exec(select(Host).where(Host.name == body.name)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, f"host '{body.name}' already enrolled")
    if session.exec(select(Host).where(Host.address == body.address)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, f"a host at '{body.address}' is already enrolled")

    try:
        fingerprint, server_pem = lxd_client.redeem_trust_token(
            body.address, body.project, body.lxd_trust_token
        )
    except LXDError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not complete LXD trust handshake: {exc}") from exc

    join.used_at = utcnow()
    join.used_by_host = body.name

    host = Host(
        name=body.name,
        address=body.address,
        project=body.project,
        cert_fingerprint=fingerprint,
        server_cert_pem=server_pem,
        labels=body.labels,
        capacity_cpu_cores=body.capacity.cpu_cores,
        capacity_memory_gb=body.capacity.memory_gb,
        status=HostStatus.online,
    )
    session.add(host)
    session.add(join)
    session.commit()
    session.refresh(host)
    return _to_response(host, session)


@router.get("", response_model=list[HostResponse], dependencies=[Depends(require_viewer)])
def list_hosts(session: Session = Depends(get_session)) -> list[HostResponse]:
    hosts = session.exec(select(Host)).all()
    return [_to_response(h, session) for h in hosts]


@router.post("/{name}/drain", response_model=HostResponse, dependencies=[Depends(require_operator)])
def drain_host(name: str, session: Session = Depends(get_session)) -> HostResponse:
    host = session.exec(select(Host).where(Host.name == name)).first()
    if host is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such host '{name}'")
    host.status = HostStatus.draining
    session.add(host)
    session.commit()
    session.refresh(host)
    return _to_response(host, session)
