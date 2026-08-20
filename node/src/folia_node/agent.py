"""folia-nexa-node entry point. PLAN.md §9.

Orchestration only — reads the world assignment, stages it, runs the JVM,
serves health/metrics, and restarts on crash. snapd's own
restart-condition (snapcraft.yaml) is the outer safety net if this process
itself dies; the retry loop here handles the JVM dying under a live agent.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path

from folia_node.devlxd import DevLXDClient, DevLXDError
from folia_node.health import AgentState, BroadcastLogHandler, start_health_server
from folia_node.runner import JVMRunner, build_java_command
from folia_node.staging import ensure_staged, sync_world_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

CRASH_RESTART_DELAY_SECONDS = 5


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
    logging.getLogger().addHandler(BroadcastLogHandler(state.agent_log))

    devlxd = DevLXDClient(devlxd_socket)
    try:
        assignment = devlxd.get_world_assignment()
    except DevLXDError:
        logger.exception("could not read world assignment from devlxd — nothing to run")
        raise

    state.world_name = assignment.world_name
    start_health_server(state, port=health_port)
    logger.info("health server up on :%d for world '%s'", health_port, assignment.world_name)

    jar_path = ensure_staged(world_dir, assignment)
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
        runner = JVMRunner(command, cwd=world_dir, on_line=state.console_log.append)
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
