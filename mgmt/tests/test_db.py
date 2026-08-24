from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, select

from folia_mgmt.db import _add_missing_columns, _purge_legacy_lxd_snapshot_backups, _purge_soft_deleted_worlds
from folia_mgmt.models import AccessRequest, PlayerStat, World, WorldBackup, WorldType


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


def test_add_missing_columns_backfills_scalar_bool_default(tmp_path):
    """Regression test for the class of bug the docstring above
    _add_missing_columns warns about, now exercised for a plain scalar
    default (AccessRequest.auto_managed: bool = True) rather than a JSON
    default_factory column — without the fix, existing rows would
    backfill to NULL/None instead of the model's actual default."""
    engine = create_engine(f"sqlite:///{tmp_path / 'old_access_request.db'}")

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE accessrequest ("
                "id INTEGER PRIMARY KEY, discord_user_id VARCHAR, discord_username VARCHAR, "
                "minecraft_username VARCHAR, minecraft_uuid VARCHAR, status VARCHAR, "
                "created_at DATETIME, decided_at DATETIME, decided_by INTEGER, deny_reason VARCHAR"
                ")"
            )
        )
        conn.execute(
            text(
                "INSERT INTO accessrequest (discord_user_id, discord_username, status, created_at) VALUES "
                "('123', 'somebody', 'approved', '2026-01-01 00:00:00')"
            )
        )

    _add_missing_columns(engine)

    columns = {col["name"] for col in inspect(engine).get_columns("accessrequest")}
    assert "auto_managed" in columns

    with Session(engine) as session:
        request = session.exec(select(AccessRequest)).one()
        assert request.auto_managed is True


def test_add_missing_columns_backfills_playerstat_world_name_as_legacy(tmp_path):
    """The actual live-data migration for the multi-world stats fix
    (routers/stats.py) — PlayerStat grew a world_name column so a
    player's contributions from different worlds no longer clobber each
    other. Existing rows, written back when there was no such concept,
    must backfill into the "__legacy__" bucket (routers/stats.py's
    LEGACY_WORLD_NAME) — the same bucket a not-yet-upgraded plugin's
    reports land in — rather than NULL, which would break the SUM()
    GROUP BY queries public_stats.py now does over this column."""
    engine = create_engine(f"sqlite:///{tmp_path / 'old_playerstat.db'}")

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE playerstat ("
                "id INTEGER PRIMARY KEY, player_uuid VARCHAR, stat_key VARCHAR, "
                "value FLOAT, updated_at DATETIME"
                ")"
            )
        )
        conn.execute(
            text(
                "INSERT INTO playerstat (player_uuid, stat_key, value, updated_at) VALUES "
                "('uuid-a', 'kills', 42, '2026-01-01 00:00:00')"
            )
        )

    _add_missing_columns(engine)

    columns = {col["name"] for col in inspect(engine).get_columns("playerstat")}
    assert "world_name" in columns

    with Session(engine) as session:
        stat = session.exec(select(PlayerStat).where(PlayerStat.player_uuid == "uuid-a")).one()
        assert stat.world_name == "__legacy__"
        assert stat.value == 42


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


def test_purge_soft_deleted_worlds_removes_deleted_phase_rows(tmp_path):
    from sqlmodel import SQLModel

    from folia_mgmt.models import WorldPhase

    engine = create_engine(f"sqlite:///{tmp_path / 'stale.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(World(name="still-running", type=WorldType.overworld, cpu_cores=1, memory_gb=1))
        session.add(
            World(name="torn-down", type=WorldType.overworld, cpu_cores=1, memory_gb=1, phase=WorldPhase.deleted)
        )
        session.commit()

    _purge_soft_deleted_worlds(engine)

    with Session(engine) as session:
        names = {w.name for w in session.exec(select(World)).all()}
    assert names == {"still-running"}


def test_purge_soft_deleted_worlds_is_a_noop_with_nothing_to_purge(tmp_path):
    from sqlmodel import SQLModel

    engine = create_engine(f"sqlite:///{tmp_path / 'clean.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(World(name="still-running", type=WorldType.overworld, cpu_cores=1, memory_gb=1))
        session.commit()

    _purge_soft_deleted_worlds(engine)  # should not raise

    with Session(engine) as session:
        assert session.exec(select(World)).one().name == "still-running"


def test_purge_soft_deleted_worlds_handles_missing_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    _purge_soft_deleted_worlds(engine)  # should not raise even with no tables at all


def test_purge_legacy_lxd_snapshot_backups_removes_null_size_rows_once(tmp_path):
    from sqlmodel import SQLModel

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(WorldBackup(world_name="w", snapshot_name="legacy-snap", kind="scheduled", size_bytes=None))
        session.commit()

    _purge_legacy_lxd_snapshot_backups(engine)

    with Session(engine) as session:
        assert session.exec(select(WorldBackup)).all() == []


def test_purge_legacy_lxd_snapshot_backups_does_not_delete_a_future_null_size_row_on_a_later_startup(tmp_path):
    # The original implementation targeted `size_bytes IS NULL` alone,
    # with no real one-time marker — every mgmt restart would re-run the
    # same DELETE, so any *future* legitimate NULL-size row (e.g. a
    # streaming-backup-in-progress row that doesn't exist today, but
    # could) would be silently deleted on the very next startup, forever,
    # indistinguishable from a genuine pre-redesign legacy row. This
    # confirms the fix: the DELETE only ever runs once, gated by a real
    # migration marker, not "once per NULL-size row that happens to
    # exist right now."
    from sqlmodel import SQLModel

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy2.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(WorldBackup(world_name="w", snapshot_name="legacy-snap", kind="scheduled", size_bytes=None))
        session.commit()

    _purge_legacy_lxd_snapshot_backups(engine)  # first ever run — purges the legacy row

    # A brand new NULL-size row shows up after the one-time purge already
    # ran (this session's own analogue of a future "in-progress" row) —
    # a second call (e.g. mgmt restarting again) must leave it alone.
    with Session(engine) as session:
        session.add(WorldBackup(world_name="w", snapshot_name="in-progress", kind="scheduled", size_bytes=None))
        session.commit()

    _purge_legacy_lxd_snapshot_backups(engine)

    with Session(engine) as session:
        names = {b.snapshot_name for b in session.exec(select(WorldBackup)).all()}
    assert names == {"in-progress"}


def test_purge_legacy_lxd_snapshot_backups_handles_missing_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty2.db'}")
    _purge_legacy_lxd_snapshot_backups(engine)  # should not raise even with no tables at all
