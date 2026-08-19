"""Recursive listing of a plugin's folder inside a world's container, for
the plugin config file browser (PLAN.md — plugin config editing). Kept
separate from `lxd_client.py` since depth/count limits and path-joining
are application policy, not LXD API mechanics — `LXDClient.list_files` is
the one non-recursive primitive this builds on.
"""

from __future__ import annotations

from folia_mgmt.access_apply import NODE_WORLD_DIR
from folia_mgmt.lxd_client import Host, LXDClient, LXDError

MAX_DEPTH = 6
MAX_FILES = 500


def plugin_root(plugin_id: str) -> str:
    return f"{NODE_WORLD_DIR}/plugins/{plugin_id}"


def list_plugin_files(lxd_client: LXDClient, host: Host, container_name: str, plugin_id: str) -> list[str]:
    """Recursively walks plugins/<plugin_id>/ inside the container,
    returning sorted relative posix paths (e.g. "config.yml",
    "lang/en.yml"). Returns an empty list — not an error — if the
    plugin hasn't booted yet and has no folder at all, since that's a
    common, expected state rather than a failure."""
    root = plugin_root(plugin_id)
    try:
        root_entries = lxd_client.list_files(host, container_name, root)
    except LXDError:
        return []

    paths: list[str] = []
    _walk(lxd_client, host, container_name, root, "", root_entries, paths, depth=0)
    return sorted(paths)


def _walk(
    lxd_client: LXDClient,
    host: Host,
    container_name: str,
    absolute_dir: str,
    relative_dir: str,
    entries: list[str],
    paths: list[str],
    *,
    depth: int,
) -> None:
    if depth > MAX_DEPTH:
        return
    for entry in entries:
        if len(paths) >= MAX_FILES:
            return
        relative_path = f"{relative_dir}/{entry}" if relative_dir else entry
        absolute_path = f"{absolute_dir}/{entry}"
        try:
            # One call does double duty: a successful list_files both
            # confirms this entry is a directory and hands back its
            # children for the recursive step below, instead of a
            # separate probe call followed by a second, duplicate fetch.
            child_entries = lxd_client.list_files(host, container_name, absolute_path)
        except LXDError:
            # Not a directory (or vanished between listing and stat) — a file.
            paths.append(relative_path)
            continue
        _walk(lxd_client, host, container_name, absolute_path, relative_path, child_entries, paths, depth=depth + 1)
