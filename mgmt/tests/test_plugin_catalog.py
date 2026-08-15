from __future__ import annotations

from folia_mgmt.config import Settings
from folia_mgmt.plugin_catalog import get_plugin, load_catalog


def test_load_catalog_returns_bundled_entries():
    entries = load_catalog(Settings())
    ids = {e.id for e in entries}
    assert "LuckPerms" in ids
    assert "Spark" in ids


def test_load_catalog_sorted_by_id_case_insensitive():
    entries = load_catalog(Settings())
    ids = [e.id for e in entries]
    assert ids == sorted(ids, key=str.lower)


def test_get_plugin_known_id():
    entry = get_plugin(Settings(), "LuckPerms")
    assert entry is not None
    assert entry.category == "permissions"
    assert entry.verified is True


def test_get_plugin_unknown_id_returns_none():
    assert get_plugin(Settings(), "NotARealPlugin") is None


def test_server_selector_is_a_verified_lobby_entry():
    entry = get_plugin(Settings(), "ServerSelector")
    assert entry is not None
    assert entry.category == "lobby"
    assert entry.verified is True
    assert entry.download_url is not None


def test_override_file_adds_new_entry(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "plugin-catalog-override.yaml").write_text(
        """
- id: MyInHousePlugin
  category: in-house
  source: in-house
  version: "1.0.0"
  download_url: "https://example.internal/my-plugin.jar"
  verified: true
"""
    )
    settings = Settings(state_dir=state_dir)
    entry = get_plugin(settings, "MyInHousePlugin")
    assert entry is not None
    assert entry.source == "in-house"
    assert entry.download_url == "https://example.internal/my-plugin.jar"

    # bundled entries are still there too — override adds, doesn't replace everything
    assert get_plugin(settings, "LuckPerms") is not None


def test_override_file_replaces_bundled_entry(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "plugin-catalog-override.yaml").write_text(
        """
- id: LuckPerms
  category: permissions
  source: external
  version: "99.0.0"
  download_url: "https://mirror.internal/luckperms-pinned.jar"
  verified: true
"""
    )
    settings = Settings(state_dir=state_dir)
    entry = get_plugin(settings, "LuckPerms")
    assert entry.version == "99.0.0"
    assert entry.download_url == "https://mirror.internal/luckperms-pinned.jar"

    # still exactly one LuckPerms entry, not a duplicate
    matches = [e for e in load_catalog(settings) if e.id == "LuckPerms"]
    assert len(matches) == 1


def test_missing_bundled_and_override_files_returns_empty(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    settings = Settings(state_dir=state_dir, plugin_catalog_path=tmp_path / "does-not-exist.yaml")
    assert load_catalog(settings) == []
