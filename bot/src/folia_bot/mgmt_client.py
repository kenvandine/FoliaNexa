"""Async client for the calls folia-nexa-bot makes to
folia-nexa-mgmt. PLAN.md §16.
"""

from __future__ import annotations

import httpx


class MgmtClient:
    def __init__(self, base_url: str, api_token: str, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_token}"}

    async def get_worlds(self) -> list[dict]:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.get("/api/v1/worlds", headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def relay_chat(self, *, channel_id: str, discord_username: str, message: str) -> dict:
        """POST /api/v1/chat/relay (PLAN.md §16) — called for every guild
        message the bot sees; mgmt decides whether channel_id is actually
        bridge-configured (routers/chat.py), the bot doesn't need its own
        copy of that mapping."""
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.post(
                "/api/v1/chat/relay",
                headers=self._headers(),
                json={"channel_id": channel_id, "discord_username": discord_username, "message": message},
            )
            resp.raise_for_status()
            return resp.json()

    async def create_access_request(
        self,
        *,
        discord_user_id: str,
        discord_username: str,
        minecraft_username: str,
        auto_approve: bool,
        minecraft_uuid: str | None = None,
    ) -> dict:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.post(
                "/api/v1/access-requests",
                headers=self._headers(),
                json={
                    "discord_user_id": discord_user_id,
                    "discord_username": discord_username,
                    "minecraft_username": minecraft_username,
                    "minecraft_uuid": minecraft_uuid,
                    "auto_approve": auto_approve,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def get_discord_gate_config(self) -> dict:
        """GET /access-requests/discord-gate-config — whether the dynamic
        Discord-role allowlist is on and which guild/role gates it,
        editable from mgmt's dashboard. Polled periodically rather than
        read once at startup, so a toggle there takes effect without a
        bot restart."""
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.get("/api/v1/access-requests/discord-gate-config", headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def role_sync(self, *, discord_user_ids_with_role: list[str]) -> dict:
        """POST /access-requests/role-sync — reports the *complete
        current* membership of the configured allowlist role so mgmt can
        reconcile AccessRequest.status against it."""
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.post(
                "/api/v1/access-requests/role-sync",
                headers=self._headers(),
                json={"discord_user_ids_with_role": discord_user_ids_with_role},
            )
            resp.raise_for_status()
            return resp.json()
