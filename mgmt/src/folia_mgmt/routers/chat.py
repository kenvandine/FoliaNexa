"""Discord chat bridge: two independent flows, both routed through mgmt
rather than a direct proxy<->bot connection. PLAN.md §16.

Outbound (game chat -> Discord): folia-routes-sync (the Velocity proxy
plugin, already installed everywhere and already proxy-wide — see
FoliaRoutesSyncPlugin's onPlayerChat) reports every chat message here via
POST /chat/report. mgmt looks up this world's webhook (and/or the
server-wide one) and POSTs directly to Discord's webhook API — no bot
gateway connection needed for this direction at all, a webhook is just an
unauthenticated HTTP endpoint Discord hands out per channel.

Inbound (Discord -> game chat): folia-nexa-bot (already gateway-connected,
already reading messages) relays every message it sees in a guild channel
to POST /chat/relay. mgmt resolves the channel via ChatBridgeConfig.
inbound_channels and appends it to an in-process pending queue;
folia-routes-sync polls GET /chat/pending in its existing 5s poll cycle
and broadcasts each one directly to the right players via Velocity's own
player messaging — never touching a backend Paper server at all, since
Velocity can message any connected player directly regardless of which
world they're on.

Why not just install DiscordSRV per-world instead (the plugin catalog
already has a real, verified DiscordSRV entry)? DiscordSRV was built for
a single Paper server and has no concept of "which world" in a
multi-server Velocity network — see that catalog entry's own notes. This
sidesteps it entirely: one bridge, proxy-wide, world-aware by
construction.

The pending queue is in-process only (app.state.chat_pending, same
TTLCache-adjacent "fine at this scale" pattern as public_stats.py) — a
message lost on a mgmt restart is an acceptable loss for chat, not
something worth a DB table and its own migration.

A third, operator-facing flow reuses the same pending queue: POST
/chat/broadcast (dashboard's Broadcast card) lets an operator announce
something (e.g. "proxy restarting in 5 minutes") to one world or, with
world=None, every connected player cluster-wide in one shot — same
delivery path as inbound Discord messages, so it doesn't depend on any
world's RCON being reachable. The queued item's kind distinguishes the
two so folia-routes-sync can render them differently (see
PendingChatMessage's own docstring).
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.auth import require_operator, require_viewer
from folia_mgmt.db import get_session
from folia_mgmt.models import ChatBridgeConfig, User, World, utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

# Defends against an unbounded queue if folia-routes-sync is unreachable
# for a long stretch — oldest messages are dropped first, matching "best-
# effort, not a durable log" from the module docstring.
_MAX_PENDING = 200


def _get_config(session: Session) -> ChatBridgeConfig:
    return session.get(ChatBridgeConfig, 1) or ChatBridgeConfig(id=1)


class ChatConfigResponse(BaseModel):
    server_wide_webhook_url: str | None = None
    world_webhook_urls: dict[str, str] = {}
    inbound_channels: dict[str, str] = {}


def _to_config_response(config: ChatBridgeConfig) -> ChatConfigResponse:
    return ChatConfigResponse(
        server_wide_webhook_url=config.server_wide_webhook_url,
        world_webhook_urls=config.world_webhook_urls,
        inbound_channels=config.inbound_channels,
    )


@router.get("/config", response_model=ChatConfigResponse, dependencies=[Depends(require_operator)])
def get_chat_config(session: Session = Depends(get_session)) -> ChatConfigResponse:
    """Operator-only, unlike the display config's GET — webhook URLs are
    write-capable credentials (anyone holding one can post as the bridge
    in that Discord channel), not just cosmetic config."""
    return _to_config_response(_get_config(session))


class UpdateChatConfigRequest(BaseModel):
    server_wide_webhook_url: str | None = None
    world_webhook_urls: dict[str, str] = {}
    inbound_channels: dict[str, str] = {}


@router.put("/config", response_model=ChatConfigResponse, dependencies=[Depends(require_operator)])
def update_chat_config(body: UpdateChatConfigRequest, session: Session = Depends(get_session)) -> ChatConfigResponse:
    config = _get_config(session)
    config.server_wide_webhook_url = body.server_wide_webhook_url
    config.world_webhook_urls = body.world_webhook_urls
    config.inbound_channels = body.inbound_channels
    config.updated_at = utcnow()
    session.add(config)
    session.commit()
    session.refresh(config)
    return _to_config_response(config)


class ReportChatRequest(BaseModel):
    world: str
    player: str
    message: str


class ReportChatResponse(BaseModel):
    delivered: int


@router.post("/report", response_model=ReportChatResponse, dependencies=[Depends(require_viewer)])
def report_chat(body: ReportChatRequest, session: Session = Depends(get_session)) -> ReportChatResponse:
    """Called by folia-routes-sync for every in-game chat message
    (FoliaRoutesSyncPlugin.onPlayerChat). Fans out to Discord via plain
    webhook POSTs — no bot/gateway involvement for this direction."""
    config = _get_config(session)
    urls = []
    if config.server_wide_webhook_url:
        urls.append(config.server_wide_webhook_url)
    per_world = config.world_webhook_urls.get(body.world)
    if per_world:
        urls.append(per_world)

    delivered = 0
    for url in urls:
        try:
            resp = httpx.post(
                url,
                json={"username": f"[{body.world}] {body.player}"[:80], "content": body.message[:2000]},
                timeout=5.0,
            )
            resp.raise_for_status()
            delivered += 1
        except httpx.HTTPError:
            logger.warning("failed to deliver chat message to a Discord webhook for world '%s'", body.world)
    return ReportChatResponse(delivered=delivered)


class RelayChatRequest(BaseModel):
    channel_id: str
    discord_username: str
    message: str


class RelayChatResponse(BaseModel):
    queued: bool


@router.post("/relay", response_model=RelayChatResponse, dependencies=[Depends(require_operator)])
def relay_chat_from_discord(
    body: RelayChatRequest, request: Request, session: Session = Depends(get_session)
) -> RelayChatResponse:
    """Called by folia-nexa-bot for every message it sees in a guild text
    channel — it relays everything and lets mgmt decide whether the
    channel is actually bridge-configured, keeping all the bridge logic
    server-side (matches bot.py's "never touches mgmt's DB directly"
    design elsewhere)."""
    config = _get_config(session)
    world = config.inbound_channels.get(body.channel_id)
    if world is None:
        return RelayChatResponse(queued=False)

    pending: list[dict] = request.app.state.chat_pending
    pending.append(
        {"world": None if world == "*" else world, "author": body.discord_username, "message": body.message, "kind": "discord"}
    )
    del pending[:-_MAX_PENDING]  # keep only the newest _MAX_PENDING if it overflowed
    return RelayChatResponse(queued=True)


class BroadcastChatRequest(BaseModel):
    world: str | None = None  # None = every connected player, cluster-wide
    message: str


class BroadcastChatResponse(BaseModel):
    queued: bool


@router.post("/broadcast", response_model=BroadcastChatResponse, dependencies=[Depends(require_operator)])
def broadcast_chat(
    body: BroadcastChatRequest,
    request: Request,
    user: User = Depends(require_operator),
    session: Session = Depends(get_session),
) -> BroadcastChatResponse:
    """Operator-authored announcement (e.g. "restarting in 5 minutes"),
    e.g. from the dashboard's Broadcast card. Rides the exact same
    pending-queue/Velocity-delivery path as a Discord-relayed message
    (folia-routes-sync's deliverPendingChat) — reaches every connected
    player via the proxy directly, without going through any backend
    world's RCON at all, so it still lands even if a specific world's
    RCON happens to be unreachable. world=None fans out cluster-wide in
    one shot (Velocity already holds every connected player, regardless
    of which world they're on); a specific world name only reaches that
    world's currently-connected players. kind="broadcast" is what tells
    the proxy to render this as a server announcement rather than a
    Discord-relayed chat line (different tag/color — see
    FoliaRoutesSyncPlugin.deliverPendingChat)."""
    message = body.message.strip()
    if not message:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "message must not be empty")
    if body.world is not None and not session.exec(select(World).where(World.name == body.world)).first():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"world '{body.world}' not found")

    pending: list[dict] = request.app.state.chat_pending
    pending.append({"world": body.world, "author": user.username, "message": message, "kind": "broadcast"})
    del pending[:-_MAX_PENDING]
    return BroadcastChatResponse(queued=True)


class PendingChatMessage(BaseModel):
    world: str | None
    author: str
    message: str
    kind: str = "discord"


@router.get("/pending", response_model=list[PendingChatMessage], dependencies=[Depends(require_viewer)])
def get_pending_chat(request: Request) -> list[PendingChatMessage]:
    """Polled by folia-routes-sync in its existing 5s cycle — drains the
    queue on every call (single-consumer: exactly one proxy instance in
    this architecture, same assumption GET /routes/display etc. already
    make)."""
    pending: list[dict] = request.app.state.chat_pending
    request.app.state.chat_pending = []
    return [PendingChatMessage(**item) for item in pending]
