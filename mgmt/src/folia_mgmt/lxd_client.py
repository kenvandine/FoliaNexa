"""Thin wrapper over the LXD remote (HTTPS) API. PLAN.md §3, §4, §5.

mgmt never touches a local LXD unix socket — every host, including one
colocated on the same machine, is reached the same way: mTLS over HTTPS,
with the peer's leaf certificate pinned by fingerprint at enrollment time
(TOFU) rather than verified against a CA, since LXD hosts use self-signed
certs by default.

NOTE: this was written against the documented LXD REST API contract
(trust-token bootstrap via POST /1.0/certificates, instance CRUD under
/1.0/instances, async operations under /1.0/operations) and, apart from
snapshot_container (confirmed live 2026-08-24 — see LONG_OPERATION_TIMEOUT
and its per-container lock below, plus CLAUDE.md's World backups entry),
has not been exercised against a live LXD daemon in this environment.
Treat the exact request/response shapes of every other method as a first
draft to validate against a real host.
"""

from __future__ import annotations

import hashlib
import socket
import ssl
import threading
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
# Deliberately much longer than DEFAULT_TIMEOUT, and passed explicitly
# only to the handful of calls below that actually need it
# (snapshot_container/restore_snapshot) rather than being _wait_operation's
# own default: a snapshot/restore on a storage pool with no native COW
# support (LXD's "dir" driver) is a real, potentially large rootfs copy
# rather than an instant atomic operation, and DEFAULT_TIMEOUT is sized
# for ordinary fast CRUD calls like restart/stop/start. Confirmed live
# (2026-08-24): a manual world backup on a dir-backend host genuinely took
# longer than 15s, so this GET timed out client-side while the snapshot
# was still legitimately running on LXD's side; mgmt reported that as a
# failure, the operator retried, and three separate snapshot operations
# ended up queued against the very same container — which wedged it in
# FREEZING (LXD's dir driver has to freeze the container for the duration
# of the copy, and doesn't tolerate that happening more than once
# concurrently for the same instance). The per-container lock in
# snapshot_container below is the other half of this fix — it stops mgmt
# from ever issuing that second concurrent request in the first place,
# regardless of how long the first one takes.
#
# _wait_operation/_finish previously applied this 600s timeout to every
# async LXD call (restart/stop/start/delete/launch/migrate too, via the
# same shared helper) rather than just the two calls above — since
# reconcile() runs its steps synchronously in one call, a single merely-
# slow-not-dead host could stall the entire reconcile tick (whitelist
# sync, LuckPerms sync, backups, teardown, everything) for up to 10
# minutes, exactly the "single dead host must not stall the whole tick"
# failure PING_TIMEOUT's own comment above says to avoid. Fixed by making
# DEFAULT_TIMEOUT _wait_operation/_finish's default and opting into this
# long timeout only where it's actually needed.
LONG_OPERATION_TIMEOUT = 600.0
# Wider than DEFAULT_TIMEOUT but not as wide as LONG_OPERATION_TIMEOUT —
# for the one restart_container call issued outside a batched reconcile
# tick (routers/worlds.py's restore_backup). LXD's own request body
# already asks for up to a 30s graceful-stop window (restart_container's
# "timeout": 30 below), so a client-side wait timeout smaller than that
# (DEFAULT_TIMEOUT's 15s) can misreport a restart that's still genuinely
# completing server-side as failed — during a restore, that means
# deleting the just-armed pending-restore marker and losing the restore
# entirely, not just the restart. Reconcile-loop callers (e.g.
# recover_crashed_worlds) keep plain DEFAULT_TIMEOUT, since those iterate
# every world in one tick and a single slow-not-dead host must not stall
# the others.
RESTART_WAIT_TIMEOUT = 45.0
# Storage-pool drivers confirmed to have native copy-on-write snapshot
# support — see _refuse_dir_backend's docstring for why this is an
# allowlist rather than a "dir"-only blocklist.
ALLOWED_SNAPSHOT_DRIVERS = frozenset({"zfs", "btrfs"})


class LXDError(RuntimeError):
    pass


