"""End-to-end through HTTP, with the demo cluster as backend."""

import base64

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.demo import DemoBackend
from app.main import create_app

ROLLOUT_HEADERS = {"X-Requested-With": "healthcheck"}


def client_for(**overrides) -> TestClient:
    settings = Settings(cluster_name="demo-eks", demo_mode=True, cache_ttl_seconds=0, **overrides)
    return TestClient(create_app(settings, DemoBackend(seed=7)))


@pytest.fixture
def api() -> TestClient:
    return client_for()


def test_probes(api):
    assert api.get("/healthz").json() == {"status": "ok"}
    assert api.get("/readyz").json() == {"status": "ok"}


@pytest.mark.parametrize(
    "path", ["/deployments", "/deployments?ns=payments", "/nodes", "/tv", "/deployments/payments/worker"]
)
def test_pages_render(api, path):
    r = api.get(path)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "demo-eks" in r.text


def test_root_redirects(api):
    r = api.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/deployments"


def test_deployments_sorted_nok_first(api):
    items = api.get("/api/deployments").json()
    statuses = [d["status"] for d in items]
    assert statuses == sorted(statuses, key=lambda s: s == "OK")
    nok = {f'{d["namespace"]}/{d["name"]}' for d in items if d["status"] == "NOK"}
    assert nok == {"payments/worker", "search/indexer", "notifications/sms-sender"}


def test_deployment_detail_lists_pods(api):
    d = api.get("/api/deployments/payments/worker").json()
    assert d["status"] == "NOK"
    assert any("CrashLoopBackOff" in r for r in d["reasons"])
    assert len(d["pods"]) == 3 and d["pods"][0]["status"] == "CrashLoopBackOff"


def test_unknown_deployment_is_404(api):
    assert api.get("/api/deployments/payments/nope").status_code == 404
    page = api.get("/deployments/payments/nope")
    assert page.status_code == 404 and "text/html" in page.headers["content-type"]


def test_nodes_and_summary(api):
    nodes = api.get("/api/nodes").json()
    assert nodes[0]["status"] == "NOK" and nodes[0]["instanceType"] == "t3.large"
    s = api.get("/api/summary").json()
    assert s["deployments"]["nok"] == 3 and s["nodes"] == {"total": 6, "ready": 5}
    assert s["nodeFamilies"] == {"c6g": 2, "m5": 3, "t3": 1}
    assert not s["healthy"]


def test_events_and_logs(api):
    pod = api.get("/api/deployments/payments/worker").json()["pods"][0]["name"]
    events = api.get(f"/api/pods/payments/{pod}/events").json()
    assert events[0]["reason"] == "BackOff" and events[0]["type"] == "Warning"
    logs = api.get(f"/api/pods/payments/{pod}/logs?tail=50&previous=true")
    assert logs.status_code == 200 and "panic: database unavailable" in logs.text


def test_logs_of_waiting_container_returns_api_error(api):
    pod = api.get("/api/deployments/search/indexer").json()["pods"][0]["name"]
    r = api.get(f"/api/pods/search/{pod}/logs")
    assert r.status_code == 400 and "waiting to start" in r.json()["error"]


def test_logs_validate_container_and_tail(api):
    pod = api.get("/api/deployments/catalog/api").json()["pods"][0]["name"]
    assert api.get(f"/api/pods/catalog/{pod}/logs?container=evil").status_code == 400
    assert api.get(f"/api/pods/catalog/{pod}/logs?tail=999999").status_code == 422
    assert api.get("/api/pods/catalog/does-not-exist/logs").status_code == 404


def test_rollout_requires_csrf_header(api):
    assert api.post("/api/deployments/catalog/api/rollout").status_code == 403


def test_rollout_recreates_pods(api):
    before = {p["name"] for p in api.get("/api/deployments/catalog/api").json()["pods"]}
    r = api.post("/api/deployments/catalog/api/rollout", headers=ROLLOUT_HEADERS)
    assert r.status_code == 200 and r.json()["restartedAt"]
    after = api.get("/api/deployments/catalog/api").json()
    assert after["restartedAt"] == r.json()["restartedAt"]
    assert before.isdisjoint({p["name"] for p in after["pods"]})


def test_rollout_disabled():
    c = client_for(rollout_enabled=False)
    assert c.post("/api/deployments/catalog/api/rollout", headers=ROLLOUT_HEADERS).status_code == 403
    assert "Rollout restart" not in c.get("/deployments/catalog/api").text


def test_excluded_namespace_is_invisible():
    c = client_for(exclude_namespaces=("kube-system",))
    assert all(d["namespace"] != "kube-system" for d in c.get("/api/deployments").json())
    assert c.get("/api/deployments/kube-system/coredns").status_code == 404
    assert c.get("/api/pods/kube-system/anything/logs").status_code == 404
    assert c.post("/api/deployments/kube-system/coredns/rollout", headers=ROLLOUT_HEADERS).status_code == 404


def test_basic_auth():
    c = client_for(auth_username="ops", auth_password="s3cret")
    assert c.get("/deployments").status_code == 401
    assert c.get("/healthz").status_code == 200  # kubelet probes stay open
    bad = base64.b64encode(b"ops:wrong").decode()
    assert c.get("/deployments", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    good = base64.b64encode(b"ops:s3cret").decode()
    assert c.get("/deployments", headers={"Authorization": f"Basic {good}"}).status_code == 200


def test_security_headers(api):
    r = api.get("/deployments")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
