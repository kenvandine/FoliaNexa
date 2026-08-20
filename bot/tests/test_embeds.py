from __future__ import annotations

from folia_bot.embeds import build_leaderboard_embed, build_status_embed, build_who_embed


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


def test_leaderboard_embed_empty_says_no_data_yet():
    embed = build_leaderboard_embed("kills", [])
    assert "no data" in embed.description.lower()


def test_leaderboard_embed_lists_ranked_entries():
    entries = [{"uuid": "u1", "username": "Alice", "value": 42.0}, {"uuid": "u2", "username": "Bob", "value": 10.0}]
    embed = build_leaderboard_embed("kills", entries)
    assert "Kills" in embed.title
    assert "1." in embed.description and "Alice" in embed.description and "42" in embed.description
    assert "2." in embed.description and "Bob" in embed.description


def test_leaderboard_embed_formats_playtime_as_hours_and_minutes():
    entries = [{"uuid": "u1", "username": "Alice", "value": 5400.0}]  # 1h 30m
    embed = build_leaderboard_embed("playtime_seconds_total", entries)
    assert "1h 30m" in embed.description


def test_leaderboard_embed_falls_back_to_raw_stat_key_for_unknown_stat():
    embed = build_leaderboard_embed("some_future_stat", [])
    assert "some_future_stat" in embed.title


def test_who_embed_nobody_online():
    embed = build_who_embed({"worlds": [], "total_players": 0})
    assert "nobody" in embed.description.lower()


def test_who_embed_lists_players_per_world():
    worlds_online = {
        "worlds": [
            {"world": "world-overworld", "type": "overworld", "player_count": 2, "players": [
                {"uuid": "u1", "username": "Alice"}, {"uuid": "u2", "username": "Bob"},
            ]},
            {"world": "world-lobby", "type": "lobby", "player_count": 0, "players": []},
        ],
        "total_players": 2,
    }
    embed = build_who_embed(worlds_online)
    names = [field.name for field in embed.fields]
    assert names == ["world-overworld (2)"]  # empty worlds omitted
    assert "Alice" in embed.fields[0].value and "Bob" in embed.fields[0].value
    assert "2 player" in embed.footer.text
