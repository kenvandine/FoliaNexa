from __future__ import annotations

from helpers import auth_header

PNG_64X64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAYElEQVR4nO3PQQ0AIBDAMMC/sJOF"
    "CB4Nyapg27P+dnTAqwa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0"
    "BrQGtAa0BrQGtAa0BrQGtAa0C/RXAUhw4L3GAAAAAElFTkSuQmCC"
)
PNG_32X32 = (
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKUlEQVR4nO3NMQEAAAjDMMC/MGRh"
    "gn2pgKa3sk34DwAAAAAAAAAAAN46CP0BCMDA+XQAAAAASUVORK5CYII="
)


def test_display_defaults_when_unset(client, viewer_token):
    resp = client.get("/api/v1/routes/display", headers=auth_header(viewer_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["motd"] == "<#09add3>A Velocity Server"
    assert body["icon_base64"] is None


def test_viewer_cannot_update_display(client, viewer_token):
    resp = client.put(
        "/api/v1/routes/display", json={"motd": "hi"}, headers=auth_header(viewer_token)
    )
    assert resp.status_code == 403


def test_operator_can_update_and_read_back_display(client, operator_token, viewer_token):
    resp = client.put(
        "/api/v1/routes/display",
        json={"motd": "<red>Sullivan SMP</red>", "icon_base64": PNG_64X64},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200
    assert resp.json() == {"motd": "<red>Sullivan SMP</red>", "icon_base64": PNG_64X64}

    resp = client.get("/api/v1/routes/display", headers=auth_header(viewer_token))
    assert resp.json() == {"motd": "<red>Sullivan SMP</red>", "icon_base64": PNG_64X64}


def test_update_rejects_wrong_size_icon(client, operator_token):
    resp = client.put(
        "/api/v1/routes/display",
        json={"motd": "hi", "icon_base64": PNG_32X32},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 400
    assert "64x64" in resp.json()["detail"]


def test_update_rejects_garbage_icon(client, operator_token):
    resp = client.put(
        "/api/v1/routes/display",
        json={"motd": "hi", "icon_base64": "not-a-png"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 400


def test_update_can_clear_icon(client, operator_token):
    client.put(
        "/api/v1/routes/display",
        json={"motd": "hi", "icon_base64": PNG_64X64},
        headers=auth_header(operator_token),
    )
    resp = client.put(
        "/api/v1/routes/display",
        json={"motd": "hi again", "icon_base64": None},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200
    assert resp.json()["icon_base64"] is None
