"""Settings come from environment variables (the Helm chart sets them)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _list(value: str | None) -> tuple[str, ...]:
    return tuple(x.strip() for x in (value or "").split(",") if x.strip())


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    cluster_name: str = "my-cluster"
    demo_mode: bool = False
    namespaces: tuple[str, ...] = ()  # empty = every namespace
    exclude_namespaces: tuple[str, ...] = ()
    cache_ttl_seconds: int = 10
    refresh_seconds: int = 30
    tv_refresh_seconds: int = 15
    rollout_enabled: bool = True
    auth_username: str = ""
    auth_password: str = ""
    log_tail_lines: int = 300
    pending_grace_seconds: int = 300

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        return cls(
            cluster_name=e.get("CLUSTER_NAME", cls.cluster_name),
            demo_mode=_bool(e.get("DEMO_MODE"), False),
            namespaces=_list(e.get("NAMESPACES")),
            exclude_namespaces=_list(e.get("EXCLUDE_NAMESPACES")),
            cache_ttl_seconds=_int(e.get("CACHE_TTL_SECONDS"), cls.cache_ttl_seconds),
            refresh_seconds=_int(e.get("REFRESH_SECONDS"), cls.refresh_seconds),
            tv_refresh_seconds=_int(e.get("TV_REFRESH_SECONDS"), cls.tv_refresh_seconds),
            rollout_enabled=_bool(e.get("ROLLOUT_ENABLED"), True),
            auth_username=e.get("AUTH_USERNAME", ""),
            auth_password=e.get("AUTH_PASSWORD", ""),
            log_tail_lines=_int(e.get("LOG_TAIL_LINES"), cls.log_tail_lines),
            pending_grace_seconds=_int(e.get("PENDING_GRACE_SECONDS"), cls.pending_grace_seconds),
        )

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_username and self.auth_password)
