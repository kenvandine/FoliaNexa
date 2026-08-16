"""Curated plugin catalog API. PLAN.md §14.

GET is viewer-role (browsing the catalog is read-only, low-stakes); PUT/
DELETE are operator-role — they write to $SNAP_COMMON/plugin-catalog-
override.yaml (plugin_catalog.py), the same file an operator could always
hand-edit directly. This just puts a dashboard/API in front of that,
for adding a new plugin, bumping a download_url to a new version, or
flipping verified once its download's actually been checked.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from folia_mgmt.auth import require_operator, require_viewer
from folia_mgmt.config import Settings
from folia_mgmt.deps import settings_dependency
from folia_mgmt.plugin_catalog import (
    PluginEntry,
    delete_plugin_override,
    get_plugin,
    load_catalog,
    overridden_ids,
    save_plugin_override,
)

router = APIRouter(prefix="/plugins", tags=["plugins"])


class PluginCatalogEntryResponse(PluginEntry):
    # True if this id has an entry in the operator override file — i.e.
    # added/edited from the dashboard rather than only ever the bundled,
    # PR-reviewed catalog.yaml. Computed at read time, never itself
    # written back to either YAML file (see save_plugin_override, which
    # only ever persists real PluginEntry fields).
    is_override: bool = False


@router.get("", response_model=list[PluginCatalogEntryResponse], dependencies=[Depends(require_viewer)])
def list_plugins(
    category: str | None = None, settings: Settings = Depends(settings_dependency)
) -> list[PluginCatalogEntryResponse]:
    entries = load_catalog(settings)
    if category is not None:
        entries = [e for e in entries if e.category == category]
    overridden = overridden_ids(settings)
    return [PluginCatalogEntryResponse(**e.model_dump(), is_override=e.id in overridden) for e in entries]


@router.get("/{plugin_id}", response_model=PluginCatalogEntryResponse, dependencies=[Depends(require_viewer)])
def get_plugin_entry(plugin_id: str, settings: Settings = Depends(settings_dependency)) -> PluginCatalogEntryResponse:
    entry = get_plugin(settings, plugin_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such plugin '{plugin_id}' in the catalog")
    return PluginCatalogEntryResponse(**entry.model_dump(), is_override=plugin_id in overridden_ids(settings))


class UpsertPluginRequest(BaseModel):
    category: str
    source: Literal["external", "in-house"]
    version: str
    download_url: str | None = None
    sha256: str | None = None
    homepage: str | None = None
    verified: bool = False
    notes: str | None = None
    default_for_all_worlds: bool = False


@router.put("/{plugin_id}", response_model=PluginCatalogEntryResponse, dependencies=[Depends(require_operator)])
def upsert_plugin(
    plugin_id: str, body: UpsertPluginRequest, settings: Settings = Depends(settings_dependency)
) -> PluginCatalogEntryResponse:
    """Adds a new catalog entry, or edits an existing one (bundled or
    already-overridden) — either way the result is written to the
    operator override file, which always wins over the bundled catalog
    for this id (plugin_catalog.load_catalog). A world that already
    declares this plugin picks up a download_url/version change on its
    plugins-manifest's next fetch — see folia_node.staging's per-restart
    reconciliation, no mgmt restart needed for this to take effect."""
    try:
        entry = PluginEntry(id=plugin_id, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    save_plugin_override(settings, entry)
    return PluginCatalogEntryResponse(**entry.model_dump(), is_override=True)


@router.delete("/{plugin_id}", dependencies=[Depends(require_operator)])
def delete_plugin(plugin_id: str, settings: Settings = Depends(settings_dependency)) -> dict:
    """Removes plugin_id from the override file. If catalog.yaml also has
    this id, it falls back to that bundled version (a "reset to bundled"
    for an entry that was only ever edited, not added); if it was
    override-only, it disappears from the catalog entirely — a world that
    still declares it will just skip it in its plugins-manifest, same as
    any other id no longer resolvable in the catalog (get_plugins_manifest
    already logs and skips those, not a hard failure)."""
    removed = delete_plugin_override(settings, plugin_id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no override entry for '{plugin_id}' to remove")
    remaining = get_plugin(settings, plugin_id)
    return {"removed_override": True, "reverted_to_bundled": remaining is not None}