class RestoreInProgressError(LXDError):
    """Raised by restore_guard when a restore of the same (host,
    container) is already in flight — a distinct subclass so callers
    (routers/worlds.py's restore_backup) can map this specific rejection
    to a 409 Conflict instead of the 502 Bad Gateway every other LXDError
    from a real backend failure gets."""


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
        # Guards against two overlapping snapshot_container calls for the
        # same (host, container) — see LONG_OPERATION_TIMEOUT's comment
        # above for the live incident this closes the other half of.
        self._snapshot_lock = threading.Lock()
        self._snapshots_in_flight: set[tuple[str, str]] = set()
        # Same protection, for restore_guard below — a double-clicked
        # dashboard Restore button, or two admins restoring the same
        # world at once, would otherwise race two push_file writes to
        # the same .pending-restore.tar.gz marker and two restart_container
        # calls against the same container, unserialized.
        self._restore_lock = threading.Lock()
        self._restores_in_flight: set[tuple[str, str]] = set()

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

    def _wait_operation(
        self, client: httpx.Client, op_body: dict, *, timeout: float = DEFAULT_TIMEOUT
    ) -> dict:
        op_url = op_body.get("operation")
        if not op_url:
            return op_body
        resp = client.get(f"{op_url}/wait", timeout=timeout)
        resp.raise_for_status()
        result = resp.json()
        metadata = result.get("metadata") or {}
        if metadata.get("status") == "Failure":
            raise LXDError(f"LXD operation failed: {metadata.get('err')}")
        return metadata

    def _finish(
        self,
        client: httpx.Client,
        resp: httpx.Response,
        *,
        ok_codes: tuple[int, ...],
        error: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Shared tail end of every synchronous-or-async LXD call in this
        class: LXD answers either immediately (200/201) or with a
        background operation to poll (202) — everything else is a real
        failure. Centralizing this once means a future change to that
        convention (e.g. surfacing LXD's structured error `code`/`err`
        field) only needs to happen here, not in every method below.
        `timeout` only affects the 202 operation-wait poll — pass
        LONG_OPERATION_TIMEOUT explicitly for calls that can involve a
        real full-rootfs copy (see that constant's own comment); every
        other call gets DEFAULT_TIMEOUT so a slow-not-dead host fails
        fast instead of stalling the whole reconcile tick."""
        if resp.status_code not in ok_codes:
            raise LXDError(f"{error}: {resp.text}")
        if resp.status_code == 202:
            self._wait_operation(client, resp.json(), timeout=timeout)

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
            self._finish(client, resp, ok_codes=(200, 202), error=f"config update of '{name}' on '{host.name}' failed")

    def restart_container(self, host: Host, name: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        # force=false, not true — LXD's own semantics for this flag are
        # "skip the graceful signal-and-wait sequence, kill it now", which
        # for a Minecraft/Folia JVM means no chance for Paper's SIGTERM
        # shutdown hook (world save, clean close) to run before the
        # process is gone. false sends the graceful stop signal first and
        # waits up to `timeout` seconds before forcing — routers/worlds.py
        # additionally issues an RCON `save-all` right before calling
        # this, as a save that doesn't depend on the JVM's signal handling
        # working at all; this flag is the complementary half of that.
        #
        # `timeout` here is our own client-side operation-wait budget
        # (passed through to _finish), independent of the "timeout": 30
        # in the request body below (LXD's own graceful-stop deadline) —
        # see RESTART_WAIT_TIMEOUT's comment for why a caller outside a
        # batched reconcile tick should widen it.
        with self._client_for(host) as client:
            resp = client.put(
                f"/1.0/instances/{name}/state",
                params={"project": host.project},
                json={"action": "restart", "timeout": 30, "force": False},
            )
            self._finish(
                client, resp, ok_codes=(200, 202), error=f"restart of '{name}' on '{host.name}' failed",
                timeout=timeout,
            )

    def stop_container(self, host: Host, name: str) -> None:
        # force=false — see restart_container's comment above, same
        # graceful-shutdown reasoning applies here.
        with self._client_for(host) as client:
            resp = client.put(
                f"/1.0/instances/{name}/state",
                params={"project": host.project},
                json={"action": "stop", "timeout": 30, "force": False},
            )
            self._finish(client, resp, ok_codes=(200, 202), error=f"stop of '{name}' on '{host.name}' failed")

    def start_container(self, host: Host, name: str) -> None:
        with self._client_for(host) as client:
            resp = client.put(
                f"/1.0/instances/{name}/state",
                params={"project": host.project},
                json={"action": "start", "timeout": 30, "force": True},
            )
            self._finish(client, resp, ok_codes=(200, 202), error=f"start of '{name}' on '{host.name}' failed")

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
            self._finish(client, resp, ok_codes=(200, 202, 404), error=f"delete of '{name}' on '{host.name}' failed")

    @contextmanager
    def restore_guard(self, host: Host, name: str) -> Iterator[None]:
        """Raises LXDError immediately, without touching the network, if a
        restore of this exact (host, container) is already in progress —
        mirrors snapshot_container's own per-container lock (see
        LONG_OPERATION_TIMEOUT's comment) for the identical class of
        hazard: a double-clicked dashboard Restore button, or two admins
        restoring the same world concurrently, racing two push_file
        writes to the same pending-restore marker and two
        restart_container calls against the same container, with nothing
        upstream serializing them. Wrap the whole push_file +
        restart_container sequence in `with lxd_client.restore_guard(...)`."""
        key = (host.name, name)
        with self._restore_lock:
            if key in self._restores_in_flight:
                raise RestoreInProgressError(f"a restore of '{name}' on '{host.name}' is already in progress")
            self._restores_in_flight.add(key)
        try:
            yield
        finally:
            with self._restore_lock:
                self._restores_in_flight.discard(key)

    def push_file(
        self,
        host: Host,
        name: str,
        path: str,
        content: bytes | Iterable[bytes],
        *,
        mode: str = "0644",
        timeout: float | None = None,
    ) -> None:
        """Writes a file into a running container via LXD's file API — used
        to apply ops.json/whitelist.json changes without an exec round
        trip. PLAN.md §11B. `content` may also be a bytes iterator/
        generator (httpx streams it with chunked transfer-encoding rather
        than buffering it all up front) — used by routers/worlds.py's
        restore_backup so a multi-GB backup tarball never has to sit
        fully in mgmt's own memory before the request even starts.

        `timeout` overrides this client's own DEFAULT_TIMEOUT for just
        this request — left unset (None) for ordinary small-file pushes;
        pass LONG_OPERATION_TIMEOUT explicitly for a call that may need
        to move a large amount of data, same convention as
        LONG_OPERATION_TIMEOUT's own comment describes for snapshot/
        restore."""
        with self._client_for(host) as client:
            kwargs: dict[str, Any] = {}
            if timeout is not None:
                kwargs["timeout"] = timeout
            resp = client.post(
                f"/1.0/instances/{name}/files",
                params={"project": host.project, "path": path},
                content=content,
                headers={"X-LXD-type": "file", "X-LXD-mode": mode},
                **kwargs,
            )
            if resp.status_code not in (200, 201):
                raise LXDError(f"failed to push '{path}' to '{name}' on '{host.name}': {resp.text}")

    def delete_file(self, host: Host, name: str, path: str) -> None:
        """Deletes a single file inside a running container — the
        cleanup counterpart to push_file, used by routers/worlds.py's
        restore_backup to remove an armed-but-unconsumed pending-restore
        marker if the restart that was supposed to consume it fails. 404
        is treated as success (nothing to clean up)."""
        with self._client_for(host) as client:
            resp = client.delete(f"/1.0/instances/{name}/files", params={"project": host.project, "path": path})
            if resp.status_code not in (200, 202, 404):
                raise LXDError(f"failed to delete '{path}' on '{name}' on '{host.name}': {resp.text}")

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

    def get_storage_driver_for_instance(self, host: Host, name: str) -> str:
        """Resolves which storage-pool driver backs this instance's root
        disk (e.g. "zfs", "dir", "btrfs") — used to unconditionally
        refuse snapshot_container/restore_snapshot on "dir", which has no
        native copy-on-write support and freezes the whole container for
        the duration of a full rootfs copy (see LONG_OPERATION_TIMEOUT's
        comment for the live incident this caused). Storage pools aren't
        project-scoped in LXD, unlike everything else in this class."""
        with self._client_for(host) as client:
            resp = client.get(f"/1.0/instances/{name}", params={"project": host.project})
            if resp.status_code != 200:
                raise LXDError(f"failed to read instance '{name}' on '{host.name}': {resp.text}")
            expanded_devices = resp.json().get("metadata", {}).get("expanded_devices", {})
            pool = (expanded_devices.get("root") or {}).get("pool")
            if not pool:
                raise LXDError(f"instance '{name}' on '{host.name}' has no root disk device")

            pool_resp = client.get(f"/1.0/storage-pools/{pool}")
            if pool_resp.status_code != 200:
                raise LXDError(f"failed to read storage pool '{pool}' on '{host.name}': {pool_resp.text}")
            return pool_resp.json().get("metadata", {}).get("driver", "")

    def _refuse_dir_backend(self, host: Host, name: str, action: str) -> None:
        """Allowlists drivers actually confirmed to have native
        copy-on-write snapshot support, rather than blocklisting the one
        driver ("dir") known to freeze the whole container — a
        blocklist lets any *other* unrecognized/future driver (an lvm
        pool without thin provisioning, a renamed/new LXD driver string)
        sail through unrefused if lxd_snapshot_backups_enabled is ever
        turned back on, on a backend that was never actually diagnosed
        as safe. See CLAUDE.md's World backups entry for the "dir"
        incident this refusal exists to prevent from recurring."""
        driver = self.get_storage_driver_for_instance(host, name)
        if driver not in ALLOWED_SNAPSHOT_DRIVERS:
            raise LXDError(
                f"refusing to {action} '{name}' on '{host.name}': its storage pool uses the "
                f"'{driver}' driver, which isn't confirmed to have native copy-on-write "
                "snapshot support — a 'dir' pool freezes the whole container for the duration "
                "of a full rootfs copy (see CLAUDE.md's World backups entry), and any other "
                "unverified driver is refused the same way rather than assumed safe. Migrate "
                "that host's storage pool to zfs or btrfs first (tools/migrate-storage-to-zfs.sh), "
                "or use the file-level world backup feature instead, which doesn't depend on the "
                "storage pool at all."
            )

    def snapshot_container(self, host: Host, name: str, snapshot_name: str) -> None:
        """Raises LXDError immediately, without ever reaching the network,
        if a snapshot of this exact (host, container) is already in
        flight — both the hourly scheduler and the manual-backup endpoint
        call this, and nothing upstream of here serializes them against
        each other. See LONG_OPERATION_TIMEOUT's comment above for why
        that matters: on a storage backend with no native snapshot
        support (LXD's "dir" driver), a snapshot freezes the container
        for the duration of a real rootfs copy, and LXD doesn't tolerate
        a second concurrent snapshot request for the same instance while
        that's in progress — confirmed live, it wedges the container in
        FREEZING rather than queueing cleanly. Also unconditionally
        refuses a "dir"-backed instance outright (_refuse_dir_backend) —
        this check is independent of Settings.lxd_snapshot_backups_enabled
        (a router-layer feature toggle) and can't be bypassed by any
        caller, present or future."""
        self._refuse_dir_backend(host, name, "snapshot")
        key = (host.name, name)
        with self._snapshot_lock:
            if key in self._snapshots_in_flight:
                raise LXDError(f"a snapshot of '{name}' on '{host.name}' is already in progress")
            self._snapshots_in_flight.add(key)
        try:
            with self._client_for(host) as client:
                resp = client.post(
                    f"/1.0/instances/{name}/snapshots",
                    params={"project": host.project},
                    json={"name": snapshot_name},
                )
                self._finish(
                    client,
                    resp,
                    ok_codes=(200, 202),
                    error=f"snapshot of '{name}' failed",
                    timeout=LONG_OPERATION_TIMEOUT,
                )
        finally:
            with self._snapshot_lock:
                self._snapshots_in_flight.discard(key)

    def restore_snapshot(self, host: Host, name: str, snapshot_name: str) -> None:
        """See snapshot_container's docstring — the same unconditional
        "dir" refusal applies here (restoring on a non-COW backend is the
        same kind of full-copy, freeze-prone operation as taking one)."""
        self._refuse_dir_backend(host, name, "restore")
        with self._client_for(host) as client:
            resp = client.put(
                f"/1.0/instances/{name}",
                params={"project": host.project},
                json={"restore": snapshot_name},
            )
            self._finish(
                client,
                resp,
                ok_codes=(200, 202),
                error=f"restore of '{name}' to '{snapshot_name}' failed",
                timeout=LONG_OPERATION_TIMEOUT,
            )

    def delete_snapshot(self, host: Host, name: str, snapshot_name: str) -> None:
        """Deletes one instance snapshot — the retention-pruning
        counterpart to snapshot_container. 404 is treated as success (the
        snapshot's already gone, e.g. a previous prune attempt deleted it
        but failed to commit its own DB row removal)."""
        with self._client_for(host) as client:
            resp = client.delete(
                f"/1.0/instances/{name}/snapshots/{snapshot_name}", params={"project": host.project}
            )
            self._finish(
                client,
                resp,
                ok_codes=(200, 202, 404),
                error=f"delete of snapshot '{snapshot_name}' for '{name}' on '{host.name}' failed",
            )

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
