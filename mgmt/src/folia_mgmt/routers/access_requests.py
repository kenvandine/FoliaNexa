"""Discord-authenticated network access requests. PLAN.md §11C."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.auth import User, require_operator, require_viewer
from folia_mgmt.config import Settings
from folia_mgmt.db import get_session
from folia_mgmt.deps import get_lxd_client, settings_dependency
from folia_mgmt.discord import (
    DiscordError,
    build_authorize_url,
    exchange_code,
    get_current_user,
    get_guild_member_roles,
    resolve_minecraft_uuid,
)
from folia_mgmt.lxd_client import LXDClient
from folia_mgmt.models import AccessRequest, AccessRequestStatus, DiscordAccessGateConfig, utcnow
from folia_mgmt.scheduler import sync_whitelisted_worlds

router = APIRouter(tags=["access-requests"])


def _get_gate_config(session: Session) -> DiscordAccessGateConfig:
    return session.get(DiscordAccessGateConfig, 1) or DiscordAccessGateConfig(id=1)


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
    auto_managed: bool = True


def _to_response(req: AccessRequest, auto_approved: bool = False) -> AccessRequestResponse:
    return AccessRequestResponse(
        id=req.id,
        discord_user_id=req.discord_user_id,
        discord_username=req.discord_username,
        minecraft_username=req.minecraft_username,
        minecraft_uuid=req.minecraft_uuid,
        status=req.status.value,
        auto_approved=auto_approved,
        auto_managed=req.auto_managed,
    )


@router.get("/auth/discord/callback", response_model=AccessRequestResponse)
def discord_callback(
    code: str,
    state: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dependency),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> AccessRequestResponse:
    minecraft_username = state
    gate_config = _get_gate_config(session)
    try:
        access_token = exchange_code(settings, code)
        identity = get_current_user(access_token)
        roles = get_guild_member_roles(access_token, gate_config.guild_id) if gate_config.guild_id else []
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
        if gate_config.enabled and gate_config.role_id and gate_config.role_id in roles:
            request.status = AccessRequestStatus.approved
            request.decided_at = utcnow()
            auto_approved = True

    session.add(request)
    session.commit()
    session.refresh(request)
    if auto_approved:
        sync_whitelisted_worlds(session, lxd_client)
    return _to_response(request, auto_approved=auto_approved)


class GateConfigResponse(BaseModel):
    enabled: bool
    guild_id: str | None
    role_id: str | None


@router.get(
    "/access-requests/discord-gate-config",
    response_model=GateConfigResponse,
    dependencies=[Depends(require_viewer)],
)
def get_gate_config(session: Session = Depends(get_session)) -> GateConfigResponse:
    """Viewer-role is enough — a guild/role id isn't sensitive on its own.
    Polled by folia-nexa-bot to know what to watch for role-sync."""
    c = _get_gate_config(session)
    return GateConfigResponse(enabled=c.enabled, guild_id=c.guild_id, role_id=c.role_id)


class UpdateGateConfigRequest(BaseModel):
    enabled: bool
    guild_id: str | None = None
    role_id: str | None = None


@router.put(
    "/access-requests/discord-gate-config",
    response_model=GateConfigResponse,
    dependencies=[Depends(require_operator)],
)
def update_gate_config(
    body: UpdateGateConfigRequest, session: Session = Depends(get_session)
) -> GateConfigResponse:
    c = _get_gate_config(session)
    c.enabled = body.enabled
    c.guild_id = body.guild_id
    c.role_id = body.role_id
    c.updated_at = utcnow()
    session.add(c)
    session.commit()
    session.refresh(c)
    return GateConfigResponse(enabled=c.enabled, guild_id=c.guild_id, role_id=c.role_id)


class CreateAccessRequest(BaseModel):
    discord_user_id: str
    discord_username: str
    minecraft_username: str
    minecraft_uuid: str | None = None
    auto_approve: bool = False
    auto_managed: bool = True
    # False for the dashboard's manual (non-Discord) allowlist entries —
    # keeps them permanently exempt from POST /access-requests/role-sync,
    # which would otherwise revoke them the moment it runs (their
    # discord_user_id, e.g. "manual:<username>", will never appear in a
    # real Discord role-holder list). The bot's real /request-access flow
    # doesn't send this, so it keeps the default (True, role-sync-managed).


@router.post(
    "/access-requests",
    response_model=AccessRequestResponse,
    dependencies=[Depends(require_operator)],
)
def create_access_request(
    body: CreateAccessRequest,
    user: User = Depends(require_operator),
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> AccessRequestResponse:
    """The in-Discord counterpart to `GET /auth/discord/callback` — used by
    folia-nexa-bot's `/request-access` command (PLAN.md §16) for
    players who'd rather not leave Discord for the web OAuth flow. Same
    upsert-by-discord-user-id behavior; `auto_approve` is trusted from the
    caller since reaching this endpoint at all already requires an
    operator-role token (the bot decided auto-approval locally from the
    inviting member's roles — see folia_bot.access.decide_auto_approve).
    Also used by the dashboard's manual-allowlist form, with
    `auto_managed=False`.

    `minecraft_uuid`, if supplied, is stored as-is instead of resolving
    `minecraft_username` through Mojang — the path for Bedrock players
    (PLAN.md §7B), whose Floodgate-assigned UUID the Mojang API has no
    way to look up from a gamertag."""
    existing = session.exec(
        select(AccessRequest).where(AccessRequest.discord_user_id == body.discord_user_id)
    ).first()
    request = existing or AccessRequest(discord_user_id=body.discord_user_id, discord_username=body.discord_username)
    request.discord_username = body.discord_username
    request.minecraft_username = body.minecraft_username
    request.minecraft_uuid = body.minecraft_uuid or resolve_minecraft_uuid(body.minecraft_username)
    # Only ever move auto_managed False->True here if the row is brand
    # new or was never human-touched. Once approve/deny has set it False
    # (sticky), a routine repeat call (e.g. the same player running
    # /request-access again) must not silently re-enable role-sync
    # management for them — that's exactly the "needs an explicit human
    # re-decision" invariant this whole flag exists to preserve.
    if existing is None or existing.auto_managed:
        request.auto_managed = body.auto_managed

    auto_approved = False
    if request.status == AccessRequestStatus.pending and body.auto_approve:
        request.status = AccessRequestStatus.approved
        request.decided_at = utcnow()
        request.decided_by = user.id
        auto_approved = True

    session.add(request)
    session.commit()
    session.refresh(request)
    if auto_approved:
        sync_whitelisted_worlds(session, lxd_client)
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


class RoleSyncRequest(BaseModel):
    discord_user_ids_with_role: list[str]


class RoleSyncResponse(BaseModel):
    approved: list[str]
    revoked: list[str]


@router.post(
    "/access-requests/role-sync",
    response_model=RoleSyncResponse,
    dependencies=[Depends(require_operator)],
)
def role_sync(
    body: RoleSyncRequest,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> RoleSyncResponse:
    """Reconciles AccessRequest.status against the *complete current*
    membership of the configured Discord allowlist role (PLAN.md §11C).
    Called by folia-nexa-bot both on role-change gateway events and on a
    periodic safety-net timer. Only ever touches auto_managed=True rows —
    an operator's explicit approve/deny (or a manually-added allowlist
    entry) is sticky until they act again, this endpoint never overrides
    it. Rows with no existing AccessRequest are skipped entirely: this
    manages known requesters only, initial linking still requires
    /request-access or the OAuth flow once. No-ops if the gate is
    currently disabled, so a stale bot call made just after an operator
    flips it off in the dashboard can't revoke anyone."""
    config = _get_gate_config(session)
    if not config.enabled:
        return RoleSyncResponse(approved=[], revoked=[])

    role_holders = set(body.discord_user_ids_with_role)
    approved_ids: list[str] = []
    revoked_ids: list[str] = []

    managed = session.exec(select(AccessRequest).where(AccessRequest.auto_managed.is_(True))).all()
    for request in managed:
        has_role = request.discord_user_id in role_holders
        if has_role and request.status != AccessRequestStatus.approved:
            request.status = AccessRequestStatus.approved
            request.decided_at = utcnow()
            request.decided_by = None
            request.deny_reason = None
            approved_ids.append(request.discord_user_id)
            session.add(request)
        elif not has_role and request.status == AccessRequestStatus.approved:
            request.status = AccessRequestStatus.revoked
            request.decided_at = utcnow()
            request.decided_by = None
            request.deny_reason = "Discord role removed"
            revoked_ids.append(request.discord_user_id)
            session.add(request)

    session.commit()
    if approved_ids or revoked_ids:
        sync_whitelisted_worlds(session, lxd_client)
    return RoleSyncResponse(approved=approved_ids, revoked=revoked_ids)


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
    request_id: int,
    user: User = Depends(require_operator),
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> AccessRequestResponse:
    request = session.get(AccessRequest, request_id)
    if request is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such access request")
    request.status = AccessRequestStatus.approved
    request.decided_at = utcnow()
    request.decided_by = user.id
    request.auto_managed = False
    session.add(request)
    session.commit()
    session.refresh(request)
    sync_whitelisted_worlds(session, lxd_client)
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
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> AccessRequestResponse:
    request = session.get(AccessRequest, request_id)
    if request is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such access request")
    request.status = AccessRequestStatus.denied
    request.decided_at = utcnow()
    request.decided_by = user.id
    request.deny_reason = body.reason
    request.auto_managed = False
    session.add(request)
    session.commit()
    session.refresh(request)
    sync_whitelisted_worlds(session, lxd_client)
    return _to_response(request)
