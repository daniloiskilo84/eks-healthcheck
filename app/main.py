"""Web layer: HTML pages for people, a JSON API for scripts and the TV."""

from __future__ import annotations

import base64
import binascii
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .backend import Backend, BackendError
from .config import Settings
from .demo import DemoBackend
from .format import cpu_cores, humanize_duration, memory_gib
from .health import ClusterSummary, DeploymentHealth, NodeHealth
from .models import Pod
from .service import ClusterService

HERE = Path(__file__).parent
# Browsers cannot send a custom header cross-site without a CORS preflight, which this
# app never allows, so requiring it on the one state-changing endpoint blocks CSRF.
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "healthcheck"
MAX_TAIL = 2000

log = logging.getLogger("healthcheck")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _ago(dt: datetime | None) -> str:
    return humanize_duration((datetime.now(UTC) - dt).total_seconds()) if dt else "-"


def _basic_user(header: str) -> tuple[str, str] | None:
    if not header.lower().startswith("basic "):
        return None
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return user, password


def _authorized(header: str, settings: Settings) -> bool:
    creds = _basic_user(header)
    if creds is None:
        return False
    user_ok = secrets.compare_digest(creds[0].encode(), settings.auth_username.encode())
    pass_ok = secrets.compare_digest(creds[1].encode(), settings.auth_password.encode())
    return user_ok and pass_ok


# ---- JSON shapes (stable API, independent of the internal models) ----------------


def pod_json(p: Pod) -> dict:
    return {
        "name": p.name,
        "status": p.status_text,
        "phase": p.phase,
        "ready": f"{p.ready_count}/{len(p.main_containers)}",
        "restarts": p.restarts,
        "node": p.node or None,
        "created": _iso(p.created),
        "containers": p.container_names,
    }


def deployment_json(h: DeploymentHealth, with_pods: bool = False) -> dict:
    d = h.deployment
    out = {
        "namespace": d.namespace,
        "name": d.name,
        "status": h.status,
        "reasons": h.reasons,
        "notes": h.notes,
        "replicas": {"desired": d.replicas, "ready": d.ready, "updated": d.updated, "available": d.available},
        "restarts": h.restarts,
        "images": d.images,
        "created": _iso(d.created),
        "restartedAt": d.restarted_at or None,
    }
    if with_pods:
        out["pods"] = [pod_json(p) for p in h.pods]
    return out


def node_json(h: NodeHealth) -> dict:
    n = h.node
    return {
        "name": n.name,
        "status": h.status,
        "reasons": h.reasons,
        "notes": h.notes,
        "instanceType": h.instance_type,
        "family": h.family,
        "capacityType": h.capacity_type,
        "zone": h.zone,
        "nodegroup": h.nodegroup,
        "kubeletVersion": n.kubelet_version,
        "allocatable": {"cpu": n.cpu, "memory": n.memory},
        "pods": h.pod_count,
        "created": _iso(n.created),
        "uptimeSeconds": int(h.uptime_seconds),
    }


def summary_json(s: ClusterSummary, cluster: str, stale: bool) -> dict:
    return {
        "cluster": cluster,
        "healthy": s.healthy,
        "stale": stale,
        "takenAt": _iso(s.taken_at),
        "deployments": {"total": s.deployments_total, "ok": s.deployments_ok, "nok": s.deployments_nok},
        "nodes": {"total": s.nodes_total, "ready": s.nodes_ready},
        "pods": {"byPhase": s.pods_by_phase, "problems": s.problem_pods, "restarts": s.restarts},
        "nodeFamilies": s.families,
        "nokDeployments": [deployment_json(d) for d in s.nok_deployments],
        "nokNodes": [node_json(n) for n in s.nok_nodes],
    }


# ---- app factory -----------------------------------------------------------------


