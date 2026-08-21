"""Thin wrapper over the LXD remote (HTTPS) API. PLAN.md §3, §4, §5.

mgmt never touches a local LXD unix socket — every host, including one
colocated on the same machine, is reached the same way: mTLS over HTTPS,
with the peer's leaf certificate pinned by fingerprint at enrollment time
(TOFU) rather than verified against a CA, since LXD hosts use self-signed
certs by default.

NOTE: this has been written against the documented LXD REST API contract
(trust-token bootstrap via POST /1.0/certificates, instance CRUD under
/1.0/instances, async operations under /1.0/operations) but has not been
exercised against a live LXD daemon in this environment. Treat the exact
request/response shapes as a first draft to validate against a real host.
"""

from __future__ import annotations

import hashlib
import socket
import ssl
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import httpx

from folia_mgmt.models import Host

DEFAULT_PORT = 8443
DEFAULT_TIMEOUT = 15.0
# Deliberately shorter than DEFAULT_TIMEOUT: ping_host runs once per trusted
# host on every reconcile tick (scheduler.check_host_health), so a single
# dead host must not stall the whole tick for the full 15s.
PING_TIMEOUT = 5.0


class LXDError(RuntimeError):
    pass


def extract_ipv4(instance_state: dict[str, Any]) -> str | None:
    """Pull the first global-scope IPv4 off an instance's /state response."""
    network = instance_state.get("network") or {}
    for iface_name, iface in network.items():
        if iface_name == "lo":
            continue
        for addr in iface.get("addresses", []):
            if addr.get("family") == "inet" and addr.get("scope") == "global":
                return addr.get("address")
    return None


def fetch_server_cert_pem(address: str, timeout: float = 10.0) -> str:
    """TLS-connects to an LXD host without verification, just to read its
    leaf certificate — the first half of trust-on-first-use pinning."""
    host, _, port_s = address.partition(":")
    port = int(port_s) if port_s else DEFAULT_PORT
    ctx = ssl._create_unverified_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if der is None:
        raise LXDError(f"no certificate presented by {address}")
    return ssl.DER_cert_to_PEM_cert(der)


def cert_fingerprint(pem: str) -> str:
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).hexdigest()


def _pinned_context(server_pem: str, client_cert: Path, client_key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=server_pem)
    ctx.load_cert_chain(certfile=str(client_cert), keyfile=str(client_key))
    return ctx


