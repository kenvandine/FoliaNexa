"""folia-discord-bridge entry point. PLAN.md §16.

Three slash commands, all backed by folia-smp-mgmt's REST API — this bot
never touches mgmt's DB or LXD directly:

- `/status` — declared worlds and their phase/host, from GET /api/v1/worlds.
- `/request-access <minecraft_username>` — an in-Discord alternative to
  the web OAuth flow (§11C) for players who'd rather not leave Discord.
  Auto-approves if the invoking member holds `FOLIA_BOT_AUTO_APPROVE_ROLE_ID`
  (optional; unset means every request lands pending, same as the web
  flow with no role configured).
- `/leaderboard` — an explicit stub. There's no analytics store backing
  it yet (PLAN.md §16's Future Expansion), and saying so beats a missing
  command or fabricated numbers.

Configuration, environment variables:
  DISCORD_BOT_TOKEN (required), FOLIA_MGMT_URL (required),
  FOLIA_MGMT_API_TOKEN (required — needs at least operator role, since
  creating access requests on another user's behalf is more than a
  read-only action), DISCORD_GUILD_ID (optional — syncs commands to one
  guild instantly instead of waiting up to an hour for a global sync,
  useful during setup), FOLIA_BOT_AUTO_APPROVE_ROLE_ID (optional).

NOT exercised against a live Discord gateway connection in this
environment — no bot token or registered Discord application was
available to test against. The gateway/heartbeat/reconnect protocol
itself is discord.py's job (a well-tested third-party library, not
hand-rolled here); what's actually new code in this package — embeds.py,
access.py, mgmt_client.py — is unit-tested without needing a live
connection.
"""

from __future__ import annotations

import logging
import os

import discord
from discord import app_commands

from folia_bot.access import decide_auto_approve
from folia_bot.embeds import build_leaderboard_stub_embed, build_status_embed
from folia_bot.mgmt_client import MgmtClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} must be set")
    return value


def build_client() -> discord.Client:
    mgmt_url = _require_env("FOLIA_MGMT_URL")
    api_token = _require_env("FOLIA_MGMT_API_TOKEN")
    mgmt = MgmtClient(mgmt_url, api_token)

    auto_approve_role_raw = os.environ.get("FOLIA_BOT_AUTO_APPROVE_ROLE_ID")
    auto_approve_role_id = int(auto_approve_role_raw) if auto_approve_role_raw else None
    guild_id_raw = os.environ.get("DISCORD_GUILD_ID")

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    @tree.command(name="status", description="Show the current worlds and their status")
    async def status(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            worlds = await mgmt.get_worlds()
        except Exception:
            logger.exception("failed to fetch worlds from mgmt")
            await interaction.followup.send("Couldn't reach the cluster manager — try again shortly.")
            return
        await interaction.followup.send(embed=build_status_embed(worlds))

    @tree.command(name="leaderboard", description="Show cluster leaderboards")
    async def leaderboard(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=build_leaderboard_stub_embed())

    @tree.command(name="request-access", description="Request access to the Minecraft server")
    @app_commands.describe(minecraft_username="Your exact in-game username")
    async def request_access(interaction: discord.Interaction, minecraft_username: str) -> None:
        await interaction.response.defer(ephemeral=True)

        role_ids: set[int] = set()
        if isinstance(interaction.user, discord.Member):
            role_ids = {role.id for role in interaction.user.roles}
        auto_approve = decide_auto_approve(role_ids, auto_approve_role_id)

        try:
            result = await mgmt.create_access_request(
                discord_user_id=str(interaction.user.id),
                discord_username=str(interaction.user),
                minecraft_username=minecraft_username,
                auto_approve=auto_approve,
            )
        except Exception:
            logger.exception("failed to create access request for %s", minecraft_username)
            await interaction.followup.send("Couldn't reach the cluster manager — try again shortly.", ephemeral=True)
            return

        outcome = "approved! You can join now." if result.get("status") == "approved" else "submitted and pending review."
        await interaction.followup.send(f"Access request for `{minecraft_username}` {outcome}", ephemeral=True)

    @client.event
    async def on_ready() -> None:
        if guild_id_raw:
            guild = discord.Object(id=int(guild_id_raw))
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        else:
            await tree.sync()
        logger.info("folia-discord-bridge ready as %s", client.user)

    return client


def main() -> None:
    bot_token = _require_env("DISCORD_BOT_TOKEN")
    client = build_client()
    client.run(bot_token)


if __name__ == "__main__":
    main()
