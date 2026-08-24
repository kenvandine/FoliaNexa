from __future__ import annotations

import json
import logging
from collections.abc import Generator
from functools import lru_cache

from pydantic_core import PydanticUndefined
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from folia_mgmt import models
from folia_mgmt.config import Settings, get_settings

logger = logging.getLogger(__name__)


@lru_cache
def _engine_for(db_url: str):
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    return create_engine(db_url, connect_args=connect_args)


def get_engine(settings: Settings | None = None):
    settings = settings or get_settings()
    return _engine_for(f"sqlite:///{settings.db_path}")


def _add_missing_columns(engine) -> None:
    """SQLModel.metadata.create_all only creates tables that don't exist
    yet — it never alters an existing table's schema, so adding a field to
    an already-table=True model (e.g. World.properties) silently 500s on
    every query against that table on an already-deployed instance until
    something adds the column for real. Confirmed the hard way: this
    exact gap took down live route polling for a few seconds on
    2026-08-16 when World.properties shipped with no migration story.

    Not a real migration framework — no renames, drops, or type changes,
    no rollback. Just enough to make "add an optional column with a
    default" safe, which is the only kind of schema change this codebase
    has needed so far. JSON columns get their SQLModel default_factory
    backfilled as the column's SQL DEFAULT (matching plugins/datapacks/
    properties/placement_labels/ops's existing default_factory=list/dict
    pattern) so existing rows read back as `[]`/`{}`, not `None` — a
    non-Optional Pydantic response field (e.g. WorldResponse.properties)
    would otherwise 500 on serialization for every pre-existing row.
    Plain-scalar defaults (e.g. AccessRequest.auto_managed: bool = True)
    get the same treatment, added alongside that field — without it,
    existing rows would backfill to NULL/None instead of the model's
    actual default.
    """
    model_by_table = {
        cls.__tablename__: cls
        for cls in vars(models).values()
        if isinstance(cls, type) and issubclass(cls, SQLModel) and cls is not SQLModel and hasattr(cls, "__tablename__")
    }

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue  # brand new table — create_all already made it, nothing to add
            existing = {col["name"] for col in inspector.get_columns(table.name)}
            model = model_by_table.get(table.name)
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl_type = column.type.compile(dialect=conn.dialect)
                default_clause = ""
                field = model.model_fields.get(column.name) if model is not None else None
                if field is not None and field.default_factory is not None:
                    default_clause = f" NOT NULL DEFAULT '{json.dumps(field.default_factory())}'"
                elif field is not None and field.default is not None and field.default is not PydanticUndefined:
                    if isinstance(field.default, bool):
                        default_clause = f" NOT NULL DEFAULT {1 if field.default else 0}"
                    elif isinstance(field.default, (int, float)):
                        default_clause = f" NOT NULL DEFAULT {field.default}"
                    elif isinstance(field.default, str):
                        default_clause = f" NOT NULL DEFAULT '{field.default}'"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}{default_clause}'))


def _purge_soft_deleted_worlds(engine) -> None:
    """One-time cleanup for rows left behind by teardown_world's old
    behavior (scheduler.py used to set phase='deleted' and keep the row
    forever, rather than actually deleting it) — World.name is a real
    unique column, so a lingering deleted-phase row permanently blocked
    reusing that world name. teardown_world hard-deletes for real now;
    this just sweeps up whatever's already stuck from before that fix, on
    every startup (a no-op once there's nothing left to sweep)."""
    inspector = inspect(engine)
    if not inspector.has_table("world"):
        return
    with engine.begin() as conn:
        result = conn.execute(text("DELETE FROM world WHERE phase = 'deleted'"))
        if result.rowcount:
            logger.info(
                "purged %d soft-deleted world row(s) left over from before teardown_world hard-deleted for real",
                result.rowcount,
            )


_SCHEMA_MIGRATION_TABLE = "schema_migration"


def _migration_applied(conn, name: str) -> bool:
    """Tiny generic idempotency marker for one-time data migrations that
    can't be expressed as "delete rows matching some real, permanent
    column state" — not a real migration framework (no ordering, no
    rollback, see _add_missing_columns' own docstring for this module's
    scope), just enough to record "this one-off cleanup already ran"
    without overloading an unrelated column's semantics as a marker.
    See _purge_legacy_lxd_snapshot_backups for why that mattered here."""
    conn.execute(text(f'CREATE TABLE IF NOT EXISTS "{_SCHEMA_MIGRATION_TABLE}" (name TEXT PRIMARY KEY)'))
    row = conn.execute(text(f'SELECT 1 FROM "{_SCHEMA_MIGRATION_TABLE}" WHERE name = :name'), {"name": name}).first()
    return row is not None


def _mark_migration_applied(conn, name: str) -> None:
    conn.execute(text(f'INSERT OR IGNORE INTO "{_SCHEMA_MIGRATION_TABLE}" (name) VALUES (:name)'), {"name": name})


def _purge_legacy_lxd_snapshot_backups(engine) -> None:
    """One-time cleanup for WorldBackup rows created before the
    file-level backup redesign — they reference LXD snapshot names the
    new restore path (world_backups.py) can't do anything with, since
    there's no corresponding tarball on disk for them.

    Originally targeted by `size_bytes IS NULL` alone (that column didn't
    exist before this change, so every pre-existing row had it NULL,
    backfilled by _add_missing_columns above) on the reasoning that every
    backup created by the new fetch_and_store_backup always sets it to a
    real byte count, making the DELETE naturally a no-op after the first
    run. That overloads a column that's genuinely nullable in the schema
    (WorldBackup.size_bytes: Optional[int]) as an implicit "is this a
    legacy row" flag — a future code path that ever constructs a
    WorldBackup row before its size is known (e.g. a streaming-backup-
    in-progress row) would get silently deleted by this on the very next
    mgmt restart, indistinguishable from a genuine pre-redesign row. Now
    gated by a real one-time migration marker (_migration_applied) in
    addition to the size_bytes filter, so this DELETE only ever runs
    once, ever — not "once per NULL-size row that happens to exist."
    The underlying LXD snapshots these rows pointed at are left orphaned
    on their host's storage pool rather than best-effort-deleted through
    the very snapshot API this change exists to stop depending on for
    routine backups."""
    MIGRATION_NAME = "purge_legacy_lxd_snapshot_backups"
    inspector = inspect(engine)
    if not inspector.has_table("worldbackup"):
        return
    with engine.begin() as conn:
        if _migration_applied(conn, MIGRATION_NAME):
            return
        result = conn.execute(text("DELETE FROM worldbackup WHERE size_bytes IS NULL"))
        if result.rowcount:
            logger.info(
                "purged %d legacy LXD-snapshot-based world backup row(s) — "
                "no longer restorable via the file-level backup path",
                result.rowcount,
            )
        _mark_migration_applied(conn, MIGRATION_NAME)


def init_db(settings: Settings | None = None) -> None:
    engine = get_engine(settings)
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    _purge_soft_deleted_worlds(engine)
    _purge_legacy_lxd_snapshot_backups(engine)


def get_session() -> Generator[Session, None, None]:
    # No parameters (beyond what FastAPI itself would inject) on purpose:
    # a plain pydantic-model-typed parameter here — even an optional one —
    # gets picked up by FastAPI as an implicit extra request-body field on
    # every endpoint that depends on this, which silently breaks body
    # parsing for routes that also declare their own body model.
    engine = get_engine()
    with Session(engine) as session:
        yield session
