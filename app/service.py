"""Caching and filtering between the backend and the pages.

A TV, a few browser tabs and the API all poll. Without a cache each refresh would
list every pod in the cluster; with it, the API server sees one LIST per TTL no
matter how many people are watching. If the API server is down, the last good
snapshot keeps being served (flagged as stale) instead of a blank TV.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from datetime import datetime

from .backend import Backend, BackendError
from .config import Settings
from .health import (
    ClusterSummary,
    DeploymentHealth,
    NodeHealth,
    evaluate_deployment,
    evaluate_node,
    sort_deployments,
    sort_nodes,
    summarize,
)
from .models import Snapshot

log = logging.getLogger("healthcheck")


class ClusterService:
    def __init__(self, backend: Backend, settings: Settings, clock=time.monotonic) -> None:
        self.backend = backend
        self.settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._fetched_at = 0.0
        self.last_error: str = ""

    # ---- snapshot -----------------------------------------------------------

    def namespace_allowed(self, namespace: str) -> bool:
        s = self.settings
        return (not s.namespaces or namespace in s.namespaces) and namespace not in s.exclude_namespaces

    def _filter(self, snap: Snapshot) -> Snapshot:
        return Snapshot(
            deployments=[d for d in snap.deployments if self.namespace_allowed(d.namespace)],
            pods=[p for p in snap.pods if self.namespace_allowed(p.namespace)],
            nodes=snap.nodes,
            taken_at=snap.taken_at,
        )

    def snapshot(self) -> Snapshot:
        with self._lock:  # one fetch at a time; concurrent callers reuse its result
            fresh = self._snapshot is not None and self._clock() - self._fetched_at < self.settings.cache_ttl_seconds
            if fresh:
                return self._snapshot
            try:
                self._snapshot = self._filter(self.backend.fetch())
                self._fetched_at = self._clock()
                self.last_error = ""
            except BackendError as exc:
                self.last_error = exc.message
                log.warning("fetch failed: %s", exc.message)
                if self._snapshot is None:
                    raise
                self._fetched_at = self._clock()  # back off for one TTL before retrying
            return self._snapshot

    @property
    def stale(self) -> bool:
        return bool(self.last_error)

    def invalidate(self) -> None:
        with self._lock:
            self._fetched_at = 0.0

    # ---- views --------------------------------------------------------------

    def deployments(self, namespace: str | None = None) -> list[DeploymentHealth]:
        snap = self.snapshot()
        grace = self.settings.pending_grace_seconds
        items = [
            evaluate_deployment(d, snap.pods, snap.taken_at, grace)
            for d in snap.deployments
            if namespace in (None, "", d.namespace)
        ]
        return sort_deployments(items)

    def deployment(self, namespace: str, name: str) -> DeploymentHealth | None:
        return next((d for d in self.deployments(namespace) if d.deployment.name == name), None)

    def nodes(self) -> list[NodeHealth]:
        snap = self.snapshot()
        per_node = Counter(p.node for p in snap.pods if p.node and not p.deleting)
        return sort_nodes([evaluate_node(n, snap.taken_at, per_node[n.name]) for n in snap.nodes])

    def summary(self) -> ClusterSummary:
        snap = self.snapshot()
        return summarize(snap, self.deployments(), self.nodes(), self.settings.pending_grace_seconds)

    def namespaces(self) -> list[str]:
        return sorted({d.namespace for d in self.snapshot().deployments})

    def find_pod(self, namespace: str, pod: str):
        return next((p for p in self.snapshot().pods if p.namespace == namespace and p.name == pod), None)

    def taken_at(self) -> datetime:
        return self.snapshot().taken_at
