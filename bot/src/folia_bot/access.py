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


def role_membership_changed(before_role_ids: set[int], after_role_ids: set[int], tracked_role_id: int) -> bool:
    """True only if the *tracked* role's membership actually flipped —
    used by on_member_update to ignore unrelated role changes (someone
    else's role was added/removed) so we don't push a role-sync for
    every member-update event in the guild, only the ones that matter."""
    return (tracked_role_id in before_role_ids) != (tracked_role_id in after_role_ids)


def compute_role_sync_ids(role) -> list[str]:
    """Full current membership of the configured allowlist role, as
    discord_user_id strings, from the bot's own gateway-maintained member
    cache (requires the privileged members intent) — zero extra API
    calls. `role=None` (unconfigured, or not found in this guild) means
    'nothing to sync': the caller should skip the POST entirely rather
    than push an empty list, which would read as 'revoke everyone'."""
    if role is None:
        return []
    return [str(member.id) for member in role.members]
