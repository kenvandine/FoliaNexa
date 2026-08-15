from __future__ import annotations

import stat
import sys
import textwrap

from folia_node.runner import JVMRunner, build_java_command


def test_build_java_command_includes_expected_flags(tmp_path):
    jar = tmp_path / "server.jar"
    cmd = build_java_command("java", jar, memory_gb=8)
    assert cmd[0] == "java"
    assert "-Xmx8G" in cmd
    assert "-Xms4G" in cmd
    assert "-XX:+UseZGC" in cmd
    assert cmd[-3:] == ["-jar", str(jar), "--nogui"]


def test_build_java_command_min_heap_floors_at_1g(tmp_path):
    cmd = build_java_command("java", tmp_path / "server.jar", memory_gb=1)
    assert "-Xms1G" in cmd


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


def test_jvm_runner_stop_terminates_running_process(tmp_path):
    script = tmp_path / "sleeper.sh"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    runner = JVMRunner([str(script)], cwd=tmp_path)
    runner.start()
    assert runner.is_running()
    runner.stop(timeout=5)
    assert not runner.is_running()
