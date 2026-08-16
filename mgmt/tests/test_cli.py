from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from folia_mgmt import cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_cli_config(tmp_path, monkeypatch):
    """Every test gets its own cli.json — never touch the real
    ~/.config/folia-nexa-mgmt/cli.json."""
    monkeypatch.setattr(cli, "_CLI_CONFIG_PATH", tmp_path / "cli.json")


@pytest.fixture
def mock_http(monkeypatch):
    """Patches httpx.Client (the same class object cli.py's `import httpx`
    resolves to) so every client it constructs — including login()'s
    inline one and _client()'s — routes through a per-test handler
    instead of a real socket, while keeping base_url/headers/timeout
    behavior exactly as the real client would apply them."""
    real_client_cls = httpx.Client

    def install(handler):
        def factory(*args, **kwargs):
            kwargs.pop("transport", None)
            return real_client_cls(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, "Client", factory)

    return install


def test_parse_memory_gb_accepts_bare_number():
    assert cli._parse_memory_gb("12") == 12


def test_parse_memory_gb_accepts_gb_suffix_either_case():
    assert cli._parse_memory_gb("12GB") == 12
    assert cli._parse_memory_gb("12gb") == 12


def test_load_cli_config_missing_file_returns_empty_dict():
    assert cli._load_cli_config() == {}


def test_save_and_load_cli_config_roundtrip():
    cli._save_cli_config({"mgmt_url": "https://x", "token": "t"})
    assert cli._load_cli_config() == {"mgmt_url": "https://x", "token": "t"}


def test_client_without_known_mgmt_url_exits_nonzero():
    with pytest.raises(cli.typer.Exit):
        cli._client(None)


def test_bootstrap_admin_creates_user(tmp_path, monkeypatch):
    monkeypatch.setenv("FOLIA_MGMT_STATE_DIR", str(tmp_path / "state"))
    result = runner.invoke(cli.app, ["bootstrap-admin", "admin", "adminpass123"])
    assert result.exit_code == 0, result.output
    assert "created admin user 'admin'" in result.output


def test_bootstrap_admin_rejects_duplicate_username(tmp_path, monkeypatch):
    monkeypatch.setenv("FOLIA_MGMT_STATE_DIR", str(tmp_path / "state"))
    runner.invoke(cli.app, ["bootstrap-admin", "admin", "adminpass123"])
    result = runner.invoke(cli.app, ["bootstrap-admin", "admin", "different-password"])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_login_saves_token_and_url(mock_http):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/auth/login"
        body = json.loads(request.content)
        assert body == {"username": "admin", "password": "hunter2"}
        return httpx.Response(200, json={"token": "tok-abc", "role": "admin"})

    mock_http(handler)
    result = runner.invoke(cli.app, ["login", "https://mgmt.example:8443", "admin", "hunter2"])
    assert result.exit_code == 0, result.output
    assert "logged in" in result.output
    assert cli._load_cli_config() == {"mgmt_url": "https://mgmt.example:8443", "token": "tok-abc"}


def test_login_failure_reports_error(mock_http):
    mock_http(lambda r: httpx.Response(401, json={"detail": "invalid username or password"}))
    result = runner.invoke(cli.app, ["login", "https://mgmt.example:8443", "admin", "wrong"])
    assert result.exit_code != 0
    assert "401" in result.output


