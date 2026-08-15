from __future__ import annotations

from folia_bot.embeds import build_leaderboard_stub_embed, build_status_embed


def test_status_embed_lists_visible_worlds_sorted():
    worlds = [
        {"name": "world-overworld", "type": "overworld", "phase": "running", "host_name": "node-a"},
        {"name": "world-lobby", "type": "lobby", "phase": "pending", "host_name": None},
    ]
    embed = build_status_embed(worlds)

    names = [field.name for field in embed.fields]
    assert names == ["world-lobby", "world-overworld"]  # alphabetical


def test_status_embed_hides_staging_and_infra_worlds():
    worlds = [
        {"name": "world-mysql", "type": "infra", "phase": "running", "host_name": "node-a"},
        {"name": "world-staging", "type": "staging", "phase": "running", "host_name": "node-a"},
        {"name": "world-overworld", "type": "overworld", "phase": "running", "host_name": "node-a"},
    ]
    embed = build_status_embed(worlds)

    names = [field.name for field in embed.fields]
    assert names == ["world-overworld"]


def test_status_embed_shows_unassigned_for_no_host():
    worlds = [{"name": "world-pending", "type": "lobby", "phase": "pending", "host_name": None}]
    embed = build_status_embed(worlds)
    assert "unassigned" in embed.fields[0].value


def test_status_embed_empty_worlds_list():
    embed = build_status_embed([])
    assert "No worlds" in embed.description


def test_status_embed_uses_known_phase_labels():
    worlds = [{"name": "world-crashed", "type": "overworld", "phase": "crashed", "host_name": "node-a"}]
    embed = build_status_embed(worlds)
    assert "crashed" in embed.fields[0].value.lower()


def test_status_embed_falls_back_to_raw_phase_for_unknown_value():
    worlds = [{"name": "world-weird", "type": "overworld", "phase": "some-future-phase", "host_name": "node-a"}]
    embed = build_status_embed(worlds)
    assert "some-future-phase" in embed.fields[0].value


def test_leaderboard_stub_is_honest_about_not_being_implemented():
    embed = build_leaderboard_stub_embed()
    assert "not available" in embed.description.lower() or "not been built" in embed.description.lower()
