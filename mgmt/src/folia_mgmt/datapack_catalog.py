"""Curated data pack catalog. Same pattern as plugin_catalog.py (PLAN.md
§14), for vanilla data packs (e.g. Matcha) instead of Bukkit/Paper plugins.

Kept as a separate module/catalog file rather than folded into
PluginEntry/catalog.yaml: a data pack has no jar, isn't loaded by the
Paper/Bukkit plugin loader, and stages into a completely different
location on disk (a world save's `datapacks/` folder, not the server
root's `plugins/` — see folia_node.staging). Reusing the exact same
"id -> download_url/sha256, catalog + operator override, manifest
endpoint" shape means node's staging logic and the dashboard/CLI need
almost no new concepts, just a second instance of each.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from folia_mgmt.config import Settings


class DatapackEntry(BaseModel):
    id: str
    category: str
    source: Literal["external", "in-house"]
    version: str
    download_url: str | None = None
    sha256: str | None = None
    homepage: str | None = None
    verified: bool = False
    notes: str | None = None


def _load_yaml_entries(path: Path) -> list[DatapackEntry]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or []
    return [DatapackEntry(**item) for item in raw]


def load_catalog(settings: Settings) -> list[DatapackEntry]:
    bundled = _load_yaml_entries(settings.datapack_catalog_path)
    overrides = _load_yaml_entries(settings.state_dir / "datapack-catalog-override.yaml")

    by_id: dict[str, DatapackEntry] = {entry.id: entry for entry in bundled}
    for entry in overrides:
        by_id[entry.id] = entry

    return sorted(by_id.values(), key=lambda entry: entry.id.lower())


def get_datapack(settings: Settings, datapack_id: str) -> DatapackEntry | None:
    for entry in load_catalog(settings):
        if entry.id == datapack_id:
            return entry
    return None
