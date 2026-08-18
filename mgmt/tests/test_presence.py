from __future__ import annotations

from helpers import auth_header


def test_report_presence_requires_auth(client):
    resp = client.post("/api/v1/presence/report", json={"worlds": []})
    assert resp.status_code == 401


def test_report_presence_allows_viewer_role(client, viewer_token):
    # The proxy's own service account only ever holds viewer (CLAUDE.md
    # Phase 7) — this endpoint must accept that, not require operator.
    resp = client.post("/api/v1/presence/report", json={"worlds": []}, headers=auth_header(viewer_token))
    assert resp.status_code == 200, resp.text


def test_report_presence_upserts_players(client, viewer_token):
    body = {
        "worlds": [
            {
                "world": "world-overworld",
                "players": [
                    {"uuid": "uuid-a", "username": "Alice"},
                    {"uuid": "uuid-b", "username": "Bob"},
                ],
            }
        ]
    }
    resp = client.post("/api/v1/presence/report", json=body, headers=auth_header(viewer_token))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"worlds_updated": 1}


def test_report_presence_replaces_previous_snapshot(client, viewer_token, db_session):
    first = {"worlds": [{"world": "world-overworld", "players": [{"uuid": "uuid-a", "username": "Alice"}]}]}
    client.post("/api/v1/presence/report", json=first, headers=auth_header(viewer_token))

    second = {"worlds": [{"world": "world-overworld", "players": []}]}
    resp = client.post("/api/v1/presence/report", json=second, headers=auth_header(viewer_token))
    assert resp.status_code == 200

    from folia_mgmt.models import WorldPresence

    presence = db_session.get(WorldPresence, "world-overworld")
    assert presence.players == []
