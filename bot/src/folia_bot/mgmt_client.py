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

    async def create_access_request(
        self,
        *,
        discord_user_id: str,
        discord_username: str,
        minecraft_username: str,
        auto_approve: bool,
    ) -> dict:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.post(
                "/api/v1/access-requests",
                headers=self._headers(),
                json={
                    "discord_user_id": discord_user_id,
                    "discord_username": discord_username,
                    "minecraft_username": minecraft_username,
                    "auto_approve": auto_approve,
                },
            )
            resp.raise_for_status()
            return resp.json()
