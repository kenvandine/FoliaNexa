"""Keeps every FoliaNexaStats-enabled world's config.yml pointed at a
real mgmt API token, so its periodic `POST /api/v1/stats/report` calls
actually succeed instead of getting rejected with 401. PLAN.md §7A.

Same shape of problem luckperms.py already solves (push a rendered
plugin config.yml to every world that has the plugin, on the reconcile
loop, retrying until the plugin's own data folder exists) — mirrored
here rather than invented fresh. The one real difference: LuckPerms
needs pre-existing operator-supplied MySQL credentials
(`luckperms_configured`); this needs an mgmt *API token*, which mgmt can
mint for itself.

`report_stats` (routers/stats.py) requires operator role, the same as
the proxy/bot's own service accounts (CLAUDE.md Phases 7-8) — this
module provisions an equivalent dedicated service-account User the
first time it's needed, rather than making an operator repeat that
curl-by-hand dance specifically for a plugin that's on by default on
every world. A token's plaintext only ever exists at the moment
create_api_token mints it (only its sha256 hash is stored afterward,
see auth.py) — so, same pattern as Settings.get_velocity_forwarding_
secret, the plaintext is persisted to a local state file once and
reused from then on, never regenerated as long as that file exists.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from sqlmodel import Session, select

from folia_mgmt.access_apply import NODE_WORLD_DIR
from folia_mgmt.auth import create_api_token, hash_password
from folia_mgmt.config import Settings
from folia_mgmt.lxd_client import LXDClient, LXDError
from folia_mgmt.models import Host, User, UserRole, World

logger = logging.getLogger(__name__)

STATS_CONFIG_PATH = f"{NODE_WORLD_DIR}/plugins/FoliaNexaStats/config.yml"
STATS_SERVICE_ACCOUNT_USERNAME = "folianexa-stats"


def _stats_api_token_path(settings: Settings) -> Path:
    return settings.state_dir / "stats-api-token"


def get_or_create_stats_api_token(session: Session, settings: Settings) -> str:
    path = _stats_api_token_path(settings)
    if path.exists():
        return path.read_text().strip()

    user = session.exec(select(User).where(User.username == STATS_SERVICE_ACCOUNT_USERNAME)).first()
    if user is None:
        # Random, never-used password — this service account only ever
        # authenticates via its API token below, never logs in
        # interactively, so what the password actually is doesn't matter
        # beyond "not guessable."
        user = User(
            username=STATS_SERVICE_ACCOUNT_USERNAME,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            role=UserRole.operator,
        )
        session.add(user)
        session.commit()
        session.refresh(user)

    token = create_api_token(session, user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token)
    return token


def render_stats_config(mgmt_base_url: str, api_token: str) -> bytes:
    """FoliaNexaStats' own config.yml shape — see that plugin's bundled
    config.yml (folianexa-plugins/folianexa-stats/src/main/resources/
    config.yml) for the authoritative field list this mirrors."""
    return (
        f'mgmt-base-url: "{mgmt_base_url}"\n'
        f'mgmt-api-token: "{api_token}"\n'
        "report-interval-seconds: 60\n"
    ).encode()


def apply_stats_config(session: Session, lxd_client: LXDClient, host: Host, world: World, settings: Settings) -> None:
    if "FoliaNexaStats" not in world.plugins or not world.container_name:
        return
    if not settings.public_url:
        # Same prerequisite plugins-manifest-url already has (routers/
        # worlds.py's _validate_plugins) — without a reachable mgmt
        # address there's nowhere for the world's report to go anyway.
        return
    token = get_or_create_stats_api_token(session, settings)
    content = render_stats_config(settings.public_url, token)
    try:
        lxd_client.push_file(host, world.container_name, STATS_CONFIG_PATH, content)
    except LXDError:
        # Expected to fail (and get retried next reconcile tick) until
        # FoliaNexaStats itself has run once and created its plugin data
        # folder — see module docstring.
        logger.debug(
            "could not push FoliaNexaStats config for world '%s' yet (plugin folder may not exist), will retry",
            world.name,
        )
