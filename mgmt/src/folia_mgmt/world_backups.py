"""File-level world backups (PLAN.md §6A) — a tar.gz of the world save +
plugins/ (jars included, not just plugin data, so a restore brings back
the exact plugin versions that were running at backup time), fetched
from folia-nexa-node's own GET /backup endpoint over plain HTTP and
stored on *this* mgmt host's own disk under Settings.world_backups_dir —
never the world's own LXD host or container, so an rsync job pointed at
that directory from a separate backup host is a complete disaster-
recovery story for a lost mgmt node (see CLAUDE.md's World backups
entry).

Deliberately doesn't go through LXD's storage layer at all — see
lxd_client.py's LONG_OPERATION_TIMEOUT comment for why the older,
LXD-instance-snapshot-based mechanism (kept, but off by default —
Settings.lxd_snapshot_backups_enabled) no longer backs the tracked "time
machine" backup feature.
"""

from __future__ import annotations

import logging
import tarfile
from pathlib import Path

import httpx

from folia_mgmt.config import Settings
from folia_mgmt.models import World

logger = logging.getLogger(__name__)


class BackupTransferError(RuntimeError):
    pass


def backup_file_path(settings: Settings, world_name: str, label: str) -> Path:
    return settings.world_backups_dir / world_name / f"{label}.tar.gz"


def fetch_and_store_backup(settings: Settings, world: World, label: str) -> int:
    """Streams a tar.gz of `world`'s world save + plugins/ off its node
    agent's GET /backup endpoint and writes it to
    backup_file_path(settings, world.name, label). Returns the size in
    bytes. Raises BackupTransferError on any failure — connection
    refused, timeout, or a downloaded stream that doesn't open cleanly as
    a tar afterwards, since a truncated/corrupt download must not look
    like a good backup."""
    if not world.address:
        raise BackupTransferError(f"world '{world.name}' has no known address yet")
    container_ip = world.address.split(":", 1)[0]
    url = f"http://{container_ip}:{settings.node_health_port}/backup"

    path = backup_file_path(settings, world.name, label)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with httpx.stream("GET", url, timeout=settings.world_backup_fetch_timeout_seconds) as resp:
            if resp.status_code != 200:
                raise BackupTransferError(
                    f"node agent for world '{world.name}' returned HTTP {resp.status_code}"
                )
            with path.open("wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
    except httpx.HTTPError as exc:
        path.unlink(missing_ok=True)
        raise BackupTransferError(f"failed to fetch backup for world '{world.name}': {exc}") from exc

    try:
        with tarfile.open(path, mode="r:gz") as tf:
            tf.getmembers()  # forces a full read, catching a truncated/corrupt stream
    except (tarfile.TarError, OSError, EOFError) as exc:
        path.unlink(missing_ok=True)
        raise BackupTransferError(
            f"backup for world '{world.name}' downloaded but isn't a valid tar.gz: {exc}"
        ) from exc

    return path.stat().st_size


def delete_backup_file(settings: Settings, world_name: str, label: str) -> None:
    """Best-effort removal — mirrors plugin_upload.delete_uploaded_jar's
    never-raises pattern, since a failed cleanup shouldn't block whatever
    triggered it (pruning, a world/backup row being deleted)."""
    path = backup_file_path(settings, world_name, label)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("failed to remove stale world backup file %s", path, exc_info=True)
