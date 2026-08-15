from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from folia_mgmt.certs import ensure_client_identity
from folia_mgmt.config import get_settings
from folia_mgmt.db import get_engine, init_db
from folia_mgmt.lxd_client import LXDClient
from folia_mgmt.routers import access_requests, auth, hosts, plugins, routes, users, worlds
from folia_mgmt.scheduler import reconcile

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = 15


async def _reconcile_loop() -> None:
    settings = get_settings()
    cert, key = ensure_client_identity(settings.certs_dir)
    lxd_client = LXDClient(cert, key)
    engine = get_engine(settings)
    while True:
        try:
            with Session(engine) as session:
                reconcile(session, lxd_client)
        except Exception:
            logger.exception("reconcile loop iteration failed")
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db(settings)
    ensure_client_identity(settings.certs_dir)
    task = asyncio.create_task(_reconcile_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    app = FastAPI(title="folia-nexa-mgmt", lifespan=lifespan)

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(hosts.router, prefix="/api/v1")
    app.include_router(worlds.router, prefix="/api/v1")
    app.include_router(users.router, prefix="/api/v1")
    app.include_router(access_requests.router, prefix="/api/v1")
    app.include_router(routes.router, prefix="/api/v1")
    app.include_router(plugins.router, prefix="/api/v1")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Mounted last so /api/v1/* and /healthz above always match first —
    # Starlette tries routes in registration order, and this mount's
    # catch-all would otherwise shadow them.
    static_dir = Path(__file__).parent / "static"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="dashboard")

    return app


app = create_app()
