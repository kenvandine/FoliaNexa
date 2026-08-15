"""Pure decision logic, kept separate from discord.py wiring so it's
directly unit-testable. PLAN.md §16.
"""

from __future__ import annotations


def decide_auto_approve(member_role_ids: set[int], configured_role_id: int | None) -> bool:
    """Mirrors the web OAuth flow's `auto_approve_on_role` policy (§11C),
    but decided locally from the interaction's own role list rather than
    a Discord API round trip — the bot already has it."""
    if configured_role_id is None:
        return False
    return configured_role_id in member_role_ids
