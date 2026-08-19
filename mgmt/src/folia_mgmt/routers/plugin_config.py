"""View/edit a world's plugin config files, live from the dashboard.

mgmt stores an operator's edit as the source of truth (`WorldPluginConfigFile`,
models.py) and re-pushes it on every reconcile tick the same way
luckperms.py's config is (see scheduler.py's `sync_plugin_config_files`) —
so an edit survives world restarts/migrations rather than being a one-shot
live push. Viewing falls back to reading the live file over LXD when mgmt
has no stored override yet, since most plugins only write their own
config.yml after their own first boot (node's staging step never handles
plugin config, see node/src/folia_node/staging.py).
"""

from __future__ import annotations

import posixpath

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.auth import User, require_operator, require_viewer
from folia_mgmt.db import get_session
from folia_mgmt.deps import get_lxd_client
from folia_mgmt.lxd_client import LXDClient, LXDError
from folia_mgmt.models import Host, World, WorldPluginConfigFile, utcnow
from folia_mgmt.plugin_files import list_plugin_files, plugin_root

router = APIRouter(prefix="/worlds", tags=["plugin-config"])


def _get_world_or_404(session: Session, name: str) -> World:
    world = session.exec(select(World).where(World.name == name)).first()
    if world is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such world '{name}'")
    return world


def _require_declared_plugin(world: World, plugin_id: str) -> None:
    if plugin_id not in world.plugins:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"plugin '{plugin_id}' is not declared on world '{world.name}'"
        )


def _safe_relative_path(path: str) -> str:
    """Rejects anything that would escape the plugin's own folder — a
    viewer could otherwise GET (and an operator PUT/DELETE) arbitrary
    paths inside the world's container via '..' segments in the URL's
    catch-all {path:path} segment. `posixpath.normpath` collapses '.'/
    '..' components; the normalized result escaping upward (or being
    absolute) is the only thing that needs rejecting — everything that
    stays within the plugin's own folder is left as-is."""
    normalized = posixpath.normpath(path)
    if normalized in (".", "") or normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid file path: '{path}'")
    return normalized


def _host_for(session: Session, world: World) -> Host | None:
    """None means "not currently reachable" (unplaced, or its host record
    is somehow missing) — every call site treats that as "fall back to
    whatever mgmt has stored," not as an error."""
    if not world.host_name or not world.container_name:
        return None
    return session.exec(select(Host).where(Host.name == world.host_name)).first()


def _override(session: Session, world_name: str, plugin_id: str, path: str) -> WorldPluginConfigFile | None:
    return session.exec(
        select(WorldPluginConfigFile).where(
            WorldPluginConfigFile.world_name == world_name,
            WorldPluginConfigFile.plugin_id == plugin_id,
            WorldPluginConfigFile.path == path,
        )
    ).first()


class FileEntry(BaseModel):
    path: str
    source: str  # "live" | "override" | "both"


class FileListResponse(BaseModel):
    files: list[FileEntry]


class FileContentResponse(BaseModel):
    path: str
    content: str | None
    is_binary: bool
    source: str  # "override" | "live" | "none"
    updated_at: str | None = None
    updated_by: str | None = None


class FileUpdateRequest(BaseModel):
    content: str


class FileUpdateResponse(BaseModel):
    path: str
    pushed_live: bool


@router.get(
    "/{name}/plugins/{plugin_id}/files",
    response_model=FileListResponse,
    dependencies=[Depends(require_viewer)],
)
def list_files(
    name: str,
    plugin_id: str,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> FileListResponse:
    world = _get_world_or_404(session, name)
    _require_declared_plugin(world, plugin_id)

    live_paths: set[str] = set()
    host = _host_for(session, world)
    if host is not None:
        try:
            live_paths = set(list_plugin_files(lxd_client, host, world.container_name, plugin_id))
        except LXDError:
            pass

    override_paths = {
        row.path
        for row in session.exec(
            select(WorldPluginConfigFile).where(
                WorldPluginConfigFile.world_name == name,
                WorldPluginConfigFile.plugin_id == plugin_id,
            )
        ).all()
    }

    entries = []
    for path in sorted(live_paths | override_paths):
        if path in live_paths and path in override_paths:
            source = "both"
        elif path in override_paths:
            source = "override"
        else:
            source = "live"
        entries.append(FileEntry(path=path, source=source))
    return FileListResponse(files=entries)


@router.get(
    "/{name}/plugins/{plugin_id}/files/{path:path}",
    response_model=FileContentResponse,
    dependencies=[Depends(require_viewer)],
)
def get_file(
    name: str,
    plugin_id: str,
    path: str,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> FileContentResponse:
    world = _get_world_or_404(session, name)
    _require_declared_plugin(world, plugin_id)
    path = _safe_relative_path(path)

    override = _override(session, name, plugin_id, path)
    if override is not None:
        text, is_binary = _try_decode(override.content)
        return FileContentResponse(
            path=path,
            content=text,
            is_binary=is_binary,
            source="override",
            updated_at=override.updated_at.isoformat(),
            updated_by=override.updated_by,
        )

    host = _host_for(session, world)
    if host is not None:
        try:
            raw = lxd_client.read_file(host, world.container_name, f"{plugin_root(plugin_id)}/{path}")
        except LXDError:
            raw = None
        if raw is not None:
            text, is_binary = _try_decode(raw)
            return FileContentResponse(path=path, content=text, is_binary=is_binary, source="live")

    raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such file '{path}' for plugin '{plugin_id}' on '{name}'")


def _try_decode(raw: bytes) -> tuple[str | None, bool]:
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return None, True


@router.put(
    "/{name}/plugins/{plugin_id}/files/{path:path}",
    response_model=FileUpdateResponse,
)
def put_file(
    name: str,
    plugin_id: str,
    path: str,
    body: FileUpdateRequest,
    user: User = Depends(require_operator),
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> FileUpdateResponse:
    world = _get_world_or_404(session, name)
    _require_declared_plugin(world, plugin_id)
    path = _safe_relative_path(path)

    content = body.content.encode("utf-8")
    existing = _override(session, name, plugin_id, path)
    if existing is None:
        existing = WorldPluginConfigFile(world_name=name, plugin_id=plugin_id, path=path, content=content, updated_by=user.username)
    else:
        existing.content = content
        existing.updated_by = user.username
        existing.updated_at = utcnow()
    session.add(existing)
    session.commit()

    pushed_live = False
    host = _host_for(session, world)
    if host is not None:
        try:
            lxd_client.push_file(host, world.container_name, f"{plugin_root(plugin_id)}/{path}", content)
            pushed_live = True
        except LXDError:
            # Saved either way — the reconcile loop (scheduler.py's
            # sync_plugin_config_files) retries on every tick, same
            # swallow-and-retry pattern luckperms.py already uses.
            pass

    return FileUpdateResponse(path=path, pushed_live=pushed_live)


@router.delete(
    "/{name}/plugins/{plugin_id}/files/{path:path}",
    dependencies=[Depends(require_operator)],
)
def delete_file_override(
    name: str,
    plugin_id: str,
    path: str,
    session: Session = Depends(get_session),
) -> dict:
    world = _get_world_or_404(session, name)
    _require_declared_plugin(world, plugin_id)
    path = _safe_relative_path(path)

    existing = _override(session, name, plugin_id, path)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no mgmt override for '{path}' to revert")
    session.delete(existing)
    session.commit()
    return {"reverted": path}
