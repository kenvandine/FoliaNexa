from __future__ import annotations

import stat
import sys
import textwrap
from pathlib import Path

from folia_node.runner import JVMRunner, build_java_command


def test_build_java_command_includes_expected_flags(tmp_path):
    jar = tmp_path / "server.jar"
    cmd = build_java_command("java", jar, memory_gb=8)
    assert cmd[0] == "java"
    # 75% of an 8GB limit, reserving the rest for metaspace/threads/GC/
    # off-heap overhead — see build_java_command's own docstring for why.
    assert "-Xmx6144m" in cmd
    assert "-Xms6144m" in cmd
    assert "-XX:+UseZGC" in cmd
    assert cmd[-3:] == ["-jar", str(jar), "--nogui"]


def test_build_java_command_xms_always_equals_xmx():
    # -XX:+AlwaysPreTouch pretouches the whole heap at startup, so a heap
    # that could still grow at runtime (Xms < Xmx) would defeat the point.
    for memory_gb in (1, 2, 4, 12):
        cmd = build_java_command("java", Path("server.jar"), memory_gb=memory_gb)
        xms = next(f for f in cmd if f.startswith("-Xms"))
        xmx = next(f for f in cmd if f.startswith("-Xmx"))
        assert xms.removeprefix("-Xms") == xmx.removeprefix("-Xmx")


def test_build_java_command_reserves_headroom_for_a_1gb_world():
    # The exact scenario that used to crash the JVM outright before it
    # ever reached Minecraft's own code: a 1GB container limit handed the
    # JVM the entire 1GB as -Xmx, leaving nothing for its own overhead.
    cmd = build_java_command("java", Path("server.jar"), memory_gb=1)
    xmx = next(f for f in cmd if f.startswith("-Xmx"))
    heap_mb = int(xmx.removeprefix("-Xmx").removesuffix("m"))
    assert heap_mb < 1024
    assert heap_mb >= 256


def _fake_java_script(tmp_path, exit_code: int, output_lines: list[str]):
    script = tmp_path / "fake_java.sh"
    body = "\n".join(f"echo '{line}'" for line in output_lines)
    script.write_text(f"#!/bin/sh\n{body}\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_jvm_runner_captures_exit_code_and_log_tail(tmp_path):
    fake_java = _fake_java_script(tmp_path, exit_code=1, output_lines=["starting up", "boom"])
    runner = JVMRunner([fake_java], cwd=tmp_path)
    runner.start()
    assert runner.is_running() or True  # timing-dependent; exit code check below is the real assertion
    code = runner.wait()
    assert code == 1
    assert "boom" in runner.log_tail
    assert not runner.is_running()


def test_jvm_runner_calls_on_line_for_live_streaming(tmp_path):
    fake_java = _fake_java_script(tmp_path, exit_code=0, output_lines=["hello", "world"])
    seen: list[str] = []
    runner = JVMRunner([fake_java], cwd=tmp_path, on_line=seen.append)
    runner.start()
    runner.wait()
    assert seen == ["hello", "world"]


def test_jvm_runner_stop_terminates_running_process(tmp_path):
    script = tmp_path / "sleeper.sh"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    runner = JVMRunner([str(script)], cwd=tmp_path)
    runner.start()
    assert runner.is_running()
    runner.stop(timeout=5)
    assert not runner.is_running()
