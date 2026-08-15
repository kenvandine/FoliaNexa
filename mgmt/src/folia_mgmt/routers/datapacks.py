"""Curated data pack catalog API. Mirrors routers/plugins.py — see
datapack_catalog.py's module docstring for why data packs get their own
catalog/router instead of folding into the plugin one."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from folia_mgmt.auth import require_viewer
from folia_mgmt.config import Settings
from folia_mgmt.datapack_catalog import DatapackEntry, load_catalog
from folia_mgmt.deps import settings_dependency

router = APIRouter(prefix="/datapacks", tags=["datapacks"])


@router.get("", response_model=list[DatapackEntry], dependencies=[Depends(require_viewer)])
def list_datapacks(
    category: str | None = None, settings: Settings = Depends(settings_dependency)
) -> list[DatapackEntry]:
    entries = load_catalog(settings)
    if category is not None:
        entries = [e for e in entries if e.category == category]
    return entries


@router.get("/{datapack_id}", response_model=DatapackEntry, dependencies=[Depends(require_viewer)])
def get_datapack(datapack_id: str, settings: Settings = Depends(settings_dependency)) -> DatapackEntry:
    for entry in load_catalog(settings):
        if entry.id == datapack_id:
            return entry
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such datapack '{datapack_id}' in the catalog")
