from __future__ import annotations

from folia_bot.presence import diff_joins, flatten_worlds_online, format_join_message


def test_flatten_worlds_online_maps_uuid_to_world_and_username():
    worlds_online = {
        "worlds": [
            {"world": "world-overworld", "players": [{"uuid": "u1", "username": "Alice"}]},
            {"world": "world-lobby", "players": [{"uuid": "u2", "username": "Bob"}]},
        ],
        "total_players": 2,
    }
    assert flatten_worlds_online(worlds_online) == {
        "u1": ("world-overworld", "Alice"),
        "u2": ("world-lobby", "Bob"),
    }


def test_flatten_worlds_online_empty():
    assert flatten_worlds_online({"worlds": [], "total_players": 0}) == {}


def test_diff_joins_detects_fresh_join():
    previous = {}
    current = {"u1": ("world-overworld", "Alice")}
    assert diff_joins(previous, current) == [("world-overworld", "Alice", "u1")]


def test_diff_joins_detects_world_switch():
    previous = {"u1": ("world-overworld", "Alice")}
    current = {"u1": ("world-lobby", "Alice")}
    assert diff_joins(previous, current) == [("world-lobby", "Alice", "u1")]


def test_diff_joins_ignores_player_who_stayed_put():
    previous = {"u1": ("world-overworld", "Alice")}
    current = {"u1": ("world-overworld", "Alice")}
    assert diff_joins(previous, current) == []


def test_diff_joins_ignores_player_who_left():
    previous = {"u1": ("world-overworld", "Alice")}
    current = {}
    assert diff_joins(previous, current) == []


def test_diff_joins_handles_multiple_players():
    previous = {"u1": ("world-overworld", "Alice")}
    current = {
        "u1": ("world-overworld", "Alice"),  # unchanged
        "u2": ("world-lobby", "Bob"),  # fresh join
    }
    assert diff_joins(previous, current) == [("world-lobby", "Bob", "u2")]


def test_format_join_message_includes_world_and_username():
    message = format_join_message("world-overworld", "Alice")
    assert "Alice" in message and "world-overworld" in message
