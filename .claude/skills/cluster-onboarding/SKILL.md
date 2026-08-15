---
name: cluster-onboarding
description: Interactive onboarding runbook for standing up FoliaNexa's public-facing infrastructure — a VPS edge (WireGuard tunnel, Caddy TLS, portal), its DNS records, and trusting one or more LXD hosts (e.g. 3 machines) into the cluster. Use when the operator says things like "help me set up my VPS", "configure DNS", "onboard my LXD servers", "bootstrap the cluster infra", or wants to walk through CLAUDE.md's Phase 0/3/9 or docs/vps-edge-deployment.md interactively instead of reading the docs cold. Drives real commands on whichever machine the session is running on (VPS, an LXD host, or a jump box with SSH to both), tracks progress across machines/sessions in a small local checklist file, and never invents infrastructure details (domains, IPs, tokens) — always asks.
---

# Cluster onboarding

This skill operationalizes `CLAUDE.md`'s bootstrap phases (0, 3, and 9)
and `docs/vps-edge-deployment.md` into a single interactive runbook
covering three things:

1. **The VPS edge** — WireGuard tunnel, Caddy TLS, the static portal.
2. **DNS** — the records that need to point at it.
3. **LXD hosts** — trusting one or more machines (typically 3) as
   compute for `folia-nexa-mgmt` via `tools/folia-host-join.sh`.

It doesn't reinvent any mechanics — every command below is exactly what
`CLAUDE.md` / `docs/vps-edge-deployment.md` already document. What this
skill adds: asking for the topology once, tracking which machine/phase
is done across multiple sessions (the operator will likely run Claude
Code separately on the VPS and on each LXD host), and running real
commands on whichever machine the *current* session actually has shell
access to — rather than making the operator copy/paste everything by
hand with no memory of what's already done.

## 0. Load or start the checklist

State lives at `~/.config/folia-nexa-mgmt/onboarding-checklist.yaml` —
the same directory the CLI already uses for `cli.json`. Read it first if
it exists (this session, or a prior one on a different machine, may have
already made progress). If it doesn't exist, gather this once — ask
directly, or use `AskUserQuestion` for the parts with a small enumerable
answer set (e.g. "how many LXD hosts?") — and write it:

```yaml
domain: example.com
vps:
  public_ip: 203.0.113.10
mgmt:
  home_address: 10.0.1.10     # mgmt's real LAN address at home (CLAUDE.md Phase 1-2)
lxd_hosts:
  - name: host1
    address: 10.0.1.21         # this host's own LAN address, NOT a WireGuard overlay address
    is_wireguard_home_peer: true   # exactly one host is this, per §4.1
    labels: {}                 # e.g. {cpu_type: p-core} — matches configs/worlds/*.sh --labels
  - name: host2
    address: 10.0.1.22
    is_wireguard_home_peer: false
    labels: {}
  - name: host3
    address: 10.0.1.23
    is_wireguard_home_peer: false
    labels: {}
phases:
  vps_packages: false
  wireguard_vps: false
  wireguard_home: false
  dns_records: false
  caddy: false
  portal: false
  proxy_relocated: false
  lxd_hosts_joined: []          # subset of lxd_hosts[].name, appended as each completes
```

Don't invent `domain` / `public_ip` / host addresses / mgmt's address —
these are real infrastructure only the operator knows. If something
isn't provisioned yet (e.g. no VPS IP because it hasn't been created),
do the phases that don't depend on it and come back once it exists.

## 1. Figure out what this session can actually reach

Run `hostname` and `ip -4 addr` (or just ask — cheaper than guessing)
and match against `vps.public_ip` / each `lxd_hosts[].address`:

- Matches the VPS → drive §2 directly on this machine.
- Matches one of `lxd_hosts` → drive §4 for that host directly.
- Matches neither (a laptop, a jump box, a Claude Code on the web
  session with no shell on either target) → still gather info and
  update the checklist, but hand the operator exact copy-pasteable
  command blocks instead of pretending to run them, and say plainly
  which physical machine each block belongs on.

Never SSH to a target on the operator's behalf unless they've explicitly
said this session has SSH access and given you the address/credentials
to use for it — don't assume a jump-host setup that was never stated.

