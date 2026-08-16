from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, select

from folia_mgmt.db import _add_missing_columns
from folia_mgmt.models import World, WorldType


def test_add_missing_columns_backfills_existing_rows_with_default(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")

    # Simulates a production DB created before World.properties existed —
    # a real "world" table, deliberately missing that one column, with an
    # already-existing row in it (the scenario that actually broke on
    # 2026-08-16: every query against this table 500'd until the column
    # was added).
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE world ("
                "id INTEGER PRIMARY KEY, name VARCHAR UNIQUE, type VARCHAR, engine VARCHAR, "
                "version VARCHAR, plugins JSON, datapacks JSON, cpu_cores INTEGER, memory_gb INTEGER, "
                "placement_labels JSON, sticky_host VARCHAR, snapshot_schedule VARCHAR, "
                "snapshot_expiry VARCHAR, phase VARCHAR, host_name VARCHAR, container_name VARCHAR, "
                "address VARCHAR, whitelist_enabled BOOLEAN, ops JSON, created_at DATETIME, updated_at DATETIME"
                ")"
            )
        )
        conn.execute(
            text(
                "INSERT INTO world (name, type, engine, version, plugins, datapacks, cpu_cores, memory_gb, "
                "placement_labels, phase, whitelist_enabled, ops, created_at, updated_at) VALUES "
                "('world-overworld', 'overworld', 'folia', '1.21.4', '[]', '[]', 4, 8, '{}', 'running', 0, "
                "'[]', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )

    _add_missing_columns(engine)

    columns = {col["name"] for col in inspect(engine).get_columns("world")}
    assert "properties" in columns

    with Session(engine) as session:
        world = session.exec(select(World).where(World.name == "world-overworld")).one()
        assert world.properties == {}
        assert world.type == WorldType.overworld


def test_add_missing_columns_is_a_noop_on_a_current_schema(tmp_path):
    from sqlmodel import SQLModel

    engine = create_engine(f"sqlite:///{tmp_path / 'current.db'}")
    SQLModel.metadata.create_all(engine)

    # Should not raise (no missing columns to add) and should leave data
    # intact — run twice to also prove idempotency.
    _add_missing_columns(engine)
    _add_missing_columns(engine)

    columns = {col["name"] for col in inspect(engine).get_columns("world")}
    assert "properties" in columns
