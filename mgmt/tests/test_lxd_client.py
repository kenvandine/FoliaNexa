"""Regression coverage for LXDClient.snapshot_container's per-container
in-flight lock — see LONG_OPERATION_TIMEOUT's comment in lxd_client.py
for the live incident this closes: an operator's manual world backup on
a "dir"-backend LXD host timed out client-side while genuinely still
running server-side, the retry piled a second concurrent snapshot
request onto the same container, and LXD wedged it in FREEZING. Every
other LXDClient method is still untested here (see CLAUDE.md) — this
file only covers the one thing that's now been confirmed live.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from folia_mgmt.lxd_client import LXDClient, LXDError
from folia_mgmt.models import Host


def _host() -> Host:
    return Host(
        name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16,
        server_cert_pem="dummy",
    )


def test_snapshot_container_rejects_a_second_concurrent_call_for_the_same_container(monkeypatch):
    client = LXDClient(Path("/dev/null"), Path("/dev/null"))
    host = _host()

    first_call_started = threading.Event()
    release_first_call = threading.Event()

    class _FakeHttpClient:
        def post(self, path, **kwargs):
            # Only the world-a snapshot blocks (simulating a slow, still-
            # in-flight LXD operation) — the world-b call used below to
            # confirm other containers are unaffected must return
            # immediately rather than piggybacking on the same gate.
            if "/instances/world-a/" in path:
                first_call_started.set()
                assert release_first_call.wait(timeout=5), "test deadlocked waiting to be released"
            resp = MagicMock()
            resp.status_code = 200
            return resp

    @contextmanager
    def _fake_client_for(self, host):
        yield _FakeHttpClient()

    monkeypatch.setattr(LXDClient, "_client_for", _fake_client_for)
    monkeypatch.setattr(LXDClient, "_finish", lambda self, client, resp, *, ok_codes, error: None)

    first_call_error: list[Exception] = []

    def _run_first_call():
        try:
            client.snapshot_container(host, "world-a", "snap-1")
        except Exception as exc:  # pragma: no cover - surfaced via first_call_error
            first_call_error.append(exc)

    thread = threading.Thread(target=_run_first_call)
    thread.start()
    assert first_call_started.wait(timeout=5), "first call never reached the fake HTTP client"

    # A second call for the *same* container, while the first is still in
    # flight, must be rejected immediately — without ever reaching the
    # network — rather than being allowed to pile a second real snapshot
    # request onto LXD for the same instance.
    with pytest.raises(LXDError, match="already in progress"):
        client.snapshot_container(host, "world-a", "snap-2")

    # A concurrent snapshot of a *different* container is unaffected.
    client.snapshot_container(host, "world-b", "snap-1")

    release_first_call.set()
    thread.join(timeout=5)
    assert not first_call_error, f"first call raised unexpectedly: {first_call_error}"

    # The lock is released once the first call finishes — a follow-up
    # call for the same container now succeeds.
    client.snapshot_container(host, "world-a", "snap-3")
