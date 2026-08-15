from __future__ import annotations

from helpers import auth_header


def _seed(client, operator_token):
    body = {
        "players": [
            {"uuid": "uuid-a", "username": "Alice", "stats": {"kills": 10}},
            {"uuid": "uuid-b", "username": "Bob", "stats": {"kills": 30}},
            {"uuid": "uuid-c", "username": "Carol", "stats": {"kills": 20}},
        ]
    }
    resp = client.post("/api/v1/stats/report", json=body, headers=auth_header(operator_token))
    assert resp.status_code == 200, resp.text


def test_public_endpoints_require_no_auth(client, operator_token):
    _seed(client, operator_token)

    resp = client.get("/api/v1/public/leaderboards", params={"stat": "kills"})
    assert resp.status_code == 200, resp.text

    resp = client.get("/api/v1/public/players")
    assert resp.status_code == 200

    resp = client.get("/api/v1/public/players/uuid-a")
    assert resp.status_code == 200


def test_leaderboard_orders_by_value_descending(client, operator_token):
    _seed(client, operator_token)

    resp = client.get("/api/v1/public/leaderboards", params={"stat": "kills"})
    body = resp.json()
    assert body["stat"] == "kills"
    assert [e["username"] for e in body["entries"]] == ["Bob", "Carol", "Alice"]


def test_leaderboard_respects_limit(client, operator_token):
    _seed(client, operator_token)

    resp = client.get("/api/v1/public/leaderboards", params={"stat": "kills", "limit": 2})
    assert len(resp.json()["entries"]) == 2


def test_leaderboard_unknown_stat_returns_empty(client):
    resp = client.get("/api/v1/public/leaderboards", params={"stat": "nonexistent"})
    assert resp.status_code == 200
    assert resp.json()["entries"] == []


def test_list_players(client, operator_token):
    _seed(client, operator_token)

    resp = client.get("/api/v1/public/players")
    usernames = {p["username"] for p in resp.json()["players"]}
    assert usernames == {"Alice", "Bob", "Carol"}


def test_get_unknown_player_404s(client):
    resp = client.get("/api/v1/public/players/does-not-exist")
    assert resp.status_code == 404


def test_leaderboard_response_is_cached(client, operator_token, app, monkeypatch):
    _seed(client, operator_token)

    first = client.get("/api/v1/public/leaderboards", params={"stat": "kills"}).json()

    # Report a change that would flip the ordering if the cache weren't
    # in effect, then confirm the cached response is still served.
    client.post(
        "/api/v1/stats/report",
        json={"players": [{"uuid": "uuid-a", "username": "Alice", "stats": {"kills": 999}}]},
        headers=auth_header(operator_token),
    )
    second = client.get("/api/v1/public/leaderboards", params={"stat": "kills"}).json()
    assert second == first

    # Clearing the app-scoped cache directly (rather than sleeping past the
    # real TTL) proves it's this cache's doing, not coincidence.
    app.state.public_stats_cache._store.clear()
    third = client.get("/api/v1/public/leaderboards", params={"stat": "kills"}).json()
    assert third["entries"][0]["username"] == "Alice"


def test_public_rate_limit_enforced(client, app):
    # Drop the limit to something the test can exceed in a handful of calls.
    from folia_mgmt import deps

    original = deps.settings_dependency

    def tiny_limit():
        settings = original()
        settings.public_api_rate_limit_per_minute = 2
        return settings

    client.app.dependency_overrides[deps.settings_dependency] = tiny_limit
    try:
        client.get("/api/v1/public/players")
        client.get("/api/v1/public/players")
        resp = client.get("/api/v1/public/players")
        assert resp.status_code == 429
    finally:
        del client.app.dependency_overrides[deps.settings_dependency]
