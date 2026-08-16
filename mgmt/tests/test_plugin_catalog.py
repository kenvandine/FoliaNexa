from __future__ import annotations

from folia_mgmt.config import Settings
from folia_mgmt.plugin_catalog import (
    PluginEntry,
    delete_plugin_override,
    get_plugin,
    load_catalog,
    overridden_ids,
    save_plugin_override,
)


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


def test_save_plugin_override_adds_new_entry(tmp_path):
    settings = Settings(state_dir=tmp_path / "state")
    entry = PluginEntry(
        id="MyNewPlugin", category="misc", source="in-house", version="1.0.0",
        download_url="https://example.internal/my-new-plugin.jar", verified=True,
    )
    save_plugin_override(settings, entry)

    saved = get_plugin(settings, "MyNewPlugin")
    assert saved is not None
    assert saved.download_url == "https://example.internal/my-new-plugin.jar"
    assert saved.verified is True
    assert "MyNewPlugin" in overridden_ids(settings)
    # bundled entries untouched
    assert get_plugin(settings, "LuckPerms") is not None


def test_save_plugin_override_edits_existing_bundled_entry(tmp_path):
    settings = Settings(state_dir=tmp_path / "state")
    original = get_plugin(settings, "LuckPerms")
    updated = original.model_copy(update={"version": "99.9.9", "download_url": "https://mirror.internal/lp.jar"})
    save_plugin_override(settings, updated)

    result = get_plugin(settings, "LuckPerms")
    assert result.version == "99.9.9"
    assert result.download_url == "https://mirror.internal/lp.jar"
    matches = [e for e in load_catalog(settings) if e.id == "LuckPerms"]
    assert len(matches) == 1


def test_save_plugin_override_twice_upserts_not_duplicates(tmp_path):
    settings = Settings(state_dir=tmp_path / "state")
    entry = PluginEntry(id="Thing", category="misc", source="external", version="1.0.0")
    save_plugin_override(settings, entry)
    save_plugin_override(settings, entry.model_copy(update={"version": "2.0.0"}))

    matches = [e for e in load_catalog(settings) if e.id == "Thing"]
    assert len(matches) == 1
    assert matches[0].version == "2.0.0"


def test_delete_plugin_override_removes_operator_added_entry(tmp_path):
    settings = Settings(state_dir=tmp_path / "state")
    save_plugin_override(settings, PluginEntry(id="Thing", category="misc", source="external", version="1.0.0"))
    assert delete_plugin_override(settings, "Thing") is True
    assert get_plugin(settings, "Thing") is None


def test_delete_plugin_override_falls_back_to_bundled(tmp_path):
    settings = Settings(state_dir=tmp_path / "state")
    save_plugin_override(settings, get_plugin(settings, "LuckPerms").model_copy(update={"version": "99.9.9"}))
    assert get_plugin(settings, "LuckPerms").version == "99.9.9"

    assert delete_plugin_override(settings, "LuckPerms") is True
    # falls back to the bundled catalog.yaml entry, not gone entirely
    restored = get_plugin(settings, "LuckPerms")
    assert restored is not None
    assert restored.version != "99.9.9"


def test_delete_plugin_override_nonexistent_returns_false(tmp_path):
    settings = Settings(state_dir=tmp_path / "state")
    assert delete_plugin_override(settings, "NoSuchOverride") is False


def test_overridden_ids_empty_when_no_override_file(tmp_path):
    settings = Settings(state_dir=tmp_path / "state")
    assert overridden_ids(settings) == set()
