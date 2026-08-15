"""Discord OAuth2 (access requests) and Mojang username resolution.
PLAN.md §11C.

NOT exercised against live Discord/Mojang endpoints in this environment —
written against their documented REST contracts. Validate against a real
Discord application before relying on it.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from folia_mgmt.config import Settings

DISCORD_API = "https://discord.com/api/v10"
MOJANG_API = "https://api.mojang.com"


class DiscordError(RuntimeError):
    pass


def build_authorize_url(settings: Settings, state: str) -> str:
    if not settings.discord_configured or not settings.discord_redirect_uri:
        raise DiscordError("Discord integration is not configured (client id/secret/guild/redirect_uri)")
    params = {
        "client_id": settings.discord_client_id,
        "redirect_uri": settings.discord_redirect_uri,
        "response_type": "code",
        "scope": "identify guilds.members.read",
        "state": state,
    }
    return f"https://discord.com/oauth2/authorize?{urlencode(params)}"


def exchange_code(settings: Settings, code: str) -> str:
    """Authorization code -> user access token."""
    resp = httpx.post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "client_id": settings.discord_client_id,
            "client_secret": settings.discord_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.discord_redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10.0,
    )
    if resp.status_code != 200:
        raise DiscordError(f"code exchange failed: {resp.status_code} {resp.text}")
    return resp.json()["access_token"]


def get_current_user(access_token: str) -> dict:
    resp = httpx.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10.0,
    )
    if resp.status_code != 200:
        raise DiscordError(f"failed to fetch discord identity: {resp.status_code} {resp.text}")
    return resp.json()


def get_guild_member_roles(access_token: str, guild_id: str) -> list[str]:
    """Requires the guilds.members.read scope. Returns [] if not a member."""
    resp = httpx.get(
        f"{DISCORD_API}/users/@me/guilds/{guild_id}/member",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10.0,
    )
    if resp.status_code == 404:
        return []
    if resp.status_code != 200:
        raise DiscordError(f"failed to fetch guild membership: {resp.status_code} {resp.text}")
    return resp.json().get("roles", [])


def resolve_minecraft_uuid(username: str) -> str | None:
    try:
        resp = httpx.get(f"{MOJANG_API}/users/profiles/minecraft/{username}", timeout=10.0)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    return resp.json().get("id")