def create_app(settings: Settings | None = None, backend: Backend | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if backend is None:
        if settings.demo_mode:
            backend = DemoBackend()
        else:
            from .k8s import KubernetesBackend

            backend = KubernetesBackend.from_environment()
    service = ClusterService(backend, settings)

    app = FastAPI(title="eks-healthcheck", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.service = service
    app.state.settings = settings

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters.update(duration=humanize_duration, ago=_ago, gib=memory_gib, cores=cpu_cores)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def security(request: Request, call_next):
        public = request.url.path in ("/healthz", "/readyz")
        if settings.auth_enabled and not public and not _authorized(request.headers.get("authorization", ""), settings):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="healthcheck"'})
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return response

    @app.exception_handler(BackendError)
    async def backend_error(request: Request, exc: BackendError):
        status = exc.status if 400 <= exc.status < 600 else 502
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.message}, status_code=status)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": exc.message, "cluster_name": settings.cluster_name, "retry": True},
            status_code=status,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/") or exc.status_code < 400:
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"{exc.status_code}: {exc.detail}", "cluster_name": settings.cluster_name, "retry": False},
            status_code=exc.status_code,
        )

    def page(request: Request, name: str, **context) -> HTMLResponse:
        base = {
            "cluster_name": settings.cluster_name,
            "demo": settings.demo_mode,
            "stale": service.stale,
            "error": service.last_error,
            "taken_at": service.taken_at(),
            "refresh_seconds": settings.refresh_seconds,
            "rollout_enabled": settings.rollout_enabled,
        }
        return templates.TemplateResponse(request, name, {**base, **context})

    def allowed_or_404(namespace: str) -> None:
        if not service.namespace_allowed(namespace):
            raise HTTPException(404, "namespace not watched by this healthcheck")

    # ---- probes ----------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        try:
            service.backend.ping()
        except BackendError as exc:
            return JSONResponse({"status": "unavailable", "error": exc.message}, status_code=503)
        return {"status": "ok"}

    # ---- pages -----------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse("/deployments", status_code=307)

    @app.get("/deployments", response_class=HTMLResponse, include_in_schema=False)
    def deployments_page(request: Request, ns: str = ""):
        items = service.deployments(ns or None)
        nok = sum(1 for d in items if not d.ok)
        return page(
            request,
            "deployments.html",
            active="deployments",
            items=items,
            ok_count=len(items) - nok,
            nok_count=nok,
            namespaces=service.namespaces(),
            selected_ns=ns,
        )

    @app.get("/deployments/{namespace}/{name}", response_class=HTMLResponse, include_in_schema=False)
    def deployment_page(request: Request, namespace: str, name: str):
        allowed_or_404(namespace)
        item = service.deployment(namespace, name)
        if item is None:
            raise HTTPException(404, "deployment not found")
        return page(request, "deployment.html", active="deployments", item=item, dep=item.deployment)

    @app.get("/nodes", response_class=HTMLResponse, include_in_schema=False)
    def nodes_page(request: Request):
        items = service.nodes()
        return page(request, "nodes.html", active="nodes", items=items, summary=service.summary())

    @app.get("/tv", response_class=HTMLResponse, include_in_schema=False)
    def tv_page(request: Request):
        return page(
            request,
            "tv.html",
            summary=service.summary(),
            nodes=service.nodes(),
            refresh_seconds=settings.tv_refresh_seconds,
        )

    # ---- API -------------------------------------------------------------------

    @app.get("/api/summary")
    def api_summary():
        return summary_json(service.summary(), settings.cluster_name, service.stale)

    @app.get("/api/deployments")
    def api_deployments(namespace: str = ""):
        return [deployment_json(d) for d in service.deployments(namespace or None)]

    @app.get("/api/deployments/{namespace}/{name}")
    def api_deployment(namespace: str, name: str):
        allowed_or_404(namespace)
        item = service.deployment(namespace, name)
        if item is None:
            raise HTTPException(404, "deployment not found")
        return deployment_json(item, with_pods=True)

    @app.post("/api/deployments/{namespace}/{name}/rollout")
    def api_rollout(namespace: str, name: str, request: Request):
        if not settings.rollout_enabled:
            raise HTTPException(403, "rollout is disabled (ROLLOUT_ENABLED=false)")
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise HTTPException(403, f"missing {CSRF_HEADER} header")
        allowed_or_404(namespace)
        if service.deployment(namespace, name) is None:
            raise HTTPException(404, "deployment not found")
        restarted_at = service.backend.rollout_restart(namespace, name)
        service.invalidate()
        creds = _basic_user(request.headers.get("authorization", ""))
        client = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else "-"
        )
        log.info(
            "rollout restart deployment=%s/%s user=%s client=%s restartedAt=%s",
            namespace,
            name,
            creds[0] if creds else "anonymous",
            client,
            restarted_at,
        )
        return {"namespace": namespace, "name": name, "restartedAt": restarted_at}

    @app.get("/api/nodes")
    def api_nodes():
        return [node_json(n) for n in service.nodes()]

    @app.get("/api/pods/{namespace}/{pod}/events")
    def api_pod_events(namespace: str, pod: str):
        allowed_or_404(namespace)
        return [
            {
                "type": e.type,
                "reason": e.reason,
                "message": e.message,
                "count": e.count,
                "lastSeen": _iso(e.last_seen),
                "age": _ago(e.last_seen),
                "source": e.source,
            }
            for e in service.backend.pod_events(namespace, pod)
        ]

    @app.get("/api/pods/{namespace}/{pod}/logs", response_class=PlainTextResponse)
    def api_pod_logs(
        namespace: str,
        pod: str,
        container: str = "",
        tail: int = Query(default=0, ge=0, le=MAX_TAIL),
        previous: bool = False,
    ):
        allowed_or_404(namespace)
        found = service.find_pod(namespace, pod)
        if found is None:
            raise HTTPException(404, "pod not found")
        if container and container not in found.container_names:
            raise HTTPException(400, f"container {container!r} is not in pod {pod}")
        name = container or (found.container_names[0] if found.container_names else None)
        text = service.backend.pod_logs(namespace, pod, name, tail or settings.log_tail_lines, previous)
        return PlainTextResponse(text or "(no log lines)\n")

    return app
