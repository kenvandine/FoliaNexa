from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlmodel import Session, SQLModel, create_engine

from folia_mgmt.config import Settings, get_settings


@lru_cache
def _engine_for(db_url: str):
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    return create_engine(db_url, connect_args=connect_args)


def get_engine(settings: Settings | None = None):
    settings = settings or get_settings()
    return _engine_for(f"sqlite:///{settings.db_path}")


def init_db(settings: Settings | None = None) -> None:
    engine = get_engine(settings)
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    # No parameters (beyond what FastAPI itself would inject) on purpose:
    # a plain pydantic-model-typed parameter here — even an optional one —
    # gets picked up by FastAPI as an implicit extra request-body field on
    # every endpoint that depends on this, which silently breaks body
    # parsing for routes that also declare their own body model.
    engine = get_engine()
    with Session(engine) as session:
        yield session
