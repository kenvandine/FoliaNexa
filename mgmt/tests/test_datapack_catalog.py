from __future__ import annotations

from folia_mgmt.config import Settings
from folia_mgmt.datapack_catalog import get_datapack, load_catalog


def test_load_catalog_returns_bundled_entries():
    entries = load_catalog(Settings())
    ids = {e.id for e in entries}
    assert "Matcha" in ids


def test_load_catalog_sorted_by_id_case_insensitive():
    entries = load_catalog(Settings())
    ids = [e.id for e in entries]
    assert ids == sorted(ids, key=str.lower)


def test_get_datapack_known_id():
    entry = get_datapack(Settings(), "Matcha")
    assert entry is not None
    assert entry.category == "gameplay-tweaks"
    assert entry.verified is True
    assert entry.download_url is not None


def test_get_datapack_unknown_id_returns_none():
    assert get_datapack(Settings(), "NotARealDatapack") is None


def test_override_file_adds_new_entry(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "datapack-catalog-override.yaml").write_text(
        """
- id: MyInHouseDatapack
  category: in-house
  source: in-house
  version: "1.0.0"
  download_url: "https://example.internal/my-datapack.zip"
  verified: true
"""
    )
    settings = Settings(state_dir=state_dir)
    entry = get_datapack(settings, "MyInHouseDatapack")
    assert entry is not None
    assert entry.source == "in-house"
    assert entry.download_url == "https://example.internal/my-datapack.zip"

    # bundled entries are still there too — override adds, doesn't replace everything
    assert get_datapack(settings, "Matcha") is not None
