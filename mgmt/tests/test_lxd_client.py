"""Regression coverage for the two live incidents LXDClient's snapshot/
restore methods have caused on "dir"-backend LXD hosts: the per-container
in-flight lock (see LONG_OPERATION_TIMEOUT's comment in lxd_client.py —
an operator's manual world backup timed out client-side while genuinely
still running server-side, the retry piled a second concurrent snapshot
request onto the same container, and LXD wedged it in FREEZING), and the
unconditional "dir" refusal added after the redesign to file-level world
backups (get_storage_driver_for_instance / _refuse_dir_backend) — since
the tracked "time machine" backup feature no longer depends on LXD
snapshots at all, this file's remaining reason to exist is exercising
the lower-level ad-hoc snapshot/restore endpoints' safety net. Every
other LXDClient method is still untested here (see CLAUDE.md).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from folia_mgmt.lxd_client import LXDClient, LXDError, RestoreInProgressError
from folia_mgmt.models import Host


def _host() -> Host:
    return Host(
        name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16,
        server_cert_pem="dummy",
    )


class _FakeHttpClient:
    """Fakes the two GETs get_storage_driver_for_instance makes (instance
    -> its root pool, then that pool -> its driver) so every test in this
    file can control which driver a container appears to sit on, plus a
    configurable POST for snapshot_container's own call. Defaults to
    "zfs" (i.e. not "dir") so tests that aren't specifically about the
    dir-refusal don't need to think about it at all."""

    def __init__(self, driver: str = "zfs", on_post=None):
        self.driver = driver
        self._on_post = on_post

    def get(self, path, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "/storage-pools/" in path:
            resp.json.return_value = {"metadata": {"driver": self.driver}}
        else:
            resp.json.return_value = {"metadata": {"expanded_devices": {"root": {"pool": "default"}}}}
        return resp

    def post(self, path, **kwargs):
        if self._on_post is not None:
            self._on_post(path, **kwargs)
        resp = MagicMock()
        resp.status_code = 200
        return resp


def test_get_storage_driver_for_instance_reads_the_instances_root_pool(monkeypatch):
    client = LXDClient(Path("/dev/null"), Path("/dev/null"))
    host = _host()

    @contextmanager
    def _fake_client_for(self, host):
        yield _FakeHttpClient(driver="btrfs")

    monkeypatch.setattr(LXDClient, "_client_for", _fake_client_for)

    assert client.get_storage_driver_for_instance(host, "world-a") == "btrfs"


@pytest.mark.parametrize("method_name", ["snapshot_container", "restore_snapshot"])
def test_refuses_dir_backed_instance(monkeypatch, method_name):
    client = LXDClient(Path("/dev/null"), Path("/dev/null"))
    host = _host()

    @contextmanager
    def _fake_client_for(self, host):
        yield _FakeHttpClient(driver="dir")

    monkeypatch.setattr(LXDClient, "_client_for", _fake_client_for)

    method = getattr(client, method_name)
    with pytest.raises(LXDError, match="'dir' driver"):
        method(host, "world-a", "some-snapshot")


@pytest.mark.parametrize("method_name", ["snapshot_container", "restore_snapshot"])
def test_refuses_an_unrecognized_driver_not_just_dir(monkeypatch, method_name):
    # _refuse_dir_backend allowlists drivers actually confirmed safe
    # (zfs, btrfs) rather than blocklisting only "dir" — an lvm pool
    # without thin provisioning (or any other driver never diagnosed
    # against the freeze-the-whole-container incident this check exists
    # to prevent) must be refused the same way "dir" is, not silently
    # let through just because it isn't literally "dir".
    client = LXDClient(Path("/dev/null"), Path("/dev/null"))
    host = _host()

    @contextmanager
    def _fake_client_for(self, host):
        yield _FakeHttpClient(driver="lvm")

    monkeypatch.setattr(LXDClient, "_client_for", _fake_client_for)

    method = getattr(client, method_name)
    with pytest.raises(LXDError, match="'lvm' driver"):
        method(host, "world-a", "some-snapshot")


def test_snapshot_container_rejects_a_second_concurrent_call_for_the_same_container(monkeypatch):
    client = LXDClient(Path("/dev/null"), Path("/dev/null"))
    host = _host()

    first_call_started = threading.Event()
    release_first_call = threading.Event()

    class _LockTestHttpClient(_FakeHttpClient):
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
        yield _LockTestHttpClient()

    monkeypatch.setattr(LXDClient, "_client_for", _fake_client_for)
    monkeypatch.setattr(LXDClient, "_finish", lambda self, client, resp, *, ok_codes, error, timeout=None: None)

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


def test_restore_guard_rejects_a_second_concurrent_restore_for_the_same_container():
    # Mirrors snapshot_container's own per-container lock — a double-
    # clicked dashboard Restore button, or two admins restoring the same
    # world at once, must not be allowed to race two push_file writes to
    # the same pending-restore marker and two restart_container calls
    # against the same container.
    client = LXDClient(Path("/dev/null"), Path("/dev/null"))
    host = _host()

    entered_first = threading.Event()
    release_first = threading.Event()
    first_call_error: list[Exception] = []

    def _run_first_guard():
        try:
            with client.restore_guard(host, "world-a"):
                entered_first.set()
                assert release_first.wait(timeout=5), "test deadlocked waiting to be released"
        except Exception as exc:  # pragma: no cover - surfaced via first_call_error
            first_call_error.append(exc)

    thread = threading.Thread(target=_run_first_guard)
    thread.start()
    assert entered_first.wait(timeout=5), "first guard never entered"

    with pytest.raises(RestoreInProgressError, match="already in progress"):
        with client.restore_guard(host, "world-a"):
            pass  # pragma: no cover - must be rejected before entering

    # A concurrent restore of a *different* container is unaffected.
    with client.restore_guard(host, "world-b"):
        pass

    release_first.set()
    thread.join(timeout=5)
    assert not first_call_error, f"first guard raised unexpectedly: {first_call_error}"

    # The lock is released once the first guard exits — a follow-up
    # restore of the same container now succeeds.
    with client.restore_guard(host, "world-a"):
        pass