def _seed_login(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_CLI_CONFIG_PATH", tmp_path / "cli.json")
    cli._save_cli_config({"mgmt_url": "https://mgmt.example:8443", "token": "tok-abc"})


def test_hosts_create_join_token(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hosts/join-token"
        assert request.headers["Authorization"] == "Bearer tok-abc"
        return httpx.Response(200, json={"token": "join-xyz", "expires_in_seconds": 900})

    mock_http(handler)
    result = runner.invoke(cli.app, ["hosts", "create-join-token"])
    assert result.exit_code == 0, result.output
    assert "join-xyz" in result.output
    assert "900" in result.output


def test_hosts_list_formats_rows(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)
    mock_http(
        lambda r: httpx.Response(
            200,
            json=[
                {
                    "name": "node-a",
                    "address": "10.0.1.11:8443",
                    "project": "folia",
                    "status": "online",
                    "labels": {},
                    "capacity_cpu_cores": 6,
                    "capacity_memory_gb": 12,
                    "allocated_cpu_cores": 2,
                    "allocated_memory_gb": 4,
                }
            ],
        )
    )
    result = runner.invoke(cli.app, ["hosts", "list"])
    assert result.exit_code == 0, result.output
    assert "node-a" in result.output
    assert "cpu 2/6" in result.output
    assert "mem 4/12GB" in result.output


def test_hosts_drain(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hosts/node-a/drain"
        return httpx.Response(200, json={})

    mock_http(handler)
    result = runner.invoke(cli.app, ["hosts", "drain", "node-a"])
    assert result.exit_code == 0, result.output
    assert "draining 'node-a'" in result.output


def test_worlds_create_sends_plugins_and_labels(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    mock_http(handler)
    result = runner.invoke(
        cli.app,
        [
            "worlds", "create", "world-survival",
            "--type", "overworld",
            "--cpu", "6",
            "--memory", "12GB",
            "--labels", "cpu_type=p-core",
            "--plugin", "LuckPerms",
            "--plugin", "HuskClaims",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["body"] == {
        "name": "world-survival",
        "type": "overworld",
        "plugins": ["LuckPerms", "HuskClaims"],
        "datapacks": [],
        "cpu_cores": 6,
        "memory_gb": 12,
        "placement_labels": {"cpu_type": "p-core"},
    }
    assert "declared world 'world-survival'" in result.output


def test_worlds_create_sends_datapacks(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    mock_http(handler)
    result = runner.invoke(
        cli.app,
        [
            "worlds", "create", "world-survival",
            "--type", "overworld",
            "--cpu", "6",
            "--memory", "12GB",
            "--datapack", "Matcha",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["body"]["datapacks"] == ["Matcha"]


def test_worlds_create_without_plugins_sends_empty_list(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    mock_http(handler)
    result = runner.invoke(
        cli.app,
        ["worlds", "create", "world-nether", "--type", "nether", "--cpu", "2", "--memory", "3"],
    )
    assert result.exit_code == 0, result.output
    assert captured["body"]["plugins"] == []
    assert captured["body"]["memory_gb"] == 3


def test_worlds_list_formats_rows(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)
    mock_http(
        lambda r: httpx.Response(
            200,
            json=[{"name": "world-overworld", "type": "overworld", "phase": "running", "host_name": "node-a"}],
        )
    )
    result = runner.invoke(cli.app, ["worlds", "list"])
    assert result.exit_code == 0, result.output
    assert "world-overworld" in result.output
    assert "host=node-a" in result.output


def test_command_error_surfaces_server_detail(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)
    mock_http(lambda r: httpx.Response(409, json={"detail": "world 'x' already exists"}))
    result = runner.invoke(
        cli.app,
        ["worlds", "create", "x", "--type", "lobby", "--cpu", "1", "--memory", "1"],
    )
    assert result.exit_code != 0
    assert "409" in result.output
    assert "already exists" in result.output


def test_worlds_delete_calls_api_and_warns_unrecoverable(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v1/worlds/world-minigame-parkour"
        return httpx.Response(200, json={})

    mock_http(handler)
    result = runner.invoke(cli.app, ["worlds", "delete", "world-minigame-parkour"])
    assert result.exit_code == 0, result.output
    assert "draining 'world-minigame-parkour'" in result.output
    assert "unrecoverable" in result.output


def test_worlds_snapshot_prints_snapshot_name(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/worlds/world-overworld/snapshot"
        return httpx.Response(200, json={"snapshot": "manual-123"})

    mock_http(handler)
    result = runner.invoke(cli.app, ["worlds", "snapshot", "world-overworld"])
    assert result.exit_code == 0, result.output
    assert "manual-123" in result.output


def test_worlds_snapshot_passes_custom_name(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"snapshot": "before-event"})

    mock_http(handler)
    result = runner.invoke(cli.app, ["worlds", "snapshot", "world-overworld", "--snapshot-name", "before-event"])
    assert result.exit_code == 0, result.output
    assert captured["params"] == {"snapshot_name": "before-event"}


def test_worlds_restore_calls_api(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/worlds/world-overworld/restore/before-event"
        return httpx.Response(200, json={"restored": "before-event"})

    mock_http(handler)
    result = runner.invoke(cli.app, ["worlds", "restore", "world-overworld", "before-event"])
    assert result.exit_code == 0, result.output
    assert "restored 'world-overworld' from snapshot 'before-event'" in result.output


def test_plugins_list_formats_rows(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)
    mock_http(
        lambda r: httpx.Response(
            200,
            json=[
                {
                    "id": "LuckPerms",
                    "category": "permissions",
                    "source": "external",
                    "version": "5.5.76",
                    "verified": True,
                }
            ],
        )
    )
    result = runner.invoke(cli.app, ["plugins", "list"])
    assert result.exit_code == 0, result.output
    assert "LuckPerms" in result.output
    assert "permissions" in result.output
    assert "verified" in result.output


def test_plugins_list_passes_category_filter(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    mock_http(handler)
    result = runner.invoke(cli.app, ["plugins", "list", "--category", "permissions"])
    assert result.exit_code == 0, result.output
    assert captured["params"] == {"category": "permissions"}


def test_plugins_show_prints_fields(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)
    mock_http(
        lambda r: httpx.Response(
            200,
            json={
                "id": "LuckPerms",
                "category": "permissions",
                "source": "external",
                "version": "5.5.76",
                "download_url": "https://download.luckperms.net/1663/bukkit/loader/LuckPerms-Bukkit-5.5.76.jar",
                "sha256": "0d6bcf05811c21a7ab823c38dc8e46bcf6e0fb70b8b871c7e3c719ac8e170cfd",
                "homepage": None,
                "verified": True,
                "notes": None,
            },
        )
    )
    result = runner.invoke(cli.app, ["plugins", "show", "LuckPerms"])
    assert result.exit_code == 0, result.output
    assert "id: LuckPerms" in result.output
    assert "verified: True" in result.output


def test_plugins_show_unknown_id_surfaces_404(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)
    mock_http(lambda r: httpx.Response(404, json={"detail": "no such plugin 'Nope' in the catalog"}))
    result = runner.invoke(cli.app, ["plugins", "show", "Nope"])
    assert result.exit_code != 0
    assert "404" in result.output


def test_datapacks_list_formats_rows(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)
    mock_http(
        lambda r: httpx.Response(
            200,
            json=[
                {
                    "id": "Matcha",
                    "category": "gameplay-tweaks",
                    "source": "external",
                    "version": "1.12",
                    "verified": True,
                }
            ],
        )
    )
    result = runner.invoke(cli.app, ["datapacks", "list"])
    assert result.exit_code == 0, result.output
    assert "Matcha" in result.output
    assert "gameplay-tweaks" in result.output
    assert "verified" in result.output


def test_datapacks_show_prints_fields(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)
    mock_http(
        lambda r: httpx.Response(
            200,
            json={
                "id": "Matcha",
                "category": "gameplay-tweaks",
                "source": "external",
                "version": "1.12",
                "download_url": "https://cdn.modrinth.com/data/QI0EmgZ1/versions/E9rngRfK/Matcha_Flavoured_1_12.zip",
                "sha256": "6209783021c358044abedabacee471faff5bd4080437d4e3b5e51963f1804248",
                "homepage": "https://modrinth.com/datapack/matcha-flavoured",
                "verified": True,
                "notes": None,
            },
        )
    )
    result = runner.invoke(cli.app, ["datapacks", "show", "Matcha"])
    assert result.exit_code == 0, result.output
    assert "id: Matcha" in result.output
    assert "verified: True" in result.output


def test_datapacks_show_unknown_id_surfaces_404(tmp_path, monkeypatch, mock_http):
    _seed_login(tmp_path, monkeypatch)
    mock_http(lambda r: httpx.Response(404, json={"detail": "no such datapack 'Nope' in the catalog"}))
    result = runner.invoke(cli.app, ["datapacks", "show", "Nope"])
    assert result.exit_code != 0
    assert "404" in result.output
