from __future__ import annotations

import json
import logging
from collections.abc import Generator
from functools import lru_cache

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


def init_db(settings: Settings | None = None) -> None:
    engine = get_engine(settings)
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    _purge_soft_deleted_worlds(engine)


def get_session() -> Generator[Session, None, None]:
    # No parameters (beyond what FastAPI itself would inject) on purpose:
    # a plain pydantic-model-typed parameter here — even an optional one —
    # gets picked up by FastAPI as an implicit extra request-body field on
    # every endpoint that depends on this, which silently breaks body
    # parsing for routes that also declare their own body model.
    engine = get_engine()
    with Session(engine) as session:
        yield session
