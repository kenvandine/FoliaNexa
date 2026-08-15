# VPS edge deployment artifacts

Supporting files for the WireGuard tunnel + Caddy edge described in
[`docs/vps-edge-deployment.md`](../../docs/vps-edge-deployment.md) and
PLAN.md §7A. Nothing here runs automatically — each file is meant to be
copied to the relevant box (the Linode VPS, or the home LXD host) and
adapted, the same way `tools/folia-host-join.sh` works today.

| File | Runs on | What it's for |
| --- | --- | --- |
| `setup-wireguard.sh` | both | Generates this peer's WireGuard keypair and a filled-in `wg0.conf`. Run once per peer. |
| `wg-vps.conf.template` | VPS | Reference: what `setup-wireguard.sh --role vps` produces, for manual review/editing. |
| `wg-home.conf.template` | home LXD host | Same, for `--role home`. |
| `Caddyfile` | VPS | Terminates public TLS for `admin.<domain>` / `api.<domain>` / `play.<domain>` and reverse-proxies to mgmt over the tunnel. |
| `deploy-portal.sh` | VPS (or wherever you push from) | Syncs `portal/`'s static files to the VPS's Caddy `file_server` root. `portal/` has no build step and no `snapcraft.yaml` — this is the entire deploy story for it. |

None of this has been run against a real Linode VPS or a real home
network in this environment — see `docs/vps-edge-deployment.md`'s own
"what's real vs. unverified" section for exactly what's config-syntax-
checked here versus what still needs validating against your real
infrastructure.
