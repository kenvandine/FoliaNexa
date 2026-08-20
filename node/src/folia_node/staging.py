"""Downloads and stages the jar + plugins + datapacks + server.properties
for this world. PLAN.md §9 step 2.

Two separate concerns, on purpose:

- `ensure_staged`: first-boot-only setup (EULA acceptance, paper-
  global.yml) — idempotent via the `.staged` marker, so a node-agent
  restart never redoes it. Also does the world's very first jar
  download.
- `sync_world_config`: reconciliation against mgmt's current manifests/
  cluster config — runs on *every* agent start, including the first
  one, so a PATCH /worlds/{name} edit or a cluster-wide Minecraft
  version change (routers/cluster.py's migrate action) actually takes
  effect the next time the world's container restarts, not just at
  initial creation. Safe to re-run any number of times; each call
  reflects current server-side state, nothing more. This *does* include
  re-downloading server.jar itself when the cluster's target engine/
  version has moved on (see JAR_VERSION_MARKER) — deliberately not
  first-boot-only, unlike everything else ensure_staged handles.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from folia_node.devlxd import WorldAssignment

logger = logging.getLogger(__name__)

JAR_FILENAME = "server.jar"
STAGED_MARKER = ".staged"
# Records which engine:version is currently staged as server.jar, so
# sync_world_config can tell a real version change (mgmt's cluster-wide
# MinecraftVersionConfig, PLAN.md §9 — see routers/cluster.py's migrate
# action) apart from "nothing to do." Confirmed the hard way: before this
# existed, a world's jar was only ever fetched once, at first boot ever —
# a client on a newer Minecraft version than whatever a world happened to
# first boot with simply couldn't connect, with no way to fix it short of
# deleting the world and recreating it from scratch.
JAR_VERSION_MARKER = ".jar-version"

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
# _validate_properties rejects these keys outright, this is just defense
# in depth against that invariant ever slipping).
#
# online-mode=false: without this, a world behind folia-nexa-proxy's
# Velocity refuses the connection outright with "IllegalStateException:
# Backend server is online-mode!" — Paper won't accept a proxied
# connection from a server it hasn't been told to trust. This plus the
# matching proxies.velocity block in paper-global.yml below is what
# Paper's own docs call "modern" forwarding:
# https://docs.papermc.io/velocity/player-information-forwarding/
#
# enable-rcon/rcon.port/rcon.password: only turned on when mgmt actually
# handed this world an RCON password (assignment.rcon_password) — a world
# placed before RCON existed just doesn't get these keys at all, rather
# than turning on RCON with no real secret. rcon.port is fixed at
# Minecraft's conventional 25575, matching routers/worlds.py's RCON_PORT.
def _required_properties(assignment: WorldAssignment) -> dict[str, str]:
    required = {"online-mode": "false"}
    if assignment.rcon_password:
        required.update({"enable-rcon": "true", "rcon.port": "25575", "rcon.password": assignment.rcon_password})
    return required


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


def _write_server_properties(world_dir: Path, overrides: dict[str, str], assignment: WorldAssignment) -> None:
    # Merge, never replace outright — Paper writes dozens of its own keys
    # into this file beyond first boot (difficulty, motd, view-distance,
    # ...) that this codebase never templates and has no business
    # clobbering just because an operator changed one unrelated property.
    path = world_dir / "server.properties"
    merged = {**_read_server_properties(path), **overrides, **_required_properties(assignment)}
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


_MANIFEST_STATE_FILENAME = ".manifest-urls.json"


def _reconcile_manifest_dir(client: httpx.Client, manifest_url: str, target_dir: Path, suffix: str) -> None:
    """Makes target_dir's `{name}{suffix}` files match the manifest exactly
    — downloads anything missing or whose URL has changed since it was
    last staged (a catalog entry's download_url bumped to a new version —
    e.g. the dashboard's plugin-catalog editor — via .manifest-urls.json,
    a name->url map of what's currently on disk), deletes anything no
    longer listed. Existing files whose URL hasn't changed are left
    alone, no redundant re-download just because a restart happened.

    Deletion is scoped to exactly this dir's top level, matching only
    `*{suffix}` files — a plugin/data pack's own subdirectory (config
    files, etc.) is never touched. An operator-placed file that happens to
    share a name with a since-removed catalog entry is indistinguishable
    from a stale managed one, though — this reconciliation assumes
    target_dir's `{suffix}` files are entirely mgmt-managed, matching
    _validate_plugins/_validate_datapacks' "no free-typed names" model.

    A world staged before this URL-tracking existed has no state file yet
    — treated the same as "every entry's URL changed," matching
    JAR_VERSION_MARKER's own tradeoff: one redundant re-download the
    first time this runs on an old world, harmless, self-healing from
    then on.

    One entry's failure (a bad/unreachable download_url, or — for
    deletion — a file this process somehow can't remove) never aborts
    the rest of the pass: confirmed the hard way on a live world where a
    single failing plugin download raised uncaught, aborting
    sync_world_config before it ever reached the datapacks/server-
    properties reconcile steps that follow it — so one broken plugin URL
    silently froze *everything* else (new plugins, removed data packs,
    server.properties edits) on every single restart, indefinitely, with
    no operator-visible error beyond a node-agent crash loop. Every
    delete and every download is now caught and logged individually;
    good entries land, a bad one is retried next reconcile pass instead
    of blocking its neighbors.
    """
    resp = client.get(manifest_url)
    resp.raise_for_status()
    manifest = resp.json()  # [{"name": "...", "url": "..."}, ...]
    desired = {entry["name"]: entry["url"] for entry in manifest}

    target_dir.mkdir(parents=True, exist_ok=True)
    state_path = target_dir / _MANIFEST_STATE_FILENAME
    try:
        staged_urls = json.loads(state_path.read_text()) if state_path.exists() else {}
    except json.JSONDecodeError:
        staged_urls = {}

    existing = {p.stem for p in target_dir.glob(f"*{suffix}")}
    for name in existing - desired.keys():
        stale = target_dir / f"{name}{suffix}"
        try:
            logger.info("removing '%s' (no longer in manifest)", stale.name)
            stale.unlink(missing_ok=True)
        except OSError:
            logger.exception("failed to remove '%s' — leaving it, will retry next reconcile", stale.name)

    for name, url in desired.items():
        dest = target_dir / f"{name}{suffix}"
        if dest.exists() and staged_urls.get(name) == url:
            continue
        try:
            logger.info("staging '%s' from %s", dest.name, url)
            _download(client, url, dest)
        except (httpx.HTTPError, OSError):
            logger.exception("failed to stage '%s' from %s — skipping, will retry next reconcile", dest.name, url)

    state_path.write_text(json.dumps(desired))


def _jar_identity(assignment: WorldAssignment) -> str:
    return f"{assignment.jar_engine}:{assignment.jar_version}"


def _reconcile_jar(client: httpx.Client, world_dir: Path, assignment: WorldAssignment) -> None:
    """Re-downloads server.jar if the cluster's engine/version has moved
    on since this world last staged one — see JAR_VERSION_MARKER's own
    comment. A brand new world's first-ever download already happens in
    ensure_staged, which also writes this marker, so this is a no-op
    immediately afterward in the same agent startup; an *existing* world
    that predates this marker existing at all gets treated as stale
    (redownloads once, harmlessly, even if the version happens to already
    match) rather than trying to guess — simpler and self-healing after
    exactly one redundant download.
    """
    marker = world_dir / JAR_VERSION_MARKER
    desired = _jar_identity(assignment)
    current = marker.read_text().strip() if marker.exists() else None
    if current == desired:
        return
    logger.info("world '%s' engine/version is now %s (was %s), re-staging jar", assignment.world_name, desired, current)
    _download(client, assignment.jar_url, world_dir / JAR_FILENAME)
    marker.write_text(desired)


def sync_world_config(world_dir: Path, assignment: WorldAssignment, client: httpx.Client | None = None) -> None:
    """Reconciles the server jar, plugins/, <level-name>/datapacks/, and
    server.properties against mgmt's current manifests/cluster config.
    Called on every agent start (see agent.py) — never gated by
    STAGED_MARKER, unlike ensure_staged's first-boot-only setup."""
    owns_client = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        _reconcile_jar(client, world_dir, assignment)

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
                    _write_server_properties(world_dir, {}, assignment)
            else:
                _write_server_properties(world_dir, overrides, assignment)
        elif not (world_dir / "server.properties").exists():
            _write_server_properties(world_dir, {}, assignment)
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

        (world_dir / JAR_VERSION_MARKER).write_text(_jar_identity(assignment))
        marker.write_text("ok")
    finally:
        if owns_client:
            client.close()

    return jar_path
