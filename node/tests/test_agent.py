from __future__ import annotations

import tarfile
from io import BytesIO

from folia_node import agent as agent_module
from folia_node.agent import PENDING_RESTORE_MARKER, _apply_pending_restore
from folia_node.health import AgentState


def _write_tar_gz(path, entries: dict[str, bytes]) -> None:
    with tarfile.open(path, mode="w:gz") as tf:
        for name, content in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, BytesIO(content))


def test_apply_pending_restore_extracts_and_replaces_existing_dirs(tmp_path):
    # Old content that must be gone after restore — a stale region file
    # not present in the backup would otherwise linger forever.
    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "stale-region.mca").write_bytes(b"old data")
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "OldPlugin.jar").write_bytes(b"old jar")

    marker = tmp_path / PENDING_RESTORE_MARKER
    _write_tar_gz(marker, {
        "world/level.dat": b"restored level data",
        "plugins/RestoredPlugin.jar": b"restored jar bytes",
    })

    state = AgentState()
    assert _apply_pending_restore(tmp_path, state) is True

    assert (tmp_path / "world" / "level.dat").read_bytes() == b"restored level data"
    assert not (tmp_path / "world" / "stale-region.mca").exists()
    assert (tmp_path / "plugins" / "RestoredPlugin.jar").read_bytes() == b"restored jar bytes"
    assert not (tmp_path / "plugins" / "OldPlugin.jar").exists()
    assert not marker.exists()
    # Recorded for mgmt's finalize_provisioning to pick up via GET
    # /metrics (see scheduler.py's _record_restore_outcome).
    assert state.last_restore_at is not None
    assert state.last_restore_error is None


def test_apply_pending_restore_is_a_noop_when_no_marker_present(tmp_path):
    state = AgentState()
    assert _apply_pending_restore(tmp_path, state) is False  # must not raise
    assert not (tmp_path / PENDING_RESTORE_MARKER).exists()
    # Nothing to report — main() uses False to decide whether to run
    # this boot's sync_world_config, and finalize_provisioning uses a
    # None last_restore_at to know no restore happened at all.
    assert state.last_restore_at is None
    assert state.last_restore_error is None


def test_apply_pending_restore_skips_a_corrupt_marker_without_crashing(tmp_path):
    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "level.dat").write_bytes(b"pre-existing data")

    marker = tmp_path / PENDING_RESTORE_MARKER
    marker.write_bytes(b"not actually a gzip tarball")

    state = AgentState()
    assert _apply_pending_restore(tmp_path, state) is False  # must not raise, and must not crash-loop the agent

    # Corrupt restore skipped — the world is left exactly as it was,
    # not partially wiped.
    assert (tmp_path / "world" / "level.dat").read_bytes() == b"pre-existing data"
    assert not marker.exists()
    assert state.last_restore_at is not None
    assert state.last_restore_error


def test_apply_pending_restore_rejects_path_traversal_entries(tmp_path):
    marker = tmp_path / PENDING_RESTORE_MARKER
    _write_tar_gz(marker, {"../../etc/passwd": b"malicious"})

    state = AgentState()
    _apply_pending_restore(tmp_path, state)  # must not raise, must not escape tmp_path

    assert not (tmp_path.parent.parent / "etc" / "passwd").exists()
    assert not marker.exists()


def test_apply_pending_restore_leaves_existing_data_untouched_when_a_later_member_is_truncated(tmp_path):
    # tarfile.open() only reads the first member's header lazily — it
    # doesn't fail just because a member later in the stream is
    # truncated/corrupt. That failure only surfaces once extractall
    # actually reaches it, by which point a first member ("world/level.dat"
    # below) may have already been extracted. A restore must not let that
    # partial success land on the real world/plugins dirs — see
    # _apply_pending_restore's own docstring.
    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "level.dat").write_bytes(b"pre-existing level data")
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "OldPlugin.jar").write_bytes(b"pre-existing jar")

    marker = tmp_path / PENDING_RESTORE_MARKER
    _write_tar_gz(marker, {
        "world/level.dat": b"restored level data",
        "plugins/RestoredPlugin.jar": b"restored jar bytes" * 1000,
    })
    # Truncate well past the first member but before the archive is
    # complete, so the second member's data is cut short.
    raw = marker.read_bytes()
    marker.write_bytes(raw[: len(raw) - 200])

    state = AgentState()
    assert _apply_pending_restore(tmp_path, state) is False  # must not raise

    assert (tmp_path / "world" / "level.dat").read_bytes() == b"pre-existing level data"
    assert (tmp_path / "plugins" / "OldPlugin.jar").read_bytes() == b"pre-existing jar"
    assert not marker.exists()


def test_apply_pending_restore_does_not_nest_when_a_stale_dir_cant_be_fully_removed(tmp_path, monkeypatch):
    # shutil.rmtree(ignore_errors=True) can leave a directory partially in
    # place (e.g. one file the agent process can't delete) — moving the
    # freshly-extracted directory into an *existing* destination directory
    # nests it instead of replacing it (plugins/plugins/*.jar). This must
    # be treated as a failed restore, not silently produce that layout.
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "Stuck.jar").write_bytes(b"can't be removed")

    marker = tmp_path / PENDING_RESTORE_MARKER
    _write_tar_gz(marker, {"plugins/RestoredPlugin.jar": b"restored jar bytes"})

    real_rmtree = agent_module.shutil.rmtree

    def _rmtree_that_leaves_plugins_behind(path, *args, **kwargs):
        if str(path).endswith("plugins"):
            return  # simulates ignore_errors=True swallowing a real failure
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(agent_module.shutil, "rmtree", _rmtree_that_leaves_plugins_behind)

    state = AgentState()
    assert _apply_pending_restore(tmp_path, state) is False

    # The stale file is still there, untouched — not nested under a new
    # plugins/plugins/ directory, and not silently replaced either.
    assert (tmp_path / "plugins" / "Stuck.jar").read_bytes() == b"can't be removed"
    assert not (tmp_path / "plugins" / "plugins").exists()
    assert state.last_restore_error is not None
    assert not marker.exists()
