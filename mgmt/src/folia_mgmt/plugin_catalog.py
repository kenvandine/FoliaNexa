"""Curated plugin catalog. PLAN.md §14.

The whole point: a world's `plugins` list is IDs into this catalog, not
free-typed strings that may or may not correspond to anything real.
Declaring a world validates every plugin ID against it (routers/worlds.py),
and `GET /worlds/{name}/plugins-manifest` generates the manifest
folia-smp-node fetches directly from these entries — no more hand-authoring
a separate manifest JSON file per world and hosting it somewhere.

Two sources, merged:
  1. The catalog bundled with the mgmt snap (`plugin_catalog_path`,
     defaults to this module's own directory — see config.py). This is
     "vetted, in the repo" — PR-reviewed alongside the code.
  2. An optional operator override/addition file at
     $SNAP_COMMON/plugin-catalog-override.yaml. Entries there with an
     `id` matching a bundled one replace it; new ids are appended. This
     is how "our own in-house plugins, maybe from a different repo" work
     without needing a new mgmt release for every plugin update — an
     entry's `download_url` can point anywhere (a separate GitHub repo's
     releases, Modrinth, Hangar, an internal artifact host), the catalog
     doesn't care, it's just an index.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from folia_mgmt.config import Settings


class PluginEntry(BaseModel):
    id: str
    category: str
    source: Literal["external", "in-house"]
    version: str
    download_url: str | None = None
    sha256: str | None = None
    homepage: str | None = None
    verified: bool = False
    notes: str | None = None


def _load_yaml_entries(path: Path) -> list[PluginEntry]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or []
    return [PluginEntry(**item) for item in raw]


def load_catalog(settings: Settings) -> list[PluginEntry]:
    bundled = _load_yaml_entries(settings.plugin_catalog_path)
    overrides = _load_yaml_entries(settings.state_dir / "plugin-catalog-override.yaml")

    by_id: dict[str, PluginEntry] = {entry.id: entry for entry in bundled}
    for entry in overrides:
        by_id[entry.id] = entry

    return sorted(by_id.values(), key=lambda entry: entry.id.lower())


def get_plugin(settings: Settings, plugin_id: str) -> PluginEntry | None:
    for entry in load_catalog(settings):
        if entry.id == plugin_id:
            return entry
    return None