class LXDClient:
    def __init__(self, client_cert: Path, client_key: Path, timeout: float = DEFAULT_TIMEOUT):
        self._client_cert = client_cert
        self._client_key = client_key
        self._timeout = timeout

    # -- trust bootstrap -----------------------------------------------------

    def redeem_trust_token(self, address: str, project: str, trust_token: str) -> tuple[str, str]:
        """Consume a single-use LXD trust token, completing the mTLS handshake
        from the client side. Returns (fingerprint, server_cert_pem).

        Mirrors what `lxc remote add <name> <addr> --token <token>` does.
        """
        server_pem = fetch_server_cert_pem(address, timeout=self._timeout)
        ctx = _pinned_context(server_pem, self._client_cert, self._client_key)

        with httpx.Client(base_url=f"https://{address}", verify=ctx, timeout=self._timeout) as client:
            resp = client.post(
                "/1.0/certificates",
                json={"type": "client", "trust_token": trust_token, "name": "folia-nexa-mgmt"},
            )
            if resp.status_code not in (200, 201):
                raise LXDError(
                    f"trust token redemption failed against {address}: "
                    f"HTTP {resp.status_code}: {resp.text}"
                )

        return cert_fingerprint(server_pem), server_pem

    # -- per-host client -------------------------------------------------------

    @contextmanager
    def _client_for(self, host: Host) -> Iterator[httpx.Client]:
        # Every method below raises LXDError itself for *application*-level
        # failures (LXD's API answering with a non-2xx status) — but a host
        # that's down, unreachable, or mid-TLS-handshake-timeout fails at
        # the *transport* level instead (httpx.HTTPError/httpcore, or a
        # bare socket/SSL error), which none of those call sites catch.
        # Translating that here, once, at the single choke point every
        # method already goes through, means one dead host degrades to an
        # LXDError like any other expected failure — callers throughout
        # scheduler.py are already written to catch exactly that — instead
        # of an uncaught exception escaping this class entirely. (This bit
        # a live deployment: recover_crashed_worlds hit an unrelated
        # unreachable host's ConnectTimeout uncaught, which aborted that
        # whole reconcile() tick before it ever reached sync_stats_configs,
        # stalling FoliaNexaStats token provisioning cluster-wide.)
        if not host.server_cert_pem:
            raise LXDError(f"host '{host.name}' has no pinned certificate — re-enroll it")
        ctx = _pinned_context(host.server_cert_pem, self._client_cert, self._client_key)
        with httpx.Client(base_url=f"https://{host.address}", verify=ctx, timeout=self._timeout) as client:
            try:
                yield client
            except (httpx.HTTPError, ssl.SSLError, OSError) as exc:
                raise LXDError(f"could not reach host '{host.name}': {exc}") from exc

    def _wait_operation(self, client: httpx.Client, op_body: dict) -> dict:
        op_url = op_body.get("operation")
        if not op_url:
            return op_body
        resp = client.get(f"{op_url}/wait")
        resp.raise_for_status()
        result = resp.json()
        metadata = result.get("metadata") or {}
        if metadata.get("status") == "Failure":
            raise LXDError(f"LXD operation failed: {metadata.get('err')}")
        return metadata

    # -- reachability --------------------------------------------------------

    def ping_host(self, host: Host) -> bool:
        """Lightweight liveness check for the periodic host health-check
        (scheduler.check_host_health) — GET /1.0 is LXD's own server-info
        endpoint, unauthenticated-content-wise but still mTLS-gated the same
        as every other call, and doesn't need a `project` param the way
        instance calls do. Any failure (refused connection, TLS handshake
        timeout, DNS failure because the box is off) just means "not
        reachable" — this never raises, unlike the rest of this class,
        since a health check that itself needs try/except at every call
        site defeats the point.
        """
        if not host.server_cert_pem:
            return False
        try:
            ctx = _pinned_context(host.server_cert_pem, self._client_cert, self._client_key)
            with httpx.Client(base_url=f"https://{host.address}", verify=ctx, timeout=PING_TIMEOUT) as client:
                resp = client.get("/1.0")
                return resp.status_code == 200
        except (httpx.HTTPError, ssl.SSLError, OSError):
            return False

    # -- instances ---------------------------------------------------------

    def list_instances(self, host: Host) -> list[dict[str, Any]]:
        with self._client_for(host) as client:
            resp = client.get("/1.0/instances", params={"project": host.project, "recursion": 1})
            resp.raise_for_status()
            return resp.json().get("metadata", [])

    def get_instance_state(self, host: Host, name: str) -> dict[str, Any]:
        with self._client_for(host) as client:
            resp = client.get(f"/1.0/instances/{name}/state", params={"project": host.project})
            resp.raise_for_status()
            return resp.json().get("metadata", {})

    def launch_container(
        self,
        host: Host,
        name: str,
        image_alias: str,
        *,
        profiles: list[str] | None = None,
        cpu_cores: int,
        memory_gb: int,
        config: dict[str, str] | None = None,
        snapshot_schedule: str | None = None,
        snapshot_expiry: str | None = None,
    ) -> dict[str, Any]:
        instance_config: dict[str, str] = {
            "limits.cpu": str(cpu_cores),
            "limits.memory": f"{memory_gb}GB",
            **(config or {}),
        }
        if snapshot_schedule:
            instance_config["snapshots.schedule"] = snapshot_schedule
        if snapshot_expiry:
            instance_config["snapshots.expiry"] = snapshot_expiry

        body = {
            "name": name,
            "source": {"type": "image", "alias": image_alias},
            "profiles": profiles or ["default"],
            "config": instance_config,
        }
        with self._client_for(host) as client:
            resp = client.post("/1.0/instances", params={"project": host.project}, json=body)
            if resp.status_code not in (200, 201, 202):
                raise LXDError(f"launch of '{name}' on '{host.name}' failed: {resp.text}")
            metadata = self._wait_operation(client, resp.json())

            start = client.put(
                f"/1.0/instances/{name}/state",
                params={"project": host.project},
                json={"action": "start", "timeout": 30},
            )
            if start.status_code in (200, 202):
                self._wait_operation(client, start.json())
            return metadata

    def update_config(self, host: Host, name: str, config: dict[str, str]) -> None:
        """Merges `config` into an already-launched instance's `user.*`
        config keys via LXD's PATCH (partial update — unlike PUT, doesn't
        require resending the full instance definition). Used to push new/
        changed `user.folia.*` keys (PLAN.md §9) to a world that was
        already placed before a config change, e.g. adding
        server-properties-manifest-url to a world launched before that key
        existed. Takes effect on the container's next restart — folia-nexa-
        node only reads devlxd config at its own startup, not live."""
        with self._client_for(host) as client:
            resp = client.patch(
                f"/1.0/instances/{name}",
                params={"project": host.project},
                json={"config": config},
            )
            if resp.status_code not in (200, 202):
                raise LXDError(f"config update of '{name}' on '{host.name}' failed: {resp.text}")
            if resp.status_code == 202:
                self._wait_operation(client, resp.json())

    def restart_container(self, host: Host, name: str) -> None:
        # force=false, not true — LXD's own semantics for this flag are
        # "skip the graceful signal-and-wait sequence, kill it now", which
        # for a Minecraft/Folia JVM means no chance for Paper's SIGTERM
        # shutdown hook (world save, clean close) to run before the
        # process is gone. false sends the graceful stop signal first and
        # waits up to `timeout` seconds before forcing — routers/worlds.py
        # additionally issues an RCON `save-all` right before calling
        # this, as a save that doesn't depend on the JVM's signal handling
        # working at all; this flag is the complementary half of that.
        with self._client_for(host) as client:
            resp = client.put(
                f"/1.0/instances/{name}/state",
                params={"project": host.project},
                json={"action": "restart", "timeout": 30, "force": False},
            )
            if resp.status_code not in (200, 202):
                raise LXDError(f"restart of '{name}' on '{host.name}' failed: {resp.text}")
            if resp.status_code == 202:
                self._wait_operation(client, resp.json())

    def stop_container(self, host: Host, name: str) -> None:
        # force=false — see restart_container's comment above, same
        # graceful-shutdown reasoning applies here.
        with self._client_for(host) as client:
            resp = client.put(
                f"/1.0/instances/{name}/state",
                params={"project": host.project},
                json={"action": "stop", "timeout": 30, "force": False},
            )
            if resp.status_code not in (200, 202):
                raise LXDError(f"stop of '{name}' on '{host.name}' failed: {resp.text}")
            if resp.status_code == 202:
                self._wait_operation(client, resp.json())

    def start_container(self, host: Host, name: str) -> None:
        with self._client_for(host) as client:
            resp = client.put(
                f"/1.0/instances/{name}/state",
                params={"project": host.project},
                json={"action": "start", "timeout": 30, "force": True},
            )
            if resp.status_code not in (200, 202):
                raise LXDError(f"start of '{name}' on '{host.name}' failed: {resp.text}")
            if resp.status_code == 202:
                self._wait_operation(client, resp.json())

    def delete_container(self, host: Host, name: str, *, stop_first: bool = True) -> None:
        with self._client_for(host) as client:
            if stop_first:
                stop = client.put(
                    f"/1.0/instances/{name}/state",
                    params={"project": host.project},
                    json={"action": "stop", "timeout": 30, "force": True},
                )
                if stop.status_code in (200, 202):
                    self._wait_operation(client, stop.json())

            resp = client.delete(f"/1.0/instances/{name}", params={"project": host.project})
            if resp.status_code not in (200, 202, 404):
                raise LXDError(f"delete of '{name}' on '{host.name}' failed: {resp.text}")
            if resp.status_code == 202:
                self._wait_operation(client, resp.json())

    def push_file(self, host: Host, name: str, path: str, content: bytes, *, mode: str = "0644") -> None:
        """Writes a file into a running container via LXD's file API — used
        to apply ops.json/whitelist.json changes without an exec round
        trip. PLAN.md §11B."""
        with self._client_for(host) as client:
            resp = client.post(
                f"/1.0/instances/{name}/files",
                params={"project": host.project, "path": path},
                content=content,
                headers={"X-LXD-type": "file", "X-LXD-mode": mode},
            )
            if resp.status_code not in (200, 201):
                raise LXDError(f"failed to push '{path}' to '{name}' on '{host.name}': {resp.text}")

    def list_files(self, host: Host, name: str, path: str) -> list[str]:
        """Lists entries directly under `path` inside the container (not
        recursive — callers walk the tree themselves, see
        plugin_files.py). Raises LXDError if `path` doesn't exist or isn't
        a directory."""
        with self._client_for(host) as client:
            resp = client.get(f"/1.0/instances/{name}/files", params={"project": host.project, "path": path})
            if resp.status_code == 404:
                raise LXDError(f"no such path '{path}' on '{name}' on '{host.name}'")
            if resp.status_code != 200:
                raise LXDError(f"failed to list '{path}' on '{name}' on '{host.name}': {resp.text}")
            if resp.headers.get("X-LXD-type") != "directory":
                raise LXDError(f"'{path}' on '{name}' on '{host.name}' is not a directory")
            return resp.json().get("metadata", [])

    def read_file(self, host: Host, name: str, path: str) -> bytes:
        """Reads a file's raw content back from a running container — the
        read counterpart to push_file. Raises LXDError if `path` doesn't
        exist or isn't a file."""
        with self._client_for(host) as client:
            resp = client.get(f"/1.0/instances/{name}/files", params={"project": host.project, "path": path})
            if resp.status_code == 404:
                raise LXDError(f"no such file '{path}' on '{name}' on '{host.name}'")
            if resp.status_code != 200:
                raise LXDError(f"failed to read '{path}' from '{name}' on '{host.name}': {resp.text}")
            if resp.headers.get("X-LXD-type") != "file":
                raise LXDError(f"'{path}' on '{name}' on '{host.name}' is not a file")
            return resp.content

    def snapshot_container(self, host: Host, name: str, snapshot_name: str) -> None:
        with self._client_for(host) as client:
            resp = client.post(
                f"/1.0/instances/{name}/snapshots",
                params={"project": host.project},
                json={"name": snapshot_name},
            )
            if resp.status_code not in (200, 202):
                raise LXDError(f"snapshot of '{name}' failed: {resp.text}")
            if resp.status_code == 202:
                self._wait_operation(client, resp.json())

    def restore_snapshot(self, host: Host, name: str, snapshot_name: str) -> None:
        with self._client_for(host) as client:
            resp = client.put(
                f"/1.0/instances/{name}",
                params={"project": host.project},
                json={"restore": snapshot_name},
            )
            if resp.status_code not in (200, 202):
                raise LXDError(f"restore of '{name}' to '{snapshot_name}' failed: {resp.text}")
            if resp.status_code == 202:
                self._wait_operation(client, resp.json())

    def export_backup(self, host: Host, name: str) -> bytes:
        """Exports an instance as a backup tarball, for cross-host copy
        (§13). Deliberately uses LXD's plain-HTTP backup export/import API
        rather than its websocket-based live-migration protocol — the
        latter would need a binary protocol implementation this codebase
        has no way to validate without a live LXD daemon; export/import is
        a much smaller surface to get right blind."""
        with self._client_for(host) as client:
            create = client.post(
                f"/1.0/instances/{name}/backups",
                params={"project": host.project},
                json={"name": "", "instance_only": False, "optimized_storage": False},
            )
            if create.status_code not in (200, 202):
                raise LXDError(f"failed to start backup of '{name}' on '{host.name}': {create.text}")
            metadata = self._wait_operation(client, create.json())

            backup_path = (metadata.get("resources") or {}).get("backups", [None])[0]
            if not backup_path:
                raise LXDError(f"backup of '{name}' on '{host.name}' returned no backup resource")

            export = client.get(f"{backup_path}/export", params={"project": host.project})
            if export.status_code != 200:
                raise LXDError(f"failed to download backup export for '{name}': {export.text}")
            content = export.content

            client.delete(backup_path, params={"project": host.project})
            return content

    def import_backup(self, host: Host, backup_content: bytes) -> None:
        """The imported instance's name comes from inside the backup
        tarball's metadata (it's whatever name it was exported under) —
        LXD's import endpoint doesn't take a rename parameter, so callers
        that need `target_name != name` must export under the desired
        target name in the first place."""
        with self._client_for(host) as client:
            resp = client.post(
                "/1.0/instances",
                params={"project": host.project},
                content=backup_content,
                headers={"Content-Type": "application/octet-stream"},
            )
            if resp.status_code not in (200, 202):
                raise LXDError(f"failed to import backup on '{host.name}': {resp.text}")
            self._wait_operation(client, resp.json())

    def migrate_container(self, source_host: Host, source_name: str, target_host: Host) -> None:
        """Stop -> export -> import -> start. Not a live/zero-downtime
        migration — PLAN.md §13 already documents migration as requiring a
        brief restart, which this matches. Caller is responsible for
        deleting the source container once satisfied the target is
        healthy (PLAN.md §13's "delete original once healthy").

        NOT exercised against a live LXD daemon — written against LXD's
        documented backup export/import API contract.
        """
        with self._client_for(source_host) as client:
            stop = client.put(
                f"/1.0/instances/{source_name}/state",
                params={"project": source_host.project},
                json={"action": "stop", "timeout": 30, "force": True},
            )
            if stop.status_code in (200, 202):
                self._wait_operation(client, stop.json())

        backup = self.export_backup(source_host, source_name)
        self.import_backup(target_host, backup)

        with self._client_for(target_host) as client:
            start = client.put(
                f"/1.0/instances/{source_name}/state",
                params={"project": target_host.project},
                json={"action": "start", "timeout": 30},
            )
            if start.status_code in (200, 202):
                self._wait_operation(client, start.json())