## 2. VPS phase (CLAUDE.md Phase 9 / `docs/vps-edge-deployment.md` Phases 1, 3, 5, 6)

Work through in order, flipping the matching `phases.*` key once each is
*verified*, not just attempted:

1. **Packages** (`vps_packages`) — `apt install wireguard-tools caddy`,
   `ufw allow 51820/udp 80/tcp 443/tcp`.
2. **WireGuard, VPS side** (`wireguard_vps`) — this is a two-pass key
   exchange with whichever LXD host is the home peer (§4.1), so it may
   span two sessions/visits:
   - Pass 1: `deploy/vps/setup-wireguard.sh --role vps` (no
     `--peer-public-key` yet) — prints this side's pubkey. Hand that to
     whoever/whatever is doing §4.1 on the home side.
   - Pass 2, once the home side's pubkey exists: re-run with
     `--peer-public-key <home-pubkey>`, then on both ends
     `systemctl enable --now wg-quick@wg0`.
   - Verify: `wg show` shows a recent handshake on both sides,
     `ping 10.66.0.1` reaches home from the VPS (adjust the address if
     the operator customized the overlay subnet).
3. **Scope `AllowedIPs`** — once all 3 LXD hosts' real addresses are
   known (§4), the VPS side's `AllowedIPs` needs mgmt's address and the
   LXD bridge subnet added — it defaults to routing only to the home
   peer's own tunnel address. Edit `/etc/wireguard/wg0.conf` and
   `wg syncconf`, or re-run the setup script with `--allowed-ips`. Do
   **not** widen it to the whole home LAN, and never add the LXD hosts'
   own `:8443` remote-API subnet — mgmt is the only thing that should
   ever reach that (PLAN.md).
4. **Caddy** (`caddy`) — fill in `deploy/vps/Caddyfile`'s
   `ADMIN_DOMAIN` / `API_DOMAIN` / `PLAY_DOMAIN` / `MGMT_UPSTREAM` /
   `ACME_EMAIL` from the checklist's `domain` and a tunnel-reachable
   mgmt address (`10.66.0.2`, or `mgmt.home_address` routed through the
   tunnel once §2 step 3 is done), then:
   ```bash
   caddy validate --config Caddyfile --adapter caddyfile
   sudo cp Caddyfile /etc/caddy/Caddyfile
   sudo systemctl reload caddy
   ```
   Verify with `curl -I https://admin.<domain>/healthz` and
   `curl https://api.<domain>/api/v1/public/players` — but only after
   DNS (§3) is actually live; if the operator hits this out of order,
   say so rather than treating a failed curl as a Caddy bug.
5. **Portal** (`portal`) — `./deploy/vps/deploy-portal.sh --vps-host
   root@<vps-ip>`. Verify by fetching `https://play.<domain>/` (once DNS
   is live).
6. **Proxy relocation** (`proxy_relocated`) — CLAUDE.md Phase 7's
   `snapcraft` + `snap install --dangerous` for `folia-nexa-proxy`, run
   *on the VPS* instead of home, with `FOLIA_MGMT_URL` pointed at mgmt's
   tunnel-reachable address. Confirm CLAUDE.md Phases 1-2 are already
   done (mgmt actually running at home) before this — it depends on it,
   and starting here first will just produce a proxy that can't reach
   anything.

## 3. DNS phase (`docs/vps-edge-deployment.md` Phase 2)

This skill cannot touch the operator's registrar/DNS provider — only
tell them exactly what to create and verify the result once it
propagates. Using the checklist's `domain` and `vps.public_ip`:

| Record | Type | Value |
| --- | --- | --- |
| `admin.<domain>` | A | `<vps.public_ip>` |
| `api.<domain>` | A | `<vps.public_ip>` |
| `play.<domain>` | A | `<vps.public_ip>` |

Ask whether they also want a Minecraft hostname (often reused —
`play.<domain>`, or a separate `mc.<domain>`) — it's just an A record
players type into their client; Caddy never touches `:25565`, it's raw
TCP handled directly by the relocated `folia-nexa-proxy`.

Verify with `dig +short admin.<domain>` (and the other two) once the
operator says it's done — run it yourself if this session has outbound
DNS resolution, otherwise ask them to run it and paste the result back.
Don't mark `dns_records: true` on faith; only after all three actually
resolve to the right IP.

