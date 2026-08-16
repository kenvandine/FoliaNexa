# VPS edge deployment artifacts

Supporting files for the WireGuard tunnel + Caddy edge described in
[`docs/vps-edge-deployment.md`](../../docs/vps-edge-deployment.md) and
PLAN.md §7A. Nothing here runs automatically — each file is meant to be
copied to the relevant box (the Linode VPS, or the home LXD host) and
adapted, the same way `tools/folia-host-join.sh` works today.

| File | Runs on | What it's for |
| --- | --- | --- |
| `setup-wireguard.sh` | both | Generates this peer's WireGuard keypair and a filled-in `wg0.conf`. Run once per home LXD host on the `home` side; on the VPS, run once per home host (`--peer-name`) to build a multi-peer mesh — each host's `[Peer]` stanza lives under `peers.d/` next to `wg0.conf` so adding one never disturbs another. |
| `wg-vps.conf.template` | VPS | Reference: the shape of a single `[Peer]` block `setup-wireguard.sh --role vps` produces (the real file has one such block per home host in the mesh), for manual review/editing. |
| `wg-home.conf.template` | home LXD host | Same, for `--role home`. |
| `Caddyfile` | VPS | Terminates public TLS and `import`s every cluster's site blocks from `clusters.d/*.caddy` — one cluster's `admin.<domain>` / `api.<domain>` / `play.<domain>`, each reverse-proxying to that cluster's own mgmt over its own tunnel peer. |
| `add-cluster.sh` | VPS | (Re)generates one cluster's `clusters.d/<id>.caddy` — run once per independent cluster this proxy serves (PLAN.md §7D). Re-running for the same `--id` only touches that one file. |
| `clusters.d/` | VPS | Generated output of `add-cluster.sh`, imported by `Caddyfile`. Not checked into git (real operator domains/tunnel addresses) — `clusters.d/README.md` is the only tracked file. |
| `deploy-portal.sh` | VPS (or wherever you push from) | Syncs `portal/`'s static files to a cluster's Caddy `file_server` root (`--remote-root`, one call per cluster if you're running more than one). `portal/` has no build step and no `snapcraft.yaml` — this is the entire deploy story for it. |

None of this has been run against a real Linode VPS or a real home
network in this environment — see `docs/vps-edge-deployment.md`'s own
"what's real vs. unverified" section for exactly what's config-syntax-
checked here versus what still needs validating against your real
infrastructure.
