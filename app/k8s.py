"""Real backend: the Kubernetes API through the official Python client.

Inside the cluster it authenticates with its ServiceAccount; on a laptop it falls
back to the current kubeconfig context (handy with kind or `aws eks update-kubeconfig`).
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime
from typing import Any

from .backend import BackendError
from .models import (
    Condition,
    ContainerStatus,
    Deployment,
    Event,
    LabelSelector,
    Node,
    Pod,
    SelectorRequirement,
    Snapshot,
)

RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"
TIMEOUT = 15  # seconds per API call
LOG_LIMIT_BYTES = 512 * 1024
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _api_errors(func):
    """Turn client exceptions into BackendError so the web layer has one error type."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        from kubernetes.client.exceptions import ApiException
        from urllib3.exceptions import HTTPError

        try:
            return func(*args, **kwargs)
        except ApiException as exc:
            raise BackendError(f"Kubernetes API {exc.status}: {exc.reason}", exc.status or 502) from exc
        except (HTTPError, OSError) as exc:
            raise BackendError(f"Kubernetes API unreachable: {exc}", 503) from exc

    return wrapper


def _ts(value: datetime | None) -> datetime:
    if value is None:
        return EPOCH
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _conditions(items: list[Any] | None) -> list[Condition]:
    return [Condition(c.type, c.status, c.reason or "", c.message or "") for c in items or []]


def to_deployment(d: Any) -> Deployment:
    spec, status = d.spec, d.status
    sel = spec.selector
    selector = LabelSelector(
        match_labels=dict(sel.match_labels or {}) if sel else {},
        match_expressions=tuple(
            SelectorRequirement(e.key, e.operator, tuple(e.values or ())) for e in (sel.match_expressions or [])
        )
        if sel
        else (),
    )
    template_meta = spec.template.metadata
    annotations = (template_meta.annotations or {}) if template_meta else {}
    return Deployment(
        namespace=d.metadata.namespace,
        name=d.metadata.name,
        replicas=spec.replicas if spec.replicas is not None else 1,
        ready=status.ready_replicas or 0,
        available=status.available_replicas or 0,
        updated=status.updated_replicas or 0,
        created=_ts(d.metadata.creation_timestamp),
        selector=selector,
        conditions=_conditions(status.conditions),
        images=[c.image for c in spec.template.spec.containers],
        restarted_at=annotations.get(RESTARTED_AT, ""),
    )


def _container_status(cs: Any, init: bool) -> ContainerStatus:
    state, reason = "waiting", ""
    if cs.state is not None:
        if cs.state.running is not None:
            state = "running"
        elif cs.state.terminated is not None:
            state, reason = "terminated", cs.state.terminated.reason or ""
        elif cs.state.waiting is not None:
            reason = cs.state.waiting.reason or ""
    last = ""
    if cs.last_state is not None and cs.last_state.terminated is not None:
        last = cs.last_state.terminated.reason or ""
    return ContainerStatus(cs.name, bool(cs.ready), cs.restart_count or 0, state, reason, last, init)


def to_pod(p: Any) -> Pod:
    status = p.status
    containers: list[ContainerStatus] = []
    for cs in status.init_container_statuses or []:
        containers.append(_container_status(cs, init=True))
    known = {cs.name for cs in status.container_statuses or []}
    for cs in status.container_statuses or []:
        containers.append(_container_status(cs, init=False))
    # Unscheduled pods have no container statuses yet: list them from the spec.
    for c in p.spec.containers:
        if c.name not in known:
            containers.append(ContainerStatus(c.name))
    return Pod(
        namespace=p.metadata.namespace,
        name=p.metadata.name,
        phase=status.phase or "Unknown",
        created=_ts(p.metadata.creation_timestamp),
        labels=dict(p.metadata.labels or {}),
        node=p.spec.node_name or "",
        reason=status.reason or "",
        containers=containers,
        deleting=p.metadata.deletion_timestamp is not None,
    )


def to_node(n: Any) -> Node:
    status = n.status
    allocatable = status.allocatable or {}
    return Node(
        name=n.metadata.name,
        created=_ts(n.metadata.creation_timestamp),
        labels=dict(n.metadata.labels or {}),
        conditions=_conditions(status.conditions),
        unschedulable=bool(n.spec.unschedulable),
        kubelet_version=status.node_info.kubelet_version if status.node_info else "",
        cpu=allocatable.get("cpu", ""),
        memory=allocatable.get("memory", ""),
    )


def to_event(e: Any) -> Event:
    last = e.last_timestamp or e.event_time or e.first_timestamp or e.metadata.creation_timestamp
    source = ""
    if e.source is not None:
        source = e.source.component or ""
    return Event(
        type=e.type or "Normal",
        reason=e.reason or "",
        message=(e.message or "").strip(),
        count=e.count or 1,
        last_seen=_ts(last) if last else None,
        source=source or (e.reporting_component or ""),
    )


class KubernetesBackend:
    def __init__(self, core: Any, apps: Any, version: Any) -> None:
        self.core, self.apps, self.version = core, apps, version

    @classmethod
    def from_environment(cls) -> KubernetesBackend:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        return cls(client.CoreV1Api(), client.AppsV1Api(), client.VersionApi())

    @_api_errors
    def fetch(self) -> Snapshot:
        now = datetime.now(UTC)
        deployments = self.apps.list_deployment_for_all_namespaces(_request_timeout=TIMEOUT).items
        pods = self.core.list_pod_for_all_namespaces(_request_timeout=TIMEOUT).items
        nodes = self.core.list_node(_request_timeout=TIMEOUT).items
        return Snapshot(
            deployments=[to_deployment(d) for d in deployments],
            pods=[to_pod(p) for p in pods],
            nodes=[to_node(n) for n in nodes],
            taken_at=now,
        )

    @_api_errors
    def pod_events(self, namespace: str, pod: str) -> list[Event]:
        items = self.core.list_namespaced_event(
            namespace,
            field_selector=f"involvedObject.kind=Pod,involvedObject.name={pod}",
            _request_timeout=TIMEOUT,
        ).items
        events = [to_event(e) for e in items]
        return sorted(events, key=lambda e: e.last_seen or EPOCH, reverse=True)

    @_api_errors
    def pod_logs(self, namespace: str, pod: str, container: str | None, tail_lines: int, previous: bool) -> str:
        kwargs: dict[str, Any] = {
            "tail_lines": tail_lines,
            "previous": previous,
            "timestamps": True,
            "limit_bytes": LOG_LIMIT_BYTES,
            "_request_timeout": TIMEOUT,
        }
        if container:
            kwargs["container"] = container
        return self.core.read_namespaced_pod_log(pod, namespace, **kwargs) or ""

    @_api_errors
    def rollout_restart(self, namespace: str, name: str) -> str:
        restarted_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {"spec": {"template": {"metadata": {"annotations": {RESTARTED_AT: restarted_at}}}}}
        self.apps.patch_namespaced_deployment(name, namespace, body, _request_timeout=TIMEOUT)
        return restarted_at

    @_api_errors
    def ping(self) -> None:
        self.version.get_code(_request_timeout=5)
