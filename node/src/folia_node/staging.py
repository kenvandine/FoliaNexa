"""Downloads and stages the jar + plugins for this world. PLAN.md §9 step 2:
idempotent so a node-agent restart doesn't re-download an already-staged
world.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from folia_node.devlxd import WorldAssignment

logger = logging.getLogger(__name__)

JAR_FILENAME = "server.jar"
STAGED_MARKER = ".staged"


def _download(client: httpx.Client, url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)
    tmp.replace(dest)


def ensure_staged(
    world_dir: Path, assignment: WorldAssignment, client: httpx.Client | None = None
) -> Path:
    """Returns the path to the staged server jar. Downloads jar + plugins
    only if the '.staged' marker (written last, after everything else
    succeeds) isn't already present — a partial prior attempt is retried
    in full rather than trusted."""
    world_dir.mkdir(parents=True, exist_ok=True)
    jar_path = world_dir / JAR_FILENAME
    marker = world_dir / STAGED_MARKER

    if marker.exists():
        logger.info("world '%s' already staged, skipping download", assignment.world_name)
        return jar_path

    owns_client = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        logger.info("staging jar for '%s' from %s", assignment.world_name, assignment.jar_url)
        _download(client, assignment.jar_url, jar_path)

        if assignment.plugins_manifest_url:
            plugins_dir = world_dir / "plugins"
            plugins_dir.mkdir(exist_ok=True)
            resp = client.get(assignment.plugins_manifest_url)
            resp.raise_for_status()
            manifest = resp.json()  # [{"name": "...", "url": "..."}, ...]
            for entry in manifest:
                dest = plugins_dir / f"{entry['name']}.jar"
                logger.info("staging plugin '%s'", entry["name"])
                _download(client, entry["url"], dest)

        marker.write_text("ok")
    finally:
        if owns_client:
            client.close()

    return jar_path
