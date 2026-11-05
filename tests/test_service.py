from app.backend import BackendError
from app.config import Settings
from app.models import Snapshot
from app.service import ClusterService

from .conftest import NOW, make_deployment, make_node, make_pod


class FakeBackend:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.deployments = [
            make_deployment("api", "shop"),
            make_deployment("broken", "shop", replicas=1, ready=0),
            make_deployment("coredns", "kube-system"),
        ]

    def fetch(self):
        self.calls += 1
        if self.fail:
            raise BackendError("connection refused", 503)
        return Snapshot(
            deployments=list(self.deployments),
            pods=[make_pod("api-1"), make_pod("api-2"), make_pod("dns-1", namespace="kube-system", app="coredns")],
            nodes=[make_node("node-1"), make_node("node-2", ready="False")],
            taken_at=NOW,
        )


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(settings=None):
    backend, clock = FakeBackend(), Clock()
    return ClusterService(backend, settings or Settings(cache_ttl_seconds=10), clock), backend, clock


def test_cache_serves_many_readers_with_one_fetch():
    service, backend, clock = make()
    service.deployments()
    service.nodes()
    service.summary()
    assert backend.calls == 1
    clock.t += 11
    service.deployments()
    assert backend.calls == 2


def test_invalidate_forces_next_fetch():
    service, backend, _ = make()
    service.deployments()
    service.invalidate()
    service.deployments()
    assert backend.calls == 2


def test_stale_snapshot_is_served_when_api_fails():
    service, backend, clock = make()
    first = service.snapshot()
    backend.fail = True
    clock.t += 11
    assert service.snapshot() is first
    assert service.stale and "connection refused" in service.last_error
    backend.fail = False
    clock.t += 11
    service.snapshot()
    assert not service.stale


def test_first_fetch_failure_raises():
    service, backend, _ = make()
    backend.fail = True
    try:
        service.snapshot()
    except BackendError as exc:
        assert exc.status == 503
    else:
        raise AssertionError("expected BackendError")


def test_namespace_filters():
    service, _, _ = make(Settings(exclude_namespaces=("kube-system",)))
    assert [d.deployment.name for d in service.deployments()] == ["broken", "api"]
    assert not service.namespace_allowed("kube-system")

    only, _, _ = make(Settings(namespaces=("kube-system",)))
    assert [d.deployment.name for d in only.deployments()] == ["coredns"]


def test_summary_counts():
    service, _, _ = make()
    s = service.summary()
    assert (s.deployments_total, s.deployments_ok, s.deployments_nok) == (3, 2, 1)
    assert (s.nodes_total, s.nodes_ready) == (2, 1)
    assert s.pods_by_phase == {"Running": 3}
    assert not s.healthy
    assert [n.node.name for n in service.nodes()] == ["node-2", "node-1"]  # NOK first
    assert next(n for n in service.nodes() if n.node.name == "node-1").pod_count == 3
