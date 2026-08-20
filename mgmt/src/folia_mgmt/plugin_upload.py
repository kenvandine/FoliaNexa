"""Operator jar uploads for the plugin catalog (PLAN.md §14A) — for
commercial/licensed plugins with no public download URL a catalog entry
can point at (the whole point of `download_url` up to now). An operator
uploads the real .jar once from the dashboard; it's stored under
`settings.plugin_uploads_dir` and served back out at a random-token URL
(mounted unauthenticated in main.py, same "no credential, just an
unguessable path" model routers/worlds.py's plugins-manifest already
uses for node's own fetches) that becomes the catalog entry's
`download_url` — folia-nexa-node then downloads it exactly like any
other catalog entry, no special-casing needed on that side at all.

Jar inspection is best-effort metadata extraction, not validation: a
Bukkit/Spigot/Paper plugin.yml (or Paper's newer paper-plugin.yml) at the
jar root gives us `name`, `version`, and Paper's own `folia-supported`
flag, which lets the dashboard pre-fill the catalog form and surface a
"this plugin does not declare Folia support" warning up front, the same
check a couple of catalog.yaml entries' notes already describe doing by
hand.
"""

from __future__ import annotations

import hashlib
import io
import re
import secrets
import zipfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from folia_mgmt.config import Settings

# Generous but bounded — real plugin jars run from a few KB up to maybe
# 50-100MB for something bundling its own assets; this just stops an
# accidental (or malicious) multi-GB upload from filling the disk.
MAX_UPLOAD_BYTES = 250 * 1024 * 1024

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class InvalidJarError(Exception):
    pass


@dataclass
class JarMetadata:
    name: str | None
    version: str | None
    folia_supported: bool | None
    website: str | None


def inspect_jar(content: bytes) -> JarMetadata:
    """Best-effort read of plugin.yml/paper-plugin.yml out of an uploaded
    jar. Returns all-None fields (never raises) if the jar has neither —
    some valid jars (a bundle of several plugins, or one using a loader
    this doesn't recognize) just won't have anything to pre-fill from,
    which the caller treats as "let the operator fill the form in by
    hand," same as before this feature existed, not an upload failure.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = set(zf.namelist())
            for candidate in ("paper-plugin.yml", "plugin.yml"):
                if candidate in names:
                    raw = zf.read(candidate)
                    break
            else:
                return JarMetadata(name=None, version=None, folia_supported=None, website=None)
    except zipfile.BadZipFile as exc:
        raise InvalidJarError("not a valid jar/zip file") from exc

    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return JarMetadata(name=None, version=None, folia_supported=None, website=None)
    if not isinstance(data, dict):
        return JarMetadata(name=None, version=None, folia_supported=None, website=None)

    folia_supported = data.get("folia-supported")
    return JarMetadata(
        name=str(data["name"]) if data.get("name") else None,
        version=str(data["version"]) if data.get("version") else None,
        folia_supported=bool(folia_supported) if isinstance(folia_supported, bool) else None,
        website=str(data["website"]) if data.get("website") else None,
    )


def _safe_filename(original: str) -> str:
    name = Path(original).name or "plugin.jar"
    name = _SAFE_FILENAME.sub("_", name)
    if not name.lower().endswith(".jar"):
        name += ".jar"
    return name


def save_uploaded_jar(settings: Settings, original_filename: str, content: bytes) -> tuple[str, str]:
    """Persists content under a fresh random-token directory so the
    resulting path is unguessable (see module docstring). Returns
    (token, filename) — callers build the servable path from those two,
    never from the caller-supplied original_filename directly (sanitized
    here, but the caller still needs the sanitized value to build a URL
    that will actually resolve)."""
    token = secrets.token_urlsafe(24)
    filename = _safe_filename(original_filename)
    upload_dir = settings.plugin_uploads_dir / token
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / filename).write_bytes(content)
    return token, filename


def uploaded_jar_download_url(settings: Settings, token: str, filename: str) -> str:
    if not settings.public_url:
        raise ValueError("public_url must be set before an uploaded plugin jar can be linked to")
    return f"{settings.public_url.rstrip('/')}/plugin-jars/{token}/{filename}"


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
