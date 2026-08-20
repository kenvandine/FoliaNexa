"""Pure logic behind the /who command's data shape and the join-
notification poll loop in bot.py — kept separate from discord.py wiring
so it's directly unit-testable. PLAN.md §16.

mgmt has no join/quit event stream: folia-routes-sync reports a full
per-world player snapshot on its existing 5s poll cycle, not deltas (see
mgmt's routers/presence.py module docstring). So "someone joined" is
inferred here, bot-side, by polling `GET /api/v1/public/worlds` on a
timer (`mgmt_client.get_worlds_online`) and diffing consecutive
snapshots against the last world each uuid was seen in.
"""

from __future__ import annotations

PlayerLocations = dict[str, tuple[str, str]]  # uuid -> (world, username)


def flatten_worlds_online(worlds_online: dict) -> PlayerLocations:
    """Flattens GET /api/v1/public/worlds's per-world player lists into a
    single uuid -> (world, username) map for diffing."""
    locations: PlayerLocations = {}
    for world in worlds_online.get("worlds", []):
        for player in world.get("players", []):
            locations[player["uuid"]] = (world["world"], player["username"])
    return locations


def diff_joins(previous: PlayerLocations, current: PlayerLocations) -> list[tuple[str, str, str]]:
    """Every player whose current world differs from where they were last
    seen — a fresh join to the cluster (not in `previous` at all) or a
    switch between worlds — as (world, username, uuid) tuples. A player
    who quit entirely (present in `previous`, absent from `current`)
    produces no event; there's no leave notification, only joins."""
    joins = []
    for uuid, (world, username) in current.items():
        prev = previous.get(uuid)
        if prev is None or prev[0] != world:
            joins.append((world, username, uuid))
    return joins


def format_join_message(world: str, username: str) -> str:
    return f"🟢 **{username}** joined **{world}**"
