"""Pure embed-building, kept separate from discord.py's client/gateway
wiring so it's directly unit-testable without a live connection. PLAN.md
§16.
"""

from __future__ import annotations

import discord

# Worlds not meant to be player-facing shouldn't show up in a
# player-facing status embed.
_HIDDEN_TYPES = {"staging", "infra"}

_PHASE_LABEL = {
    "running": "🟢 running",
    "provisioning": "🟡 provisioning",
    "pending": "⚪ pending",
    "crashed": "🔴 crashed",
    "draining": "🟠 draining",
    "deleted": "⚫ deleted",
}


def build_status_embed(worlds: list[dict]) -> discord.Embed:
    visible = [w for w in worlds if w.get("type") not in _HIDDEN_TYPES]
    embed = discord.Embed(title="Server Status", color=discord.Color.blurple())

    if not visible:
        embed.description = "No worlds are currently declared."
        return embed

    for world in sorted(visible, key=lambda w: w["name"]):
        phase = world.get("phase", "unknown")
        label = _PHASE_LABEL.get(phase, phase)
        host = world.get("host_name") or "unassigned"
        embed.add_field(name=world["name"], value=f"{label} · host: {host}", inline=False)

    embed.set_footer(text="Player counts and TPS aren't wired up yet.")
    return embed


# Known stat_keys as of mgmt's models.py PlayerStat docstring — kept here
# only for friendly display labels; the set is intentionally open-ended
# server-side, an unrecognized stat_key still renders fine (falls back to
# the raw key below).
_STAT_LABELS = {
    "kills": "Kills",
    "deaths": "Deaths",
    "blocks_mined": "Blocks Mined",
    "playtime_seconds_total": "Playtime",
    "auraskills_power_level": "AuraSkills Power Level",
    "axauctions_wealth": "AxAuctions Wealth",
}


def _format_stat_value(stat_key: str, value: float) -> str:
    if stat_key == "playtime_seconds_total":
        total_minutes = int(value) // 60
        hours, minutes = divmod(total_minutes, 60)
        return f"{hours}h {minutes}m"
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def build_leaderboard_embed(stat_key: str, entries: list[dict]) -> discord.Embed:
    """`entries` is `GET /api/v1/public/leaderboards`'s own `entries` list
    (mgmt's routers/public_stats.py) — the same real, already-live data
    the portal's leaderboard page renders. Data itself may still be empty
    in practice until a stats-reporting plugin is actually deployed
    (catalog id `FoliaNexaStats` is a placeholder pending its first
    release, per CLAUDE.md) — that's a live-data question, not a missing-
    feature one, so an empty result says "no data yet" rather than
    "not implemented"."""
    label = _STAT_LABELS.get(stat_key, stat_key)
    embed = discord.Embed(title=f"Leaderboard — {label}", color=discord.Color.gold())
    if not entries:
        embed.description = "No data yet for this leaderboard."
        return embed
    embed.description = "\n".join(
        f"**{i}.** {entry['username']} — {_format_stat_value(stat_key, entry['value'])}"
        for i, entry in enumerate(entries, start=1)
    )
    return embed


def build_who_embed(worlds_online: dict) -> discord.Embed:
    """`worlds_online` is `GET /api/v1/public/worlds`'s own body (mgmt's
    routers/public_stats.py) — same data source the join-notification
    poll loop diffs (presence.py) and the portal's "who's online" page
    renders. Worlds with nobody online are omitted from the field list
    (the footer's total already covers "nobody anywhere")."""
    total = worlds_online.get("total_players", 0)
    embed = discord.Embed(title="Who's Online", color=discord.Color.green())
    if total == 0:
        embed.description = "Nobody is online right now."
        return embed

    for world in worlds_online.get("worlds", []):
        players = world.get("players", [])
        if not players:
            continue
        names = ", ".join(p["username"] for p in players)
        embed.add_field(name=f"{world['world']} ({len(players)})", value=names, inline=False)

    embed.set_footer(text=f"{total} player(s) online")
    return embed
