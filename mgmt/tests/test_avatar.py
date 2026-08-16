from __future__ import annotations

import base64
import io
import json

import httpx
from PIL import Image

from folia_mgmt.avatar import (
    fetch_skin_texture_url,
    get_player_avatar,
    render_face_avatar,
)


def _make_skin_png(height: int = 64) -> bytes:
    # A real 64x{height} skin texture: red face region (8,8)-(16,16), a
    # partially-transparent blue hat overlay at (40,8)-(48,16) (only
    # meaningful for height>=64), everything else black.
    skin = Image.new("RGBA", (64, height), (0, 0, 0, 255))
    for x in range(8, 16):
        for y in range(8, 16):
            skin.putpixel((x, y), (255, 0, 0, 255))
    if height >= 64:
        for x in range(40, 48):
            for y in range(8, 16):
                skin.putpixel((x, y), (0, 0, 255, 128))
    buf = io.BytesIO()
    skin.save(buf, format="PNG")
    return buf.getvalue()


def test_render_face_avatar_crops_and_scales():
    png = render_face_avatar(_make_skin_png(height=64), size=32)
    img = Image.open(io.BytesIO(png))
    assert img.size == (32, 32)
    # Composited red base + semi-transparent blue overlay should shift the
    # pixel away from pure red, proving the hat layer was actually applied.
    r, g, b, a = img.getpixel((0, 0))
    assert r < 255
    assert b > 0


def test_render_face_avatar_legacy_skin_has_no_overlay():
    png = render_face_avatar(_make_skin_png(height=32), size=32)
    img = Image.open(io.BytesIO(png))
    assert img.size == (32, 32)
    assert img.getpixel((0, 0)) == (255, 0, 0, 255)


def test_fetch_skin_texture_url_parses_real_shaped_profile(monkeypatch):
    textures_value = base64.b64encode(
        json.dumps({"textures": {"SKIN": {"url": "https://textures.example/skin.png"}}}).encode()
    ).decode()

    def fake_get(url, timeout=None):
        assert "session/minecraft/profile/some-uuid" in url
        return httpx.Response(200, json={"properties": [{"name": "textures", "value": textures_value}]})

    monkeypatch.setattr("folia_mgmt.avatar.httpx.get", fake_get)
    assert fetch_skin_texture_url("some-uuid") == "https://textures.example/skin.png"


def test_fetch_skin_texture_url_returns_none_on_404(monkeypatch):
    monkeypatch.setattr("folia_mgmt.avatar.httpx.get", lambda url, timeout=None: httpx.Response(404))
    assert fetch_skin_texture_url("unknown-uuid") is None


def test_fetch_skin_texture_url_returns_none_on_network_error(monkeypatch):
    def raise_error(url, timeout=None):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr("folia_mgmt.avatar.httpx.get", raise_error)
    assert fetch_skin_texture_url("some-uuid") is None


def test_get_player_avatar_falls_back_to_placeholder_when_skin_unresolvable(monkeypatch):
    monkeypatch.setattr("folia_mgmt.avatar.fetch_skin_texture_url", lambda uuid, timeout=10.0: None)
    png = get_player_avatar("some-uuid", size=16)
    img = Image.open(io.BytesIO(png))
    assert img.size == (16, 16)


def test_get_player_avatar_renders_real_skin(monkeypatch):
    monkeypatch.setattr(
        "folia_mgmt.avatar.fetch_skin_texture_url",
        lambda uuid, timeout=10.0: "https://textures.example/skin.png",
    )
    skin_bytes = _make_skin_png(height=64)
    monkeypatch.setattr(
        "folia_mgmt.avatar.httpx.get",
        lambda url, timeout=None: httpx.Response(200, content=skin_bytes, request=httpx.Request("GET", url)),
    )
    png = get_player_avatar("some-uuid", size=48)
    img = Image.open(io.BytesIO(png))
    assert img.size == (48, 48)


def test_get_player_avatar_falls_back_on_malformed_texture(monkeypatch):
    monkeypatch.setattr(
        "folia_mgmt.avatar.fetch_skin_texture_url",
        lambda uuid, timeout=10.0: "https://textures.example/skin.png",
    )
    monkeypatch.setattr(
        "folia_mgmt.avatar.httpx.get",
        lambda url, timeout=None: httpx.Response(200, content=b"not a png", request=httpx.Request("GET", url)),
    )
    png = get_player_avatar("some-uuid", size=16)
    img = Image.open(io.BytesIO(png))
    assert img.size == (16, 16)
