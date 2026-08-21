from __future__ import annotations

from helpers import auth_header


def test_report_stats_requires_auth(client):
    resp = client.post("/api/v1/stats/report", json={"players": []})
    assert resp.status_code == 401


def test_report_stats_requires_operator(client, viewer_token):
    resp = client.post("/api/v1/stats/report", json={"players": []}, headers=auth_header(viewer_token))
    assert resp.status_code == 403


def test_report_stats_creates_profile_and_stats(client, operator_token):
    body = {
        "players": [
            {
                "uuid": "abc123",
                "username": "Steve",
                "stat_deltas": {"kills": 5, "deaths": 1},
                "playtime_daily": {"2026-08-15": 600},
            }
        ]
    }
    resp = client.post("/api/v1/stats/report", json=body, headers=auth_header(operator_token))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"players_updated": 1}

    profile = client.get("/api/v1/public/players/abc123")
    assert profile.status_code == 200, profile.text
    data = profile.json()
    assert data["username"] == "Steve"
    assert data["stats"] == {"kills": 5, "deaths": 1}
    assert data["playtime_daily"] == [{"date": "2026-08-15", "seconds": 600}]


def test_report_stats_sums_deltas_across_reports(client, operator_token):
    # Regression test for a live bug: an earlier design treated each
    # report's stat value as the player's whole total (mgmt just mirrored
    # the latest value) — that broke the moment a player was tracked by
    # more than one world at once (FoliaNexaStats is default_for_all_
    # worlds, so that's the common case), since two independent "this is
    # the real total" reports just clobbered each other every cycle,
    # confirmed live as visibly flickering public stats. Deltas summed
    # here are correct regardless of how many sources report for the same
    # player.
    first = {"players": [{"uuid": "abc123", "username": "Steve", "stat_deltas": {"kills": 5}}]}
    client.post("/api/v1/stats/report", json=first, headers=auth_header(operator_token))

    second = {
        "players": [
            {
                "uuid": "abc123",
                "username": "SteveRenamed",
                "stat_deltas": {"kills": 9},
                "playtime_daily": {"2026-08-15": 60},
            }
        ]
    }
    resp = client.post("/api/v1/stats/report", json=second, headers=auth_header(operator_token))
    assert resp.status_code == 200

    profile = client.get("/api/v1/public/players/abc123").json()
    assert profile["username"] == "SteveRenamed"
    assert profile["stats"]["kills"] == 14  # 5 + 9, not clobbered to 9


def test_report_stats_gauges_overwrite_rather_than_sum(client, operator_token):
    # Unlike stat_deltas, a gauge (e.g. current AuraSkills power level) is
    # a point-in-time reading, not cumulative — summing it across reports
    # would produce a nonsensical ever-growing number.
    first = {"players": [{"uuid": "abc123", "username": "Steve", "gauges": {"auraskills_power_level": 5}}]}
    second = {"players": [{"uuid": "abc123", "username": "Steve", "gauges": {"auraskills_power_level": 7}}]}
    client.post("/api/v1/stats/report", json=first, headers=auth_header(operator_token))
    client.post("/api/v1/stats/report", json=second, headers=auth_header(operator_token))

    profile = client.get("/api/v1/public/players/abc123").json()
    assert profile["stats"]["auraskills_power_level"] == 7


def test_report_stats_playtime_daily_accumulates(client, operator_token):
    body_one = {"players": [{"uuid": "abc123", "username": "Steve", "playtime_daily": {"2026-08-15": 100}}]}
    body_two = {"players": [{"uuid": "abc123", "username": "Steve", "playtime_daily": {"2026-08-15": 50}}]}
    client.post("/api/v1/stats/report", json=body_one, headers=auth_header(operator_token))
    client.post("/api/v1/stats/report", json=body_two, headers=auth_header(operator_token))

    profile = client.get("/api/v1/public/players/abc123").json()
    assert profile["playtime_daily"] == [{"date": "2026-08-15", "seconds": 150}]
