"""Builds and supervises the world's JVM process."""

from __future__ import annotations

import collections
import subprocess
import threading
from pathlib import Path

LOG_TAIL_LINES = 200


def build_java_command(java_bin: str, jar_path: Path, memory_gb: int) -> list[str]:
    """Region-scheduler-friendly flags, matching PLAN.md's original
    run-folia.sh baseline (generational ZGC tends to do well with Folia's
    per-region tick threads).

    Heap is sized at 75% of the container's own memory limit, not the
    full amount, and -Xms == -Xmx always (-XX:+AlwaysPreTouch pretouches
    every heap page at startup, so a heap that could still grow at
    runtime would defeat the point). Confirmed the hard way: a world
    declared with memory_gb=1 handing the JVM the *entire* 1GB as
    -Xmx1G -Xms1G left zero room for metaspace, thread stacks, the JIT
    code cache, GC bookkeeping (ZGC keeps real structures outside the
    heap too), and off-heap buffers — the JVM failed before Minecraft's
    own code ever ran ("Failed to allocate initial Java heap", "Failed
    to commit memory (Not enough space)"). Not a small-container-only
    problem, just the size where zero headroom is guaranteed to be fatal
    every time rather than merely risky.
    """
    heap_mb = max(256, int(memory_gb * 1024 * 0.75))
    return [
        java_bin,
        f"-Xms{heap_mb}m",
        f"-Xmx{heap_mb}m",
        "-XX:+UseZGC",
        "-XX:+ZGenerational",
        "-XX:+AlwaysPreTouch",
        "-Dterminal.jline=false",
        "-Dterminal.ansi=true",
        "-jar",
        str(jar_path),
        "--nogui",
    ]


class JVMRunner:
    """Wraps a running (or not-yet-started) JVM process, keeping a rolling
    tail of its output for crash diagnostics (PLAN.md §9 step 5)."""

    def __init__(self, command: list[str], cwd: Path):
        self._command = command
        self._cwd = cwd
        self._process: subprocess.Popen | None = None
        self._log_tail: collections.deque[str] = collections.deque(maxlen=LOG_TAIL_LINES)
        self._reader_thread: threading.Thread | None = None

    def start(self) -> None:
        self._cwd.mkdir(parents=True, exist_ok=True)
        self._process = subprocess.Popen(
            self._command,
            cwd=self._cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._reader_thread = threading.Thread(target=self._drain_output, daemon=True)
        self._reader_thread.start()

    def _drain_output(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            self._log_tail.append(line.rstrip("\n"))

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def wait(self) -> int:
        assert self._process is not None
        code = self._process.wait()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=5)
        return code

    def stop(self, timeout: float = 30.0) -> None:
        if self._process is None or self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._process.kill()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def log_tail(self) -> list[str]:
        return list(self._log_tail)
