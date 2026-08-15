from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import Depends

from folia_mgmt.certs import ensure_client_identity
from folia_mgmt.config import Settings, get_settings
from folia_mgmt.lxd_client import LXDClient
from folia_mgmt.scheduler import HealthCheck, default_health_check


def settings_dependency() -> Settings:
    return get_settings()


@lru_cache
def _lxd_client_for(certs_dir_str: str) -> LXDClient:
    cert, key = ensure_client_identity(Path(certs_dir_str))
    return LXDClient(cert, key)


def get_lxd_client(settings: Settings = Depends(settings_dependency)) -> LXDClient:
    return _lxd_client_for(str(settings.certs_dir))


def get_health_check() -> HealthCheck:
    return default_health_check
