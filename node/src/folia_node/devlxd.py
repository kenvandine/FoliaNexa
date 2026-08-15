"""Reads this container's own world assignment off the devlxd socket.
PLAN.md §9 — the container's LXD instance config *is* its registration;
no network call to mgmt is needed just to find out what to run.

devlxd (always present at /dev/lxd/sock inside an LXD container, no
credentials needed) only exposes config keys under the `user.` prefix,
which is exactly where mgmt writes `user.folia.*` at launch time
(PLAN.md §9's `lxc launch ... -c user.folia.world-name=... `).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

DEFAULT_SOCKET_PATH = "/dev/lxd/sock"


class DevLXDError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorldAssignment:
    world_name: str
    world_type: str
    jar_url: str
    plugins_manifest_url: str | None = None
    jar_engine: str = "folia"
    jar_version: str = "1.21.4"


class DevLXDClient:
    def __init__(
        self,
        socket_path: str = DEFAULT_SOCKET_PATH,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._socket_path = socket_path
        self._timeout = timeout
        # Injectable for tests (httpx.MockTransport) — real usage always
        # talks over the UDS at socket_path.
        self._transport = transport

    def _client(self) -> httpx.Client:
        transport = self._transport or httpx.HTTPTransport(uds=self._socket_path)
        return httpx.Client(transport=transport, base_url="http://devlxd", timeout=self._timeout)

    def get_config(self) -> dict[str, str]:
        """All `user.*` config keys set on this instance, key -> value."""
        with self._client() as client:
            resp = client.get("/1.0/config")
            if resp.status_code != 200:
                raise DevLXDError(f"devlxd /1.0/config failed: HTTP {resp.status_code}")
            key_paths: list[str] = resp.json()

            config: dict[str, str] = {}
            for key_path in key_paths:
                value_resp = client.get(key_path)
                if value_resp.status_code != 200:
                    continue
                config[key_path.rsplit("/", 1)[-1]] = value_resp.text
            return config

    def get_world_assignment(self) -> WorldAssignment:
        config = self.get_config()
        try:
            world_name = config["user.folia.world-name"]
            world_type = config["user.folia.world-type"]
            jar_url = config["user.folia.jar-url"]
        except KeyError as exc:
            raise DevLXDError(
                f"missing required world assignment key {exc} — "
                "was this container launched by folia-nexa-mgmt's scheduler?"
            ) from exc

        return WorldAssignment(
            world_name=world_name,
            world_type=world_type,
            jar_url=jar_url,
            plugins_manifest_url=config.get("user.folia.plugins-manifest-url"),
            jar_engine=config.get("user.folia.jar-engine", "folia"),
            jar_version=config.get("user.folia.jar-version", "1.21.4"),
        )
