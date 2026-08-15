"""Discord-authenticated network access requests. PLAN.md §11C."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.auth import User, require_operator, require_viewer
from folia_mgmt.config import Settings
from folia_mgmt.db import get_session
from folia_mgmt.deps import settings_dependency
from folia_mgmt.discord import (
    DiscordError,
    build_authorize_url,
    exchange_code,
    get_current_user,
    get_guild_member_roles,
    resolve_minecraft_uuid,
)
from folia_mgmt.models import AccessRequest, AccessRequestStatus, utcnow

router = APIRouter(tags=["access-requests"])


class AuthorizeUrlResponse(BaseModel):
    authorize_url: str


@router.get("/auth/discord/login", response_model=AuthorizeUrlResponse)
def discord_login(
    minecraft_username: str, settings: Settings = Depends(settings_dependency)
) -> AuthorizeUrlResponse:
    """Frontend calls this first to get the URL to send the player to.
    `minecraft_username` rides through as `state` since Discord's redirect
    won't carry arbitrary extra query params back for us."""
    try:
        url = build_authorize_url(settings, state=minecraft_username)
    except DiscordError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc)) from exc
    return AuthorizeUrlResponse(authorize_url=url)


class AccessRequestResponse(BaseModel):
    id: int
    discord_user_id: str
    discord_username: str
    minecraft_username: str | None
    minecraft_uuid: str | None
    status: str
    auto_approved: bool = False


def _to_response(req: AccessRequest, auto_approved: bool = False) -> AccessRequestResponse:
    return AccessRequestResponse(
        id=req.id,
        discord_user_id=req.discord_user_id,
        discord_username=req.discord_username,
        minecraft_username=req.minecraft_username,
        minecraft_uuid=req.minecraft_uuid,
        status=req.status.value,
        auto_approved=auto_approved,
    )


@router.get("/auth/discord/callback", response_model=AccessRequestResponse)
def discord_callback(
    code: str,
    state: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dependency),
) -> AccessRequestResponse:
    minecraft_username = state
    try:
        access_token = exchange_code(settings, code)
        identity = get_current_user(access_token)
        roles = get_guild_member_roles(access_token, settings.discord_guild_id)
    except DiscordError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    existing = session.exec(
        select(AccessRequest).where(AccessRequest.discord_user_id == identity["id"])
    ).first()
    request = existing or AccessRequest(
        discord_user_id=identity["id"], discord_username=identity["username"]
    )
    request.discord_username = identity["username"]
    request.minecraft_username = minecraft_username
    request.minecraft_uuid = resolve_minecraft_uuid(minecraft_username)

    auto_approved = False
    if request.status == AccessRequestStatus.pending:
        if settings.discord_auto_approve_role_id and settings.discord_auto_approve_role_id in roles:
            request.status = AccessRequestStatus.approved
            request.decided_at = utcnow()
            auto_approved = True
            # TODO once §11B's whitelist wiring exists: insert into
            # network_access and the default LuckPerms group here.

    session.add(request)
    session.commit()
    session.refresh(request)
    return _to_response(request, auto_approved=auto_approved)


class ApprovedUuidsResponse(BaseModel):
    uuids: list[str]


@router.get(
    "/access-requests/approved-uuids",
    response_model=ApprovedUuidsResponse,
    dependencies=[Depends(require_viewer)],
)
def approved_uuids(session: Session = Depends(get_session)) -> ApprovedUuidsResponse:
    """Polled by the proxy's access-gate plugin (PLAN.md §11C) to decide
    who's allowed to connect at all. Viewer-role token is enough — this
    endpoint leaks no more than "these UUIDs are approved," unlike
    /access-requests below which includes Discord usernames and is
    operator-only."""
    approved = session.exec(
        select(AccessRequest).where(
            AccessRequest.status == AccessRequestStatus.approved,
            AccessRequest.minecraft_uuid.is_not(None),
        )
    ).all()
    return ApprovedUuidsResponse(uuids=[r.minecraft_uuid for r in approved])


@router.get("/access-requests", response_model=list[AccessRequestResponse], dependencies=[Depends(require_operator)])
def list_access_requests(
    status_filter: AccessRequestStatus | None = None, session: Session = Depends(get_session)
) -> list[AccessRequestResponse]:
    query = select(AccessRequest)
    if status_filter is not None:
        query = query.where(AccessRequest.status == status_filter)
    return [_to_response(r) for r in session.exec(query).all()]


@router.post(
    "/access-requests/{request_id}/approve",
    response_model=AccessRequestResponse,
    dependencies=[Depends(require_operator)],
)
def approve_access_request(
    request_id: int, user: User = Depends(require_operator), session: Session = Depends(get_session)
) -> AccessRequestResponse:
    request = session.get(AccessRequest, request_id)
    if request is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such access request")
    request.status = AccessRequestStatus.approved
    request.decided_at = utcnow()
    request.decided_by = user.id
    session.add(request)
    session.commit()
    session.refresh(request)
    return _to_response(request)


class DenyRequest(BaseModel):
    reason: str | None = None


@router.post(
    "/access-requests/{request_id}/deny",
    response_model=AccessRequestResponse,
    dependencies=[Depends(require_operator)],
)
def deny_access_request(
    request_id: int,
    body: DenyRequest,
    user: User = Depends(require_operator),
    session: Session = Depends(get_session),
) -> AccessRequestResponse:
    request = session.get(AccessRequest, request_id)
    if request is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such access request")
    request.status = AccessRequestStatus.denied
    request.decided_at = utcnow()
    request.decided_by = user.id
    request.deny_reason = body.reason
    session.add(request)
    session.commit()
    session.refresh(request)
    return _to_response(request)
