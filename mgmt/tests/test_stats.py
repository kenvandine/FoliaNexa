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


def test_report_stats_different_worlds_sum_independently(client, operator_token, app):
    # The actual fix: two different worlds reporting deltas for the same
    # player/stat must both contribute to the total — this is what the
    # single-report-source test above can't prove on its own (it never
    # sends a differing `world`, so it only exercises "the same bucket
    # accumulates," not "two worlds don't clobber each other").
    lobby = {"world": "lobby", "players": [{"uuid": "abc123", "username": "Steve", "stat_deltas": {"blocks_mined": 5}}]}
    overworld = {
        "world": "overworld",
        "players": [{"uuid": "abc123", "username": "Steve", "stat_deltas": {"blocks_mined": 1200}}],
    }
    client.post("/api/v1/stats/report", json=lobby, headers=auth_header(operator_token))
    client.post("/api/v1/stats/report", json=overworld, headers=auth_header(operator_token))

    profile = client.get("/api/v1/public/players/abc123").json()
    assert profile["stats"]["blocks_mined"] == 1205

    # A further report from just one of the two worlds only adds to its
    # own contribution — the other world's isn't touched or re-summed.
    # (Cache cleared between reads — public_api_cache_seconds defaults to
    # 30s, longer than this test takes to run, so a second GET without
    # this would just re-serve the first response.)
    client.post(
        "/api/v1/stats/report",
        json={"world": "lobby", "players": [{"uuid": "abc123", "username": "Steve", "stat_deltas": {"blocks_mined": 3}}]},
        headers=auth_header(operator_token),
    )
    app.state.public_stats_cache._store.clear()
    profile = client.get("/api/v1/public/players/abc123").json()
    assert profile["stats"]["blocks_mined"] == 1208


def test_report_stats_legacy_format_still_accepted(client, operator_token):
    # A not-yet-upgraded plugin build sends the old `stats` (absolute
    # total) shape and no `world` at all — must keep working exactly as
    # it always did, so an operator never has to upgrade every world in
    # lockstep.
    body = {"players": [{"uuid": "abc123", "username": "Steve", "stats": {"kills": 42}}]}
    resp = client.post("/api/v1/stats/report", json=body, headers=auth_header(operator_token))
    assert resp.status_code == 200

    profile = client.get("/api/v1/public/players/abc123").json()
    assert profile["stats"]["kills"] == 42


def test_report_stats_legacy_and_upgraded_worlds_dont_cross_contaminate(client, operator_token, app):
    # A legacy world (still overwriting) and an upgraded world (summing
    # its own deltas) reporting for the same player must combine
    # correctly, and neither format's writes should corrupt the other's.
    legacy = {"players": [{"uuid": "abc123", "username": "Steve", "stats": {"kills": 100}}]}
    upgraded = {"world": "overworld", "players": [{"uuid": "abc123", "username": "Steve", "stat_deltas": {"kills": 3}}]}
    client.post("/api/v1/stats/report", json=legacy, headers=auth_header(operator_token))
    client.post("/api/v1/stats/report", json=upgraded, headers=auth_header(operator_token))

    profile = client.get("/api/v1/public/players/abc123").json()
    assert profile["stats"]["kills"] == 103  # 100 (legacy) + 3 (overworld's delta)

    # A second legacy report only overwrites the legacy bucket's own
    # value — the upgraded world's 3 kills are still there, not clobbered.
    client.post(
        "/api/v1/stats/report",
        json={"players": [{"uuid": "abc123", "username": "Steve", "stats": {"kills": 150}}]},
        headers=auth_header(operator_token),
    )
    app.state.public_stats_cache._store.clear()  # see the sibling test above for why
    profile = client.get("/api/v1/public/players/abc123").json()
    assert profile["stats"]["kills"] == 153  # 150 (legacy, overwritten) + 3 (overworld, untouched)


def test_report_stats_playtime_daily_accumulates(client, operator_token):
    body_one = {"players": [{"uuid": "abc123", "username": "Steve", "playtime_daily": {"2026-08-15": 100}}]}
    body_two = {"players": [{"uuid": "abc123", "username": "Steve", "playtime_daily": {"2026-08-15": 50}}]}
    client.post("/api/v1/stats/report", json=body_one, headers=auth_header(operator_token))
    client.post("/api/v1/stats/report", json=body_two, headers=auth_header(operator_token))

    profile = client.get("/api/v1/public/players/abc123").json()
    assert profile["playtime_daily"] == [{"date": "2026-08-15", "seconds": 150}]
