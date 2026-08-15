"""Curated plugin catalog API. PLAN.md §14."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from folia_mgmt.auth import require_viewer
from folia_mgmt.config import Settings
from folia_mgmt.deps import settings_dependency
from folia_mgmt.plugin_catalog import PluginEntry, load_catalog

router = APIRouter(prefix="/plugins", tags=["plugins"])


@router.get("", response_model=list[PluginEntry], dependencies=[Depends(require_viewer)])
def list_plugins(
    category: str | None = None, settings: Settings = Depends(settings_dependency)
) -> list[PluginEntry]:
    entries = load_catalog(settings)
    if category is not None:
        entries = [e for e in entries if e.category == category]
    return entries


@router.get("/{plugin_id}", response_model=PluginEntry, dependencies=[Depends(require_viewer)])
def get_plugin(plugin_id: str, settings: Settings = Depends(settings_dependency)) -> PluginEntry:
    for entry in load_catalog(settings):
        if entry.id == plugin_id:
            return entry
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such plugin '{plugin_id}' in the catalog")
