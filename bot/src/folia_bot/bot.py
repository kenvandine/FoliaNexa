"""folia-nexa-bot entry point. PLAN.md §16.

Five slash commands, a chat-relay message listener, and a background
presence-join announcer, all backed by folia-nexa-mgmt's REST API — this
bot never touches mgmt's DB or LXD directly:

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
  (enabled below and in the Discord Developer Portal). The guild to
  enumerate comes from the same dashboard-polled gate config as the role
  id — not an env var — so there's nothing extra to set on this bot
  beyond the intent itself; an operator who's never touched the
  dashboard's Discord role gate card just gets a permanent no-op here.
- `/who [world]` — who's currently online and which world they're in,
  from `GET /api/v1/public/worlds` (the same public endpoint the
  player-hub portal's "who's online" page reads, mgmt's
  routers/public_stats.py) — an explicit `world` choice narrows to one
  world's roster, omitted shows every world with anyone online.
- `/leaderboard [stat]` — top players by a stat, from `GET
  /api/v1/public/leaderboards` (again, the same endpoint the portal's
  leaderboard page reads). `stat` defaults to kills; the other known
  stat_keys (mgmt's models.py `PlayerStat` docstring) are offered as
  choices. Data itself may still be genuinely empty until a real
  stats-reporting plugin is deployed (catalog id `FoliaNexaStats` is a
  placeholder pending its first release, per CLAUDE.md) — the embed says
  "no data yet" rather than fabricating numbers, same honesty this
  command used to apply by being a hardcoded stub before this endpoint
  existed.
- Presence-join announcer — a background loop (`poll_presence`, mirrors
  the cadence/shape of `periodic_role_sync` below) that polls the same
  `GET /api/v1/public/worlds` `/who` reads, diffs consecutive snapshots
  (`presence.py`'s `diff_joins` — mgmt has no join/quit event stream of
  its own, folia-routes-sync only ever reports a full snapshot on its
  existing 5s poll cycle, never a delta) and posts one line to
  `FOLIA_BOT_PRESENCE_CHANNEL_ID` per player whose world changed since
  the last poll (a fresh join to the cluster, or a switch between
  worlds). Off by default (unset channel id = loop never starts); the
  first poll after (re)start only primes state; it never announces
  players already online when the bot came up, since none of them just
  "joined". There's deliberately no matching leave/quit announcement —
  only joins are inferred this way. PLAN.md §16 used to punt this to a
  per-world `DiscordSRV` install instead; that plugin has the same
  single-Paper-server blind spot the custom chat bridge above was built
  to avoid (catalog.yaml's own DiscordSRV notes: install it on one hub
  world, not every world, or duplicate relays result), so it's handled
  here instead, cluster-wide and world-aware by construction, same
  rationale as the chat bridge.
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
  useful during setup; unrelated to role-sync above, which gets its
  guild from the dashboard-editable gate config instead, not this env
  var), FOLIA_BOT_AUTO_APPROVE_ROLE_ID (optional, for the one-shot
  /request-access-time check only — the ongoing role-sync loop instead
  reads its role id live from mgmt's dashboard-editable gate config too),
  FOLIA_BOT_PRESENCE_CHANNEL_ID (optional — a Discord channel id to post
  presence-join announcements to; unset disables the poll loop entirely,
  same "off by default" treatment as FOLIA_BOT_AUTO_APPROVE_ROLE_ID).

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
from folia_bot.embeds import build_leaderboard_embed, build_status_embed, build_who_embed
from folia_bot.mgmt_client import MgmtClient
from folia_bot.presence import diff_joins, flatten_worlds_online, format_join_message

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
    presence_channel_raw = os.environ.get("FOLIA_BOT_PRESENCE_CHANNEL_ID")
    presence_channel_id = int(presence_channel_raw) if presence_channel_raw else None

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
        guild_id = gate_config.get("guild_id")
        if not gate_config.get("enabled") or not guild_id:
            return
        guild = client.get_guild(int(guild_id))
        if guild is not None:
            await _push_role_sync(guild)

    # uuid -> (world, username), as of the last successful poll — mutated
    # in place rather than reassigned so the closure below doesn't need
    # its own `nonlocal`. "primed" gates the very first poll after
    # (re)start: without it, every player already online when the bot
    # comes up would read as a fresh join.
    presence_state: dict = {"primed": False, "locations": {}}

    @tasks.loop(seconds=15)
    async def poll_presence() -> None:
        try:
            worlds_online = await mgmt.get_worlds_online()
        except Exception:
            logger.exception("failed to poll world presence from mgmt")
            return
        current = flatten_worlds_online(worlds_online)
        if presence_state["primed"]:
            joins = diff_joins(presence_state["locations"], current)
            if joins:
                channel = client.get_channel(presence_channel_id)
                if channel is None:
                    logger.warning("presence channel %s not found (bot not in that guild/channel?)", presence_channel_id)
                else:
                    for world, username, _uuid in joins:
                        await channel.send(format_join_message(world, username))
        presence_state["primed"] = True
        presence_state["locations"] = current

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

    @tree.command(name="who", description="Show who's currently online and which world they're in")
    async def who(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            worlds_online = await mgmt.get_worlds_online()
        except Exception:
            logger.exception("failed to fetch world presence from mgmt")
            await interaction.followup.send("Couldn't reach the cluster manager — try again shortly.")
            return
        await interaction.followup.send(embed=build_who_embed(worlds_online))

    _LEADERBOARD_STAT_CHOICES = [
        app_commands.Choice(name="Kills", value="kills"),
        app_commands.Choice(name="Deaths", value="deaths"),
        app_commands.Choice(name="Blocks Mined", value="blocks_mined"),
        app_commands.Choice(name="Playtime", value="playtime_seconds_total"),
        app_commands.Choice(name="AuraSkills Power Level", value="auraskills_power_level"),
        app_commands.Choice(name="AxAuctions Wealth", value="axauctions_wealth"),
    ]

    @tree.command(name="leaderboard", description="Show a cluster leaderboard")
    @app_commands.describe(stat="Which stat to rank by (defaults to Kills)")
    @app_commands.choices(stat=_LEADERBOARD_STAT_CHOICES)
    async def leaderboard(interaction: discord.Interaction, stat: app_commands.Choice[str] | None = None) -> None:
        stat_key = stat.value if stat is not None else "kills"
        await interaction.response.defer()
        try:
            result = await mgmt.get_leaderboard(stat_key)
        except Exception:
            logger.exception("failed to fetch leaderboard from mgmt")
            await interaction.followup.send("Couldn't reach the cluster manager — try again shortly.")
            return
        await interaction.followup.send(embed=build_leaderboard_embed(stat_key, result.get("entries", [])))

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
            # Populate gate_config synchronously before starting either
            # loop — tasks.loop's first iteration fires immediately but
            # asynchronously on .start(), so starting periodic_role_sync
            # right after this would otherwise race it: its first tick
            # could run before this one's first HTTP fetch completes and
            # see the stale/default {"enabled": False}, silently no-op,
            # and then not try again for a full 15 minutes.
            await refresh_gate_config()
            refresh_gate_config.start()
        if not periodic_role_sync.is_running():
            periodic_role_sync.start()
        if presence_channel_id is not None and not poll_presence.is_running():
            poll_presence.start()
        logger.info("folia-nexa-bot ready as %s", client.user)

    return client


def main() -> None:
    bot_token = _require_env("DISCORD_BOT_TOKEN")
    client = build_client()
    client.run(bot_token)


if __name__ == "__main__":
    main()
