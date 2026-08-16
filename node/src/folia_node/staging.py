"""Downloads and stages the jar + plugins + datapacks + server.properties
for this world. PLAN.md §9 step 2.

Two separate concerns, on purpose:

- `ensure_staged`: first-boot-only setup (jar download, EULA acceptance)
  — idempotent via the `.staged` marker, so a node-agent restart never
  re-downloads an already-staged world's jar. Re-running this after first
  boot would be actively wrong (re-fetching a possibly-different engine
  jar out from under an existing world save).
- `sync_world_config`: plugins/datapacks/server.properties reconciliation
  against mgmt's current manifests — runs on *every* agent start,
  including the first one, so a PATCH /worlds/{name} edit (routers/
  worlds.py) actually takes effect the next time the world's container
  restarts, not just at initial creation. Safe to re-run any number of
  times; each call reflects the manifests' current content, nothing more.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from folia_node.devlxd import WorldAssignment

logger = logging.getLogger(__name__)

JAR_FILENAME = "server.jar"
STAGED_MARKER = ".staged"

# Vanilla/Paper/Folia's default level-name ("world" in server.properties)
# — this codebase doesn't template server.properties at all (the server
# generates its own save folder on first boot), so there's no way to know
# a world's actual level-name ahead of time. Data packs have to land in
# <level-name>/datapacks/ before first boot for the server to pick them up
# as part of world generation, so this assumes the vanilla default. A
# world declared with a custom level-name in server.properties (nothing
# in this codebase sets one — level-name is one of the protected keys
# routers/worlds.py's _validate_properties rejects, exactly because of
# this assumption) would need its datapacks staged elsewhere — not
# something node can detect today.
LEVEL_NAME = "world"

# Written unconditionally, after any operator-supplied properties are
# merged in — never operator-overridable (routers/worlds.py's
# _validate_properties rejects "online-mode" outright, this is just
# defense in depth against that invariant ever slipping). Without this, a
# world behind folia-nexa-proxy's Velocity refuses the connection outright
# with "IllegalStateException: Backend server is online-mode!" — Paper
# won't accept a proxied connection from a server it hasn't been told to
# trust. This plus the matching proxies.velocity block in paper-global.yml
# below is what Paper's own docs call "modern" forwarding:
# https://docs.papermc.io/velocity/player-information-forwarding/
_REQUIRED_PROPERTIES = {"online-mode": "false"}


def _read_server_properties(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    props = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        props[key.strip()] = value.strip()
    return props


def _write_server_properties(world_dir: Path, overrides: dict[str, str]) -> None:
    # Merge, never replace outright — Paper writes dozens of its own keys
    # into this file beyond first boot (difficulty, motd, view-distance,
    # ...) that this codebase never templates and has no business
    # clobbering just because an operator changed one unrelated property.
    path = world_dir / "server.properties"
    merged = {**_read_server_properties(path), **overrides, **_REQUIRED_PROPERTIES}
    lines = [f"{key}={value}" for key, value in sorted(merged.items())]
    path.write_text("\n".join(lines) + "\n")


def _write_paper_global_config(world_dir: Path, forwarding_secret: str) -> None:
    # forwarding_secret is mgmt-generated (secrets.token_urlsafe, see
    # Settings.get_velocity_forwarding_secret) — URL-safe base64, never
    # contains a character YAML single-quoting can't handle plainly.
    config_dir = world_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "paper-global.yml").write_text(
        "proxies:\n"
        "  velocity:\n"
        "    enabled: true\n"
        "    online-mode: true\n"
        f"    secret: '{forwarding_secret}'\n"
    )


def _download(client: httpx.Client, url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)
    tmp.replace(dest)


def _reconcile_manifest_dir(client: httpx.Client, manifest_url: str, target_dir: Path, suffix: str) -> None:
    """Makes target_dir's `{name}{suffix}` files match the manifest exactly
    — downloads anything missing, deletes anything no longer listed.
    Existing files for entries still in the manifest are left alone (no
    forced re-download just because a restart happened; a catalog version
    bump doesn't retroactively update an already-staged world).

    Deletion is scoped to exactly this dir's top level, matching only
    `*{suffix}` files — a plugin/data pack's own subdirectory (config
    files, etc.) is never touched. An operator-placed file that happens to
    share a name with a since-removed catalog entry is indistinguishable
    from a stale managed one, though — this reconciliation assumes
    target_dir's `{suffix}` files are entirely mgmt-managed, matching
    _validate_plugins/_validate_datapacks' "no free-typed names" model.
    """
    resp = client.get(manifest_url)
    resp.raise_for_status()
    manifest = resp.json()  # [{"name": "...", "url": "..."}, ...]
    desired = {entry["name"]: entry["url"] for entry in manifest}

    target_dir.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in target_dir.glob(f"*{suffix}")}

    for name in existing - desired.keys():
        stale = target_dir / f"{name}{suffix}"
        logger.info("removing '%s' (no longer in manifest)", stale.name)
        stale.unlink(missing_ok=True)

    for name, url in desired.items():
        dest = target_dir / f"{name}{suffix}"
        if dest.exists():
            continue
        logger.info("staging '%s' from %s", dest.name, url)
        _download(client, url, dest)


def sync_world_config(world_dir: Path, assignment: WorldAssignment, client: httpx.Client | None = None) -> None:
    """Reconciles plugins/, <level-name>/datapacks/, and server.properties
    against mgmt's current manifests. Called on every agent start (see
    agent.py) — never gated by STAGED_MARKER, unlike ensure_staged."""
    owns_client = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        if assignment.plugins_manifest_url:
            _reconcile_manifest_dir(client, assignment.plugins_manifest_url, world_dir / "plugins", ".jar")

        if assignment.datapacks_manifest_url:
            # See LEVEL_NAME's own comment — this assumes the vanilla
            # default level-name, and (same caveat noted there) a datapack
            # zip's *removal* here doesn't retroactively undo effects
            # already baked into the world save at generation time; it
            # only keeps the files on disk in sync with the manifest.
            _reconcile_manifest_dir(
                client, assignment.datapacks_manifest_url, world_dir / LEVEL_NAME / "datapacks", ".zip"
            )

        if assignment.server_properties_url:
            try:
                resp = client.get(assignment.server_properties_url)
                resp.raise_for_status()
                overrides = resp.json()  # {"key": "value", ...}
            except httpx.HTTPError:
                # Keep whatever's already on disk rather than truncating it
                # to just the required baseline on a transient mgmt outage
                # — same "last known good" reasoning as folia-routes-sync's
                # polling (proxy/.../FoliaRoutesSyncPlugin.java). Only
                # exception: nothing on disk yet at all (first boot), where
                # *some* valid server.properties is required for the JVM
                # to start correctly.
                logger.warning("failed to fetch server-properties-manifest, keeping current server.properties")
                if not (world_dir / "server.properties").exists():
                    _write_server_properties(world_dir, {})
            else:
                _write_server_properties(world_dir, overrides)
        elif not (world_dir / "server.properties").exists():
            _write_server_properties(world_dir, {})
    finally:
        if owns_client:
            client.close()


def ensure_staged(
    world_dir: Path, assignment: WorldAssignment, client: httpx.Client | None = None
) -> Path:
    """Returns the path to the staged server jar. Downloads the jar and
    accepts the EULA only if the '.staged' marker (written last, after
    everything else succeeds) isn't already present — a partial prior
    attempt is retried in full rather than trusted. Plugins/datapacks/
    server.properties are handled separately by sync_world_config, called
    unconditionally by agent.py regardless of this marker."""
    world_dir.mkdir(parents=True, exist_ok=True)
    jar_path = world_dir / JAR_FILENAME
    marker = world_dir / STAGED_MARKER

    if marker.exists():
        logger.info("world '%s' already staged, skipping jar download", assignment.world_name)
        return jar_path

    owns_client = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        logger.info("staging jar for '%s' from %s", assignment.world_name, assignment.jar_url)
        _download(client, assignment.jar_url, jar_path)

        # Confirmed for real: without this, every world's JVM starts, logs
        # "You need to agree to the EULA", and exits cleanly (code 0) —
        # node's on-failure restart then just loops forever, never
        # surfacing as an obvious error. Writing eula=true here is the
        # cluster operator's own acceptance of Mojang's EULA
        # (https://www.minecraft.net/en-us/eula) on behalf of every world
        # this runs, not something to leave silently implicit.
        (world_dir / "eula.txt").write_text("eula=true\n")

        if assignment.velocity_forwarding_secret:
            _write_paper_global_config(world_dir, assignment.velocity_forwarding_secret)

        marker.write_text("ok")
    finally:
        if owns_client:
            client.close()

    return jar_path
