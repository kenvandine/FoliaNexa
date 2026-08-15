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
                "stats": {"kills": 5, "deaths": 1},
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


def test_report_stats_upserts_existing_player(client, operator_token):
    first = {"players": [{"uuid": "abc123", "username": "Steve", "stats": {"kills": 5}}]}
    client.post("/api/v1/stats/report", json=first, headers=auth_header(operator_token))

    second = {
        "players": [
            {
                "uuid": "abc123",
                "username": "SteveRenamed",
                "stats": {"kills": 9},
                "playtime_daily": {"2026-08-15": 60},
            }
        ]
    }
    resp = client.post("/api/v1/stats/report", json=second, headers=auth_header(operator_token))
    assert resp.status_code == 200

    profile = client.get("/api/v1/public/players/abc123").json()
    assert profile["username"] == "SteveRenamed"
    assert profile["stats"]["kills"] == 9


def test_report_stats_playtime_daily_accumulates(client, operator_token):
    body_one = {"players": [{"uuid": "abc123", "username": "Steve", "playtime_daily": {"2026-08-15": 100}}]}
    body_two = {"players": [{"uuid": "abc123", "username": "Steve", "playtime_daily": {"2026-08-15": 50}}]}
    client.post("/api/v1/stats/report", json=body_one, headers=auth_header(operator_token))
    client.post("/api/v1/stats/report", json=body_two, headers=auth_header(operator_token))

    profile = client.get("/api/v1/public/players/abc123").json()
    assert profile["playtime_daily"] == [{"date": "2026-08-15", "seconds": 150}]
