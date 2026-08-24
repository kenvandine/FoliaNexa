"""folia-nexa-node entry point. PLAN.md §9.

Orchestration only — reads the world assignment, stages it, runs the JVM,
serves health/metrics, and restarts on crash. snapd's own
restart-condition (snapcraft.yaml) is the outer safety net if this process
itself dies; the retry loop here handles the JVM dying under a live agent.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import tarfile
import tempfile
import time
from pathlib import Path

from folia_node.devlxd import DevLXDClient, DevLXDError
from folia_node.health import AgentState, BroadcastLogHandler, start_health_server
from folia_node.runner import JVMRunner, build_java_command
from folia_node.staging import LEVEL_NAME, ensure_staged, sync_world_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

CRASH_RESTART_DELAY_SECONDS = 5

# Where mgmt pushes a backup tarball ahead of a restore (POST
# /worlds/{name}/backups/{id}/restore, mgmt's routers/worlds.py) — pushed
# via LXD's file API while this container is still running, then the
# whole container is restarted (LXDClient.restart_container), which is
# what gets us to _apply_pending_restore below on a fresh process before
# the JVM has even started.
PENDING_RESTORE_MARKER = ".pending-restore.tar.gz"


def _apply_pending_restore(world_dir: Path, state: AgentState) -> bool:
    """If a restore was requested, extract the tarball over WORLD_DIR now
    — replacing the old world save + plugins/ entirely — before any of
    the staging below runs (already idempotent/reconciling, so it layers
    fine on top of what a restore just put there). A missing marker is
    the normal case (nothing to do); a present-but-corrupt one is logged
    and skipped rather than crash-looping the whole agent forever on a
    bad restore — the world just comes back up with whatever was already
    on disk, same as if the restore had never been requested.
    filter="data" (Python 3.12+) rejects absolute paths/traversal entries
    in the tarball, same defense-in-depth this codebase already applies
    to other operator/mgmt-supplied paths (see mgmt's plugin_upload.py/
    plugin_files.py).

    Extracts into a scratch directory first and only deletes/replaces the
    real world/plugins dirs once the *entire* archive has been read
    successfully — tarfile.open() only validates the gzip header/first
    block, so a member later in the stream can still be truncated/corrupt
    (a disk error on mgmt between backup and restore, or a corrupted
    push_file transfer) and only surface once extractall reaches it. Doing
    the delete before that point (the original approach) could leave a
    world with neither the old save nor a complete new one; this way a
    failure here always leaves world_dir untouched, matching the
    "world starts as-is" guarantee this function documents above.

    Returns whether a restore was actually applied this boot — main()
    uses this to skip that same boot's sync_world_config call (see its
    own call site), since re-syncing against mgmt's *current* plugin/
    datapack manifest right after a restore would immediately
    re-download anything whose catalog URL has moved on since the
    backup was taken, silently undoing the very thing a restore is
    supposed to guarantee ("brings back the exact plugin versions
    running at backup time", see this module's PENDING_RESTORE_MARKER
    comment).

    Also records the outcome on `state` (last_restore_at/
    last_restore_error) so mgmt's finalize_provisioning
    (mgmt/src/folia_mgmt/scheduler.py) can poll GET /metrics and surface
    a corrupt/failed restore back to the operator — restore_backup
    (mgmt's routers/worlds.py) only ever confirms the tarball was pushed
    and the container told to restart; without this, the actual
    extraction outcome here was previously visible nowhere but this
    process's own local log."""
    marker = world_dir / PENDING_RESTORE_MARKER
    if not marker.exists():
        return False
    logger.info("pending restore found (%s) — extracting over %s before staging", marker, world_dir)
    try:
        with tempfile.TemporaryDirectory(dir=world_dir) as scratch:
            scratch_dir = Path(scratch)
            with tarfile.open(marker, mode="r:gz") as tf:
                tf.extractall(path=scratch_dir, filter="data")
            for name in (LEVEL_NAME, "plugins"):
                target = world_dir / name
                shutil.rmtree(target, ignore_errors=True)
                if target.exists():
                    # ignore_errors=True above can still leave a
                    # directory partially in place (e.g. one file this
                    # process can't delete) — shutil.move into an
                    # *existing* destination directory nests the
                    # extracted tree inside it instead of replacing it
                    # (plugins/plugins/*.jar) rather than raising
                    # anything that would surface the problem. Raise
                    # here instead, so this is treated the same as a
                    # corrupt archive by the except clause below.
                    raise OSError(f"could not fully remove stale directory {target} before restore")
                extracted = scratch_dir / name
                if extracted.exists():
                    shutil.move(str(extracted), str(target))
        logger.info("restore applied successfully")
        state.last_restore_at = time.time()
        state.last_restore_error = None
        return True
    except (tarfile.TarError, OSError, EOFError) as exc:
        logger.exception("pending restore at %s is corrupt/unreadable — skipping, world starts as-is", marker)
        state.last_restore_at = time.time()
        state.last_restore_error = str(exc)
        return False
    finally:
        marker.unlink(missing_ok=True)


def main() -> None:
    world_dir = Path(os.environ.get("WORLD_DIR", "/tmp/folia-world"))
    java_bin = os.environ.get("JAVA_BIN", "java")
    health_port = int(os.environ.get("FOLIA_NODE_HEALTH_PORT", "8123"))
    devlxd_socket = os.environ.get("DEVLXD_SOCKET", "/dev/lxd/sock")

    # AgentState (and its agent_log broadcaster) is created — and the
    # broadcast handler attached to the root logger — before the very
    # first devlxd call, not after, so a failure right here (like the
    # jar-download 404s and the devlxd/SSL errors this was built to
    # surface) is already captured in agent_log's history. Nothing can
    # *serve* it live yet (start_health_server hasn't run), but the
    # history backfill still has it once a viewer connects.
    state = AgentState()
    state.world_dir = world_dir
    logging.getLogger().addHandler(BroadcastLogHandler(state.agent_log))

    devlxd = DevLXDClient(devlxd_socket)
    try:
        assignment = devlxd.get_world_assignment()
    except DevLXDError:
        logger.exception("could not read world assignment from devlxd — nothing to run")
        raise

    state.world_name = assignment.world_name
    state.backup_shared_secret = assignment.node_agent_shared_secret
    start_health_server(state, port=health_port)
    logger.info("health server up on :%d for world '%s'", health_port, assignment.world_name)

    restored = _apply_pending_restore(world_dir, state)

    jar_path = ensure_staged(world_dir, assignment)
    if restored:
        # Deliberately skipped this one boot — see
        # _apply_pending_restore's own docstring for why: reconciling
        # against mgmt's *current* plugin/datapack manifest right after
        # a restore would immediately re-download anything whose catalog
        # URL has moved on since the backup was taken, undoing the
        # restore for that plugin on the very boot that was supposed to
        # bring it back. The next restart (crash-loop or operator-
        # triggered) syncs normally.
        logger.info("skipping this boot's plugin/datapack/server-properties sync — a restore was just applied")
    else:
        # Unlike ensure_staged, always runs — picks up any plugins/datapacks/
        # server.properties edit made via PATCH /worlds/{name} since this
        # world's container last started (PLAN.md §9).
        sync_world_config(world_dir, assignment)

    # A world's container is restarted/stopped via a graceful LXD action
    # (force=false — see LXDClient.restart_container's own comment) that
    # sends a termination signal and waits before giving up and killing
    # everything outright. Without this handler, this process (this
    # snap's PID 1 inside the container) had no reaction to that signal
    # beyond Python's default "just die" — the JVM child was orphaned,
    # never itself signaled, and only actually stopped once LXD's own
    # timeout expired and force-killed the whole cgroup, giving Paper/
    # Folia's shutdown hook (the thing that actually saves the world —
    # not the `save-all` command, which Folia disables outright, per
    # PaperMC's own docs) no real chance to run. request_stop() just
    # forwards SIGTERM to the JVM non-blockingly; the main loop's
    # runner.wait() below (already in progress) picks up the exit once
    # Paper's own shutdown sequence finishes and the process actually
    # exits — no separate wait here, which would otherwise be a second,
    # concurrent wait() on the same subprocess.
    shutdown_requested = False
    current_runner: JVMRunner | None = None

    def _handle_shutdown_signal(signum, _frame):
        nonlocal shutdown_requested
        logger.info("received signal %s — requesting graceful JVM shutdown", signal.Signals(signum).name)
        shutdown_requested = True
        if current_runner is not None:
            current_runner.request_stop()

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    while True:
        command = build_java_command(java_bin, jar_path, memory_gb=_detect_memory_gb())
        logger.info("starting JVM: %s", " ".join(command))
        runner = JVMRunner(
            command,
            cwd=world_dir,
            on_line=state.console_log.append,
            env={**os.environ, "FOLIA_WORLD_NAME": assignment.world_name},
        )
        current_runner = runner
        runner.start()

        with state.lock:
            state.phase = "running"
            state.pid = runner.pid
            state.started_at = time.time()

        exit_code = runner.wait()

        if shutdown_requested:
            logger.info("world '%s' shut down cleanly on signal, exiting agent", assignment.world_name)
            return

        with state.lock:
            state.phase = "crashed"
            state.last_exit_code = exit_code
            state.log_tail = runner.log_tail

        logger.warning(
            "JVM for '%s' exited with code %s, restarting in %ds. Last log lines:\n%s",
            assignment.world_name,
            exit_code,
            CRASH_RESTART_DELAY_SECONDS,
            "\n".join(runner.log_tail[-20:]),
        )
        time.sleep(CRASH_RESTART_DELAY_SECONDS)


def _detect_memory_gb() -> int:
    """Falls back to a conservative default if the cgroup limit can't be
    read — the LXD instance's own `limits.memory` (set by mgmt at launch,
    PLAN.md §5) is the real ceiling regardless of what we pass to -Xmx."""
    cgroup_path = Path("/sys/fs/cgroup/memory.max")
    try:
        raw = cgroup_path.read_text().strip()
        if raw != "max":
            return max(1, int(raw) // (1024**3))
    except (OSError, ValueError):
        pass
    return 2


if __name__ == "__main__":
    main()
