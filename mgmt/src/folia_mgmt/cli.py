"""`folia-nexa-mgmt` command-line entry point. PLAN.md §17.

Talks to the running server's HTTP API using a locally-cached bearer token
(same auth as the dashboard, per PLAN.md §11A) — except `bootstrap-admin`,
which necessarily writes the DB directly since no account exists yet to
authenticate with.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import typer
import uvicorn
from sqlmodel import Session, select

from folia_mgmt.auth import hash_password
from folia_mgmt.config import get_settings
from folia_mgmt.db import get_engine, init_db
from folia_mgmt.models import User, UserRole

app = typer.Typer(help="folia-nexa-mgmt: cluster orchestrator CLI")
hosts_app = typer.Typer(help="Manage trusted LXD hosts")
worlds_app = typer.Typer(help="Manage worlds")
plugins_app = typer.Typer(help="Browse the curated plugin catalog")
app.add_typer(hosts_app, name="hosts")
app.add_typer(worlds_app, name="worlds")
app.add_typer(plugins_app, name="plugins")

_CLI_CONFIG_PATH = Path.home() / ".config" / "folia-nexa-mgmt" / "cli.json"


def _load_cli_config() -> dict:
    if _CLI_CONFIG_PATH.exists():
        return json.loads(_CLI_CONFIG_PATH.read_text())
    return {}


def _save_cli_config(config: dict) -> None:
    _CLI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CLI_CONFIG_PATH.write_text(json.dumps(config))


def _client(mgmt_url: str | None) -> httpx.Client:
    config = _load_cli_config()
    base_url = mgmt_url or config.get("mgmt_url")
    if not base_url:
        typer.echo("No mgmt URL known — pass --mgmt-url or run 'folia-nexa-mgmt login' first.", err=True)
        raise typer.Exit(1)
    token = config.get("token")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=15.0)


def _fail_on_error(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        typer.echo(f"error: HTTP {resp.status_code}: {resp.text}", err=True)
        raise typer.Exit(1)


@app.command()
def serve(host: str | None = None, port: int | None = None) -> None:
    """Run the mgmt server (dashboard + API)."""
    settings = get_settings()
    uvicorn.run(
        "folia_mgmt.main:app",
        host=host or settings.listen_host,
        port=port or settings.listen_port,
    )


@app.command()
def bootstrap_admin(username: str, password: str) -> None:
    """Create the first admin account directly in the DB. Run once, on the
    mgmt host itself, before anyone can log in to issue further accounts."""
    settings = get_settings()
    init_db(settings)
    engine = get_engine(settings)
    with Session(engine) as session:
        if session.exec(select(User).where(User.username == username)).first():
            typer.echo(f"user '{username}' already exists", err=True)
            raise typer.Exit(1)
        session.add(User(username=username, password_hash=hash_password(password), role=UserRole.admin))
        session.commit()
    typer.echo(f"created admin user '{username}'")


@app.command()
def login(mgmt_url: str, username: str, password: str) -> None:
    """Authenticate and cache a session token for subsequent CLI commands."""
    with httpx.Client(base_url=mgmt_url.rstrip("/"), timeout=15.0) as client:
        resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
        _fail_on_error(resp)
        token = resp.json()["token"]
    _save_cli_config({"mgmt_url": mgmt_url.rstrip("/"), "token": token})
    typer.echo("logged in")


@hosts_app.command("create-join-token")
def hosts_create_join_token(mgmt_url: str | None = None) -> None:
    with _client(mgmt_url) as client:
        resp = client.post("/api/v1/hosts/join-token")
        _fail_on_error(resp)
        body = resp.json()
    typer.echo(f"token: {body['token']}")
    typer.echo(f"expires in: {body['expires_in_seconds']}s")


@hosts_app.command("list")
def hosts_list(mgmt_url: str | None = None) -> None:
    with _client(mgmt_url) as client:
        resp = client.get("/api/v1/hosts")
        _fail_on_error(resp)
        for h in resp.json():
            typer.echo(
                f"{h['name']:<16} {h['status']:<10} "
                f"cpu {h['allocated_cpu_cores']}/{h['capacity_cpu_cores']}  "
                f"mem {h['allocated_memory_gb']}/{h['capacity_memory_gb']}GB  "
                f"{h['address']}"
            )


@hosts_app.command("drain")
def hosts_drain(name: str, mgmt_url: str | None = None) -> None:
    with _client(mgmt_url) as client:
        resp = client.post(f"/api/v1/hosts/{name}/drain")
        _fail_on_error(resp)
    typer.echo(f"draining '{name}'")


def _parse_memory_gb(value: str) -> int:
    """Accepts '12' or '12GB' (PLAN.md's rollout examples use the latter)."""
    return int(value.rstrip("GBgb"))


@worlds_app.command("create")
def worlds_create(
    name: str,
    type: str = typer.Option(..., "--type"),
    cpu: int = typer.Option(..., "--cpu"),
    memory: str = typer.Option(..., "--memory", help="e.g. '12' or '12GB'"),
    labels: list[str] = typer.Option([], "--labels", help="key=value, repeatable"),
    plugin: list[str] = typer.Option(
        [], "--plugin", help="plugin catalog id, repeatable — see 'folia-nexa-mgmt plugins list'"
    ),
    engine: str = typer.Option("folia", "--engine"),
    version: str = typer.Option("1.21.4", "--version"),
    mgmt_url: str | None = None,
) -> None:
    memory_gb = _parse_memory_gb(memory)
    placement_labels = dict(item.split("=", 1) for item in labels)
    with _client(mgmt_url) as client:
        resp = client.post(
            "/api/v1/worlds",
            json={
                "name": name,
                "type": type,
                "engine": engine,
                "version": version,
                "plugins": plugin,
                "cpu_cores": cpu,
                "memory_gb": memory_gb,
                "placement_labels": placement_labels,
            },
        )
        _fail_on_error(resp)
    typer.echo(f"declared world '{name}' (phase: pending)")


@worlds_app.command("list")
def worlds_list(mgmt_url: str | None = None) -> None:
    with _client(mgmt_url) as client:
        resp = client.get("/api/v1/worlds")
        _fail_on_error(resp)
        for w in resp.json():
            typer.echo(f"{w['name']:<20} {w['type']:<12} {w['phase']:<14} host={w['host_name']}")


@plugins_app.command("list")
def plugins_list(category: str | None = None, mgmt_url: str | None = None) -> None:
    with _client(mgmt_url) as client:
        resp = client.get("/api/v1/plugins", params={"category": category} if category else None)
        _fail_on_error(resp)
        for p in resp.json():
            verified = "verified" if p["verified"] else "unverified"
            typer.echo(f"{p['id']:<20} {p['category']:<14} {p['source']:<10} {p['version']:<10} {verified}")


@plugins_app.command("show")
def plugins_show(plugin_id: str, mgmt_url: str | None = None) -> None:
    with _client(mgmt_url) as client:
        resp = client.get(f"/api/v1/plugins/{plugin_id}")
        _fail_on_error(resp)
        p = resp.json()
    for key in ("id", "category", "source", "version", "download_url", "sha256", "homepage", "verified", "notes"):
        typer.echo(f"{key}: {p.get(key)}")


if __name__ == "__main__":
    app()
