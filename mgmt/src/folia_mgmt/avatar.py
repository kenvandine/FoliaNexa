"""Self-hosted player-face avatars for the public player hub (portal/,
PLAN.md §7A) — GET /api/v1/public/players/{uuid}/avatar.

Previously the portal linked straight to crafatar.com (a free, public
third-party avatar-rendering service) — simple, but a single point of
failure this project has zero control over. Confirmed live 2026-08-16:
crafatar.com was returning Cloudflare 521 ("web server is down") for
every UUID, including well-known ones (Notch's), not anything specific
to a UUID or request shape on our end — every avatar on the portal was
a broken image simultaneously. This renders the same kind of image
(cropped+composited player face) ourselves instead, from the same
public data (Mojang's session server), so the portal has no runtime
dependency on any third party for this at all.

Mojang's own skin texture format: a 64x64 (or legacy 64x32) PNG "skin"
image. The front-facing head is an 8x8 region at (8,8); a 64-tall skin
also has an optional second "hat" layer at (40,8) — semi-transparent
overlay pixels (hair, glasses, etc.) meant to render on top of the base
face. Both get composited and scaled up (nearest-neighbor, to keep the
blocky look rather than blurring it) to the requested output size.
"""

from __future__ import annotations

import base64
import io
import json
import logging

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

MOJANG_SESSION_API = "https://sessionserver.mojang.com"
FACE_REGION = (8, 8, 16, 16)
HAT_REGION = (40, 8, 48, 16)

# A flat, unmistakably-placeholder gray square — shown when a real skin
# can't be resolved at all (Mojang unreachable, unknown UUID, malformed
# profile) rather than erroring the whole portal page. Built once at
# import time since it never changes.
_PLACEHOLDER_COLOR = (60, 64, 74, 255)  # matches the dashboard's --panel-ish tone


def _placeholder_avatar(size: int) -> bytes:
    img = Image.new("RGBA", (size, size), _PLACEHOLDER_COLOR)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def fetch_skin_texture_url(uuid: str, timeout: float = 10.0) -> str | None:
    """Looks up uuid's current skin texture URL via Mojang's session
    server. Returns None on any failure (network error, unknown UUID,
    no textures property) — never raises, since a missing skin is an
    expected, common case (default Steve/Alex skin, or Mojang briefly
    unreachable), not an error condition callers need to handle
    specially beyond falling back to a placeholder.
    """
    try:
        resp = httpx.get(
            f"{MOJANG_SESSION_API}/session/minecraft/profile/{uuid}", timeout=timeout
        )
    except httpx.HTTPError:
        logger.warning("failed to reach Mojang session server for uuid '%s'", uuid)
        return None
    if resp.status_code != 200:
        return None

    try:
        properties = resp.json().get("properties", [])
        textures_prop = next((p for p in properties if p.get("name") == "textures"), None)
        if textures_prop is None:
            return None
        decoded = json.loads(base64.b64decode(textures_prop["value"]))
        return decoded.get("textures", {}).get("SKIN", {}).get("url")
    except (ValueError, KeyError, TypeError):
        logger.warning("unexpected shape in Mojang session profile for uuid '%s'", uuid)
        return None


def render_face_avatar(skin_png_bytes: bytes, size: int = 64) -> bytes:
    """Crops the front-facing head (plus its hat-layer overlay, if
    present) out of a raw Minecraft skin texture and scales it up to a
    size x size PNG."""
    skin = Image.open(io.BytesIO(skin_png_bytes)).convert("RGBA")
    face = skin.crop(FACE_REGION)
    if skin.height >= 64:
        hat = skin.crop(HAT_REGION)
        face = Image.alpha_composite(face, hat)
    face = face.resize((size, size), Image.NEAREST)
    buf = io.BytesIO()
    face.save(buf, format="PNG")
    return buf.getvalue()


def get_player_avatar(uuid: str, size: int = 64, timeout: float = 10.0) -> bytes:
    """The one function callers actually need — resolves uuid all the
    way to a ready-to-serve PNG, falling back to a flat placeholder
    rather than ever raising, so a broken/unreachable Mojang lookup
    degrades to "one gray square" instead of a broken image or a 500."""
    texture_url = fetch_skin_texture_url(uuid, timeout=timeout)
    if texture_url is None:
        return _placeholder_avatar(size)

    try:
        resp = httpx.get(texture_url, timeout=timeout)
        resp.raise_for_status()
        return render_face_avatar(resp.content, size=size)
    except (httpx.HTTPError, OSError) as exc:
        # OSError also covers Pillow's own "cannot identify image file"
        # for a malformed/truncated download — same fallback either way.
        logger.warning("failed to fetch/render skin texture for uuid '%s': %s", uuid, exc)
        return _placeholder_avatar(size)
