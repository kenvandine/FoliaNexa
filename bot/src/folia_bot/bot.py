"""folia-nexa-bot entry point. PLAN.md §16.

Three slash commands, plus a chat-relay message listener, all backed by
folia-nexa-mgmt's REST API — this bot never touches mgmt's DB or LXD
directly:

- `/status` — declared worlds and their phase/host, from GET /api/v1/worlds.
- `/request-access <minecraft_username> [minecraft_uuid]` — an in-Discord
  alternative to the web OAuth flow (§11C) for players who'd rather not
  leave Discord. Auto-approves if the invoking member holds
  `FOLIA_BOT_AUTO_APPROVE_ROLE_ID` (optional; unset means every request
  lands pending, same as the web flow with no role configured). The
  optional `minecraft_uuid` is for Bedrock players (PLAN.md §7B) — mgmt
  can't resolve a Floodgate UUID from a Bedrock gamertag the way it
  resolves Java usernames via Mojang, so a Bedrock player supplies their
  own (found via Floodgate's in-game `/uuid` command) and mgmt stores it
  directly instead of doing a Mojang lookup.
- Discord role-sync — separate from and complementary to the one-shot
  `/request-access`-time check above; does NOT replace it. Holding the
  configured role never gets someone onto the whitelist by itself — they
  still have to run `/request-access` (or the web OAuth flow) once, so
  mgmt learns which Minecraft account is theirs; role-sync only ever
  manages an AccessRequest mgmt already has, never invents one for a
  Discord member it's never heard from. What it adds is *ongoing*
  enforcement of that one-time link: if mgmt's dashboard has the Discord
  role gate enabled (`GET /access-requests/discord-gate-config`, polled
  here every 60s so a dashboard toggle takes effect without a bot
  restart), this bot keeps mgmt's already-linked AccessRequest rows in
  sync with *live* membership of that one configured role: on every
  relevant `on_member_update` (someone gains/loses the role) and on a
  15-minute safety-net timer, it posts the role's complete current
  membership (`guild.get_role(role_id).members` — free, from the
  gateway-maintained cache the privileged members intent provides, no
  extra API calls) to `POST /access-requests/role-sync`, which grants or
  revokes access accordingly. Requires the privileged `members` intent
  (enabled below and in the Discord Developer Portal) and
  `DISCORD_GUILD_ID` to be set.
- `/leaderboard` — an explicit stub. There's no analytics store backing
  it yet (PLAN.md §16's Future Expansion), and saying so beats a missing
  command or fabricated numbers.
- `on_message` — the Discord->game half of the chat bridge (mgmt's
  routers/chat.py has the full design). Relays every guild message it
  sees to POST /api/v1/chat/relay unconditionally; mgmt decides whether
  that channel is actually configured as a bridge channel, so this file
  never needs its own copy of that mapping. Requires the privileged
  `message_content` intent, enabled both here and in the Discord
  Developer Portal for this application.

Configuration, environment variables:
  DISCORD_BOT_TOKEN (required), FOLIA_MGMT_URL (required),
  FOLIA_MGMT_API_TOKEN (required — needs at least operator role, since
  creating access requests on another user's behalf is more than a
  read-only action), DISCORD_GUILD_ID (optional — syncs commands to one
  guild instantly instead of waiting up to an hour for a global sync,
  useful during setup; also required for the role-sync loop above to
  know which guild to enumerate — unset means role-sync silently no-ops,
  same "unset disables the feature" convention as everything else here),
  FOLIA_BOT_AUTO_APPROVE_ROLE_ID (optional, for the one-shot
  /request-access-time check only — the ongoing role-sync loop instead
  reads its role id live from mgmt's dashboard-editable gate config).

The gateway/heartbeat/reconnect protocol itself is discord.py's job (a
well-tested third-party library, not hand-rolled here); what's actually
new code in this package — embeds.py, access.py, mgmt_client.py — is
unit-tested without needing a live connection. The gateway connection
itself is now also confirmed live (2026-08-16): deployed to a real
production host with a real bot token and the privileged members intent
enabled in the Discord Developer Portal, it connected successfully (no
PrivilegedIntentsRequired crash) and began polling
discord-gate-config on its own right after on_ready, as designed. Not
yet observed live: a real /request-access invocation by an actual
Discord member, or a real on_member_update event actually firing the
role-sync POST — only the connection itself and the periodic config
poll have been watched directly so far.
"""

from __future__ import annotations

import logging
import os

import discord
from discord import app_commands
from discord.ext import tasks

