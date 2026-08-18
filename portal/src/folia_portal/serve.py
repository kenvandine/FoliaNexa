"""Static file server for the FoliaNexa player-hub portal (portal/*.html,
portal.css, portal.js) — packaged as the `folia-nexa-portal` snap so this
component deploys the same way as every other piece of the cluster
(`snap install`) instead of the old `deploy/vps/deploy-portal.sh` rsync
(PLAN.md §7A). Caddy on the VPS still terminates TLS and reverse-proxies
`play.<domain>` to this daemon's local port — see `deploy/vps/Caddyfile`.

No framework, no dependencies — `http.server.ThreadingHTTPServer` plus the
stdlib's own `SimpleHTTPRequestHandler` is plenty for a handful of static
files with no server-side logic, and keeps this snap's build as simple as
the portal itself (no Node.js/build-tool precedent anywhere in this repo,
see `portal/README.md`).
"""

from __future__ import annotations

import functools
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


def build_server(host: str, port: int, static_root: str) -> ThreadingHTTPServer:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=static_root)
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    snap_dir = os.environ.get("SNAP")
    if not snap_dir:
        raise SystemExit("SNAP is not set — this is meant to run inside the folia-nexa-portal snap")
    static_root = os.path.join(snap_dir, "portal")

    # Bound to localhost by default: Caddy (or whatever's reverse-proxying
    # play.<domain>) is expected to run on the same box and be the only
    # thing that ever talks to this port directly — see
    # FOLIA_PORTAL_LISTEN_HOST's own comment in snapcraft.yaml/
    # scripts/run-portal-daemon.sh for how an operator would widen this.
    host = os.environ.get("FOLIA_PORTAL_LISTEN_HOST", "127.0.0.1")
    port = int(os.environ.get("FOLIA_PORTAL_LISTEN_PORT", "8090"))

    server = build_server(host, port, static_root)
    server.serve_forever()


if __name__ == "__main__":
    main()
