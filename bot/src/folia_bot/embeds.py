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


def build_leaderboard_stub_embed() -> discord.Embed:
    """Leaderboards need the analytics store PLAN.md's Future Expansion
    section describes — it hasn't been built, so this says so rather than
    showing a missing command or fabricated numbers."""
    return discord.Embed(
        title="Leaderboards",
        description=(
            "Not available yet — this needs the analytics store described "
            "in PLAN.md's Future Expansion section, which hasn't been built."
        ),
        color=discord.Color.light_grey(),
    )