from folia_bot.access import compute_role_sync_ids, decide_auto_approve, role_membership_changed
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
    # Privileged intent — must also be enabled for this application in the
    # Discord Developer Portal, not just here. Needed for on_message below
    # (the chat bridge's Discord->game direction, PLAN.md §16) to actually
    # see message.content; every other intent this bot uses is already
    # covered by Intents.default().
    intents.message_content = True
    # Privileged intent — same Developer Portal requirement as above.
    # Keeps a live, gateway-maintained cache of every member's roles, so
    # on_member_update fires for role changes and guild.get_role(...).members
    # is always current, with zero extra Discord API calls — needed for
    # the Discord role-sync loop (see module docstring).
    intents.members = True
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    # Cached locally and refreshed periodically (see refresh_gate_config
    # below) rather than read once from env, since it's edited live from
    # mgmt's dashboard — a toggle there should take effect without a bot
    # restart.
    gate_config: dict = {"enabled": False, "guild_id": None, "role_id": None}

    async def _push_role_sync(guild: discord.Guild) -> None:
        role_id = gate_config.get("role_id")
        role = guild.get_role(int(role_id)) if role_id else None
        if role is None:
            # Unconfigured, or the role doesn't exist in this guild —
            # "nothing to sync", not "revoke everyone". A role that DOES
            # exist but currently has zero members is different (real
            # data, correctly pushed as an empty list below) — that case
            # is exactly how a mass revoke is supposed to propagate.
            return
        member_ids = compute_role_sync_ids(role)
        try:
            await mgmt.role_sync(discord_user_ids_with_role=member_ids)
        except Exception:
            logger.exception("failed to push discord role-sync to mgmt")

    @tasks.loop(seconds=60)
    async def refresh_gate_config() -> None:
        try:
            gate_config.update(await mgmt.get_discord_gate_config())
        except Exception:
            logger.exception("failed to refresh discord gate config from mgmt")

    @tasks.loop(minutes=15)
    async def periodic_role_sync() -> None:
        if not gate_config.get("enabled") or not guild_id_raw:
            return
        guild = client.get_guild(int(guild_id_raw))
        if guild is not None:
            await _push_role_sync(guild)

    @client.event
    async def on_member_update(before: discord.Member, after: discord.Member) -> None:
        role_id = gate_config.get("role_id")
        if not gate_config.get("enabled") or role_id is None:
            return
        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}
        if not role_membership_changed(before_ids, after_ids, int(role_id)):
            return
        await _push_role_sync(after.guild)

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
    @app_commands.describe(
        minecraft_username="Your exact in-game username (Bedrock players: your gamertag is fine here too)",
        minecraft_uuid="Bedrock players only: your Floodgate UUID (get it with /uuid in-game once you've joined via Geyser). Leave blank for Java.",
    )
    async def request_access(
        interaction: discord.Interaction,
        minecraft_username: str,
        minecraft_uuid: str | None = None,
    ) -> None:
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
                minecraft_uuid=minecraft_uuid,
                auto_approve=auto_approve,
            )
        except Exception:
            logger.exception("failed to create access request for %s", minecraft_username)
            await interaction.followup.send("Couldn't reach the cluster manager — try again shortly.", ephemeral=True)
            return

        outcome = "approved! You can join now." if result.get("status") == "approved" else "submitted and pending review."
        await interaction.followup.send(f"Access request for `{minecraft_username}` {outcome}", ephemeral=True)

    @client.event
    async def on_message(message: discord.Message) -> None:
        # Relays every guild message unconditionally and lets mgmt decide
        # whether the channel is actually chat-bridge-configured
        # (routers/chat.py) — this bot never holds its own copy of that
        # mapping, matching the "never touches mgmt's DB directly" design
        # everywhere else in this file. message.author.bot excludes this
        # bot's own relayed-from-Discord messages if it ever posts through
        # a regular message rather than a webhook, and other bots'
        # messages generally — avoids relay loops.
        if message.author.bot or message.guild is None or not message.content:
            return
        try:
            await mgmt.relay_chat(
                channel_id=str(message.channel.id), discord_username=str(message.author), message=message.content
            )
        except Exception:
            logger.exception("failed to relay chat message to mgmt")

    @client.event
    async def on_ready() -> None:
        if guild_id_raw:
            guild = discord.Object(id=int(guild_id_raw))
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        else:
            await tree.sync()
        if not refresh_gate_config.is_running():
            refresh_gate_config.start()
        if not periodic_role_sync.is_running():
            periodic_role_sync.start()
        logger.info("folia-nexa-bot ready as %s", client.user)

    return client


def main() -> None:
    bot_token = _require_env("DISCORD_BOT_TOKEN")
    client = build_client()
    client.run(bot_token)


if __name__ == "__main__":
    main()
