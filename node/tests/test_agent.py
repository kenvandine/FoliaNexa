from __future__ import annotations

import tarfile
from io import BytesIO

from folia_node.agent import PENDING_RESTORE_MARKER, _apply_pending_restore


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

    _apply_pending_restore(tmp_path)

    assert (tmp_path / "world" / "level.dat").read_bytes() == b"restored level data"
    assert not (tmp_path / "world" / "stale-region.mca").exists()
    assert (tmp_path / "plugins" / "RestoredPlugin.jar").read_bytes() == b"restored jar bytes"
    assert not (tmp_path / "plugins" / "OldPlugin.jar").exists()
    assert not marker.exists()


def test_apply_pending_restore_is_a_noop_when_no_marker_present(tmp_path):
    _apply_pending_restore(tmp_path)  # must not raise
    assert not (tmp_path / PENDING_RESTORE_MARKER).exists()


def test_apply_pending_restore_skips_a_corrupt_marker_without_crashing(tmp_path):
    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "level.dat").write_bytes(b"pre-existing data")

    marker = tmp_path / PENDING_RESTORE_MARKER
    marker.write_bytes(b"not actually a gzip tarball")

    _apply_pending_restore(tmp_path)  # must not raise, and must not crash-loop the agent

    # Corrupt restore skipped — the world is left exactly as it was,
    # not partially wiped.
    assert (tmp_path / "world" / "level.dat").read_bytes() == b"pre-existing data"
    assert not marker.exists()


def test_apply_pending_restore_rejects_path_traversal_entries(tmp_path):
    marker = tmp_path / PENDING_RESTORE_MARKER
    _write_tar_gz(marker, {"../../etc/passwd": b"malicious"})

    _apply_pending_restore(tmp_path)  # must not raise, must not escape tmp_path

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

    _apply_pending_restore(tmp_path)  # must not raise

    assert (tmp_path / "world" / "level.dat").read_bytes() == b"pre-existing level data"
    assert (tmp_path / "plugins" / "OldPlugin.jar").read_bytes() == b"pre-existing jar"
    assert not marker.exists()
