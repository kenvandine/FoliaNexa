from __future__ import annotations

from datetime import timedelta

from helpers import auth_header


def _enroll_host(client, admin_token, **overrides):
    join = client.post("/api/v1/hosts/join-token", headers=auth_header(admin_token)).json()["token"]
    body = {
        "name": "node-a",
        "address": "10.0.1.11:8443",
        "project": "folia",
        "lxd_trust_token": "good-token",
        "capacity": {"cpu_cores": 6, "memory_gb": 12},
        "labels": {},
    }
    body.update(overrides)
    resp = client.post("/api/v1/hosts/enroll", json=body, headers=auth_header(join))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _report_presence(client, viewer_token, world, players):
    body = {"worlds": [{"world": world, "players": players}]}
    resp = client.post("/api/v1/presence/report", json=body, headers=auth_header(viewer_token))
    assert resp.status_code == 200, resp.text


def test_worlds_online_lists_running_worlds_with_zero_players_by_default(client, admin_token, operator_token):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    resp = client.get("/api/v1/public/worlds")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_players"] == 0
    assert body["worlds"] == [{"world": "world-overworld", "type": "overworld", "player_count": 0, "players": []}]


def test_worlds_online_reflects_reported_presence(client, admin_token, operator_token, viewer_token):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    _report_presence(
        client,
        viewer_token,
        "world-overworld",
        [{"uuid": "uuid-a", "username": "Alice"}, {"uuid": "uuid-b", "username": "Bob"}],
    )

    resp = client.get("/api/v1/public/worlds")
    body = resp.json()
    assert body["total_players"] == 2
    [world] = body["worlds"]
    assert world["player_count"] == 2
    assert {p["username"] for p in world["players"]} == {"Alice", "Bob"}


def test_worlds_online_excludes_non_routable_and_non_running_worlds(client, admin_token, operator_token, viewer_token):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    # stays pending: not enough capacity left on node-a
    client.post(
        "/api/v1/worlds",
        json={"name": "world-huge", "type": "minigame", "cpu_cores": 10, "memory_gb": 10},
        headers=auth_header(operator_token),
    )

    resp = client.get("/api/v1/public/worlds")
    names = {w["world"] for w in resp.json()["worlds"]}
    assert names == {"world-overworld"}


def test_worlds_online_treats_stale_presence_as_empty(client, admin_token, operator_token, viewer_token, db_session):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    _report_presence(client, viewer_token, "world-overworld", [{"uuid": "uuid-a", "username": "Alice"}])

    from folia_mgmt.models import WorldPresence, utcnow

    presence = db_session.get(WorldPresence, "world-overworld")
    presence.updated_at = utcnow() - timedelta(seconds=999)
    db_session.add(presence)
    db_session.commit()

    resp = client.get("/api/v1/public/worlds")
    [world] = resp.json()["worlds"]
    assert world["player_count"] == 0
    assert world["players"] == []


def _seed(client, operator_token):
    body = {
        "players": [
            {"uuid": "uuid-a", "username": "Alice", "stat_deltas": {"kills": 10}},
            {"uuid": "uuid-b", "username": "Bob", "stat_deltas": {"kills": 30}},
            {"uuid": "uuid-c", "username": "Carol", "stat_deltas": {"kills": 20}},
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


def test_list_players_total_is_a_real_count_not_capped_by_limit(client, operator_token):
    # total must reflect every known player, not len(players) — the
    # portal's "All known players (N)" needs the true count even when
    # `limit` (default 50, max 200) is smaller than how many exist.
    _seed(client, operator_token)

    resp = client.get("/api/v1/public/players", params={"limit": 2})
    body = resp.json()
    assert len(body["players"]) == 2
    assert body["total"] == 3


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
        json={"players": [{"uuid": "uuid-a", "username": "Alice", "stat_deltas": {"kills": 999}}]},
        headers=auth_header(operator_token),
    )
    second = client.get("/api/v1/public/leaderboards", params={"stat": "kills"}).json()
    assert second == first

    # Clearing the app-scoped cache directly (rather than sleeping past the
    # real TTL) proves it's this cache's doing, not coincidence.
    app.state.public_stats_cache._store.clear()
    third = client.get("/api/v1/public/leaderboards", params={"stat": "kills"}).json()
    assert third["entries"][0]["username"] == "Alice"


def test_get_player_avatar_returns_png(client, monkeypatch):
    from folia_mgmt.routers import public_stats

    monkeypatch.setattr(public_stats, "get_player_avatar", lambda uuid, size=64: b"fake-png-bytes")

    resp = client.get("/api/v1/public/players/some-uuid/avatar")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == b"fake-png-bytes"
    assert "max-age" in resp.headers["cache-control"]


def test_get_player_avatar_respects_size_param(client, monkeypatch):
    from folia_mgmt.routers import public_stats

    seen_sizes = []

    def fake_get_player_avatar(uuid, size=64):
        seen_sizes.append(size)
        return b"fake-png-bytes"

    monkeypatch.setattr(public_stats, "get_player_avatar", fake_get_player_avatar)

    client.get("/api/v1/public/players/some-uuid/avatar", params={"size": 128})
    assert seen_sizes == [128]


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
