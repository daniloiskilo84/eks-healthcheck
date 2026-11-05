"""The contract every data source fulfils (real cluster or demo)."""

from __future__ import annotations

from typing import Protocol

from .models import Event, Snapshot


class BackendError(Exception):
    """A Kubernetes API call failed; `status` follows HTTP semantics."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class Backend(Protocol):
    def fetch(self) -> Snapshot:
        """Deployments, pods and nodes of the whole cluster, in one go."""

    def pod_events(self, namespace: str, pod: str) -> list[Event]: ...

    def pod_logs(self, namespace: str, pod: str, container: str | None, tail_lines: int, previous: bool) -> str: ...

    def rollout_restart(self, namespace: str, name: str) -> str:
        """Same as `kubectl rollout restart`; returns the restartedAt timestamp."""

    def ping(self) -> None:
        """Raise BackendError when the API server is unreachable."""