## 4. LXD hosts phase (CLAUDE.md Phase 3, once per host)

Repeat for each entry in `lxd_hosts`, appending to
`phases.lxd_hosts_joined` as each finishes. `lxc info` must succeed on
that host before proceeding — if it errors, LXD isn't initialized yet;
have the operator run `sudo snap install lxd && sudo lxd init` there
first (don't pick a storage backend for them — same rule the join
script itself follows).

### 4.1 If this host is the WireGuard home peer

Exactly **one** of the 3 hosts is this (`is_wireguard_home_peer: true`
in the checklist) — confirm which with the operator rather than
defaulting to `host1`. On that host only:

```bash
sudo ./deploy/vps/setup-wireguard.sh --role home \
  --peer-public-key <vps-pubkey-from-§2-step-2> \
  --peer-endpoint <vps-public-ip>:51820
sudo systemctl enable --now wg-quick@wg0
```

Hand its printed pubkey back to whoever's doing §2 step 2's second pass.

### 4.2 Get a join token

Run once, from wherever the `folia-nexa-mgmt` CLI is already logged in
(not necessarily this host):

```bash
folia-nexa-mgmt hosts create-join-token
```

Tokens are single-use — a fresh one per host, never reuse across the 3.

### 4.3 Join

```bash
sudo ./tools/folia-host-join.sh \
  --mgmt-url https://<mgmt-host>:8443 \
  --join-token <token-from-4.2> \
  --name <host-name-from-checklist> \
  --address <this-host's-own-LAN-address>
```

**If this host needs placement labels** (e.g. `cpu_type=p-core`, the
kind `configs/worlds/*.sh --labels` matches against) —
`folia-host-join.sh` has no `--labels` flag and its enrollment payload
doesn't send any, even though the mgmt API's `/api/v1/hosts/enroll`
accepts a `labels` field. Don't expect the script to set them. Instead,
either run the script with `--skip-enroll` and finish enrollment
yourself with a direct `curl` that adds `"labels": {...}` to the JSON
body (same shape the script builds — see its own source for the exact
fields: `name`, `address`, `project`, `lxd_trust_token`, `capacity`),
or skip labels for now and revisit once the mgmt CLI grows a way to set
them post-enrollment (as of this writing there isn't one — `hosts`
only has `create-join-token` / `list` / `drain`).

Also note: the script's own `--skip-enroll` output tells the operator to
finish with `folia-nexa-mgmt hosts add ...` — that CLI command doesn't
actually exist yet (checked `mgmt/src/folia_mgmt/cli.py`). If you hit
`--skip-enroll`, finish the enrollment with a direct `curl` to
`POST {mgmt-url}/api/v1/hosts/enroll` (`Authorization: Bearer
<join-token>`) instead of that command — say this to the operator so
they don't go looking for a CLI subcommand that isn't there.

If `folia-host-join.sh`'s step 5 (the actual enroll call) 401s/404s
outright, that's the "documented contract, not guaranteed live"
mentioned in CLAUDE.md — fall back to `--skip-enroll` either way.

### 4.4 Verify

```bash
folia-nexa-mgmt hosts list
```

Confirm this host shows up with the address/labels you expect before
moving to the next one — catching a bad `--address` on host 1 is much
cheaper than discovering it after all 3 are "done."

## 5. Close out

Once VPS + DNS + all 3 LXD hosts are checked off:

- Re-verify the `AllowedIPs` scoping from §2 step 3 now that all 3
  hosts' real addresses are known — it's easy to do this once early with
  incomplete info and forget to revisit.
- Run `./configs/worlds/create-all.sh` (or a hand-rolled `worlds
  create`) as an end-to-end placement smoke test — a world actually
  landing on one of the 3 hosts instead of staying `pending` is stronger
  proof this all worked than any individual phase's own check.
- Tell the operator plainly which phases *this session* actually
  executed versus only advised on (per §1) — a checked-off box in the
  file doesn't mean this session watched it happen if it was run
  elsewhere.

Leave the checklist file in place — the operator, or a future session on
a different machine, needs it to resume.
