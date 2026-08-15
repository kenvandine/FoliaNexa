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
    per-region tick threads)."""
    min_heap = max(1, memory_gb // 2)
    return [
        java_bin,
        f"-Xms{min_heap}G",
        f"-Xmx{memory_gb}G",
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
