"""A believable fake EKS cluster, so the UI runs (and can be shown) without AWS.

It has the failures you meet on a real cluster: a CrashLoopBackOff worker, an image
that does not exist in ECR, a pod that cannot be scheduled, a rollout half done,
a node that stopped reporting and a cordoned node.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from .backend import BackendError
from .models import (
    Condition,
    ContainerStatus,
    Deployment,
    Event,
    LabelSelector,
    Node,
    Pod,
    Snapshot,
)

ECR = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
HEALTHY, CRASHLOOP, IMAGE_PULL, PENDING = "healthy", "crashloop", "imagepull", "pending"


@dataclass
class _Spec:
    namespace: str
    name: str
    replicas: int
    image: str
    fault: str = HEALTHY
    age_days: int = 30
    rolling: bool = False


_SPECS = [
    _Spec("checkout", "frontend", 3, f"{ECR}/checkout-frontend:5.12.0", rolling=True),
    _Spec("checkout", "api", 3, f"{ECR}/checkout-api:3.8.2"),
    _Spec("checkout", "cart", 2, f"{ECR}/checkout-cart:1.19.4"),
    _Spec("payments", "api", 3, f"{ECR}/payments-api:7.2.0", age_days=64),
    _Spec("payments", "worker", 3, f"{ECR}/payments-worker:7.2.0", CRASHLOOP, age_days=64),
    _Spec("payments", "webhook", 2, f"{ECR}/payments-webhook:2.0.1"),
    _Spec("catalog", "api", 2, f"{ECR}/catalog-api:4.4.0"),
    _Spec("catalog", "images", 2, f"{ECR}/catalog-images:1.3.7"),
    _Spec("search", "api", 2, f"{ECR}/search-api:2.4.1"),
    _Spec("search", "indexer", 1, f"{ECR}/search-indexer:2.4.1", IMAGE_PULL, age_days=0),
    _Spec("notifications", "email-sender", 2, f"{ECR}/email-sender:1.7.0"),
    _Spec("notifications", "sms-sender", 2, f"{ECR}/sms-sender:1.2.3", PENDING),
    _Spec("auth", "api", 2, f"{ECR}/auth-api:6.0.4", age_days=90),
    _Spec("batch", "report-generator", 0, f"{ECR}/report-generator:0.9.0"),
    _Spec(
        "ingress-nginx", "ingress-nginx-controller", 3, "registry.k8s.io/ingress-nginx/controller:v1.11.3", age_days=120
    ),
    _Spec("monitoring", "grafana", 1, "docker.io/grafana/grafana:11.3.0", age_days=120),
    _Spec("monitoring", "kube-state-metrics", 1, "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.13.0"),
    _Spec(
        "kube-system", "coredns", 2, "602401143452.dkr.ecr.us-east-1.amazonaws.com/eks/coredns:v1.11.3", age_days=200
    ),
    _Spec("kube-system", "metrics-server", 1, "registry.k8s.io/metrics-server/metrics-server:v0.7.2", age_days=200),
    _Spec("kube-system", "aws-load-balancer-controller", 2, "public.ecr.aws/eks/aws-load-balancer-controller:v2.9.2"),
    _Spec("kube-system", "ebs-csi-controller", 2, "public.ecr.aws/ebs-csi-driver/aws-ebs-csi-driver:v1.36.0"),
]

# name, instance type, capacity, node group, zone, age, ready, cordoned
_NODES = [
    ("ip-10-0-1-23.ec2.internal", "m5.xlarge", "ON_DEMAND", "general", "us-east-1a", timedelta(days=41), True, False),
    ("ip-10-0-2-87.ec2.internal", "m5.xlarge", "ON_DEMAND", "general", "us-east-1b", timedelta(days=41), True, False),
    ("ip-10-0-3-140.ec2.internal", "m5.2xlarge", "ON_DEMAND", "general", "us-east-1c", timedelta(days=12), True, True),
    (
        "ip-10-0-1-201.ec2.internal",
        "c6g.large",
        "SPOT",
        "spot-arm",
        "us-east-1a",
        timedelta(days=2, hours=5),
        True,
        False,
    ),
    ("ip-10-0-2-55.ec2.internal", "c6g.large", "SPOT", "spot-arm", "us-east-1b", timedelta(hours=6), True, False),
    (
        "ip-10-0-3-9.ec2.internal",
        "t3.large",
        "ON_DEMAND",
        "batch",
        "us-east-1c",
        timedelta(hours=3, minutes=20),
        False,
        False,
    ),
]


class DemoBackend:
    def __init__(self, seed: int = 42, now: datetime | None = None) -> None:
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._boot = now or datetime.now(UTC)
        self._nodes = [self._node(*n) for n in _NODES]
        self._ready_nodes = [n.name for n in self._nodes if n.condition("Ready").status == "True"]
        self._faults: dict[tuple[str, str], str] = {}
        self._deployments: dict[tuple[str, str], Deployment] = {}
        self._pods: dict[tuple[str, str], list[Pod]] = {}
        for spec in _SPECS:
            self._create(spec)

    # ---- fake cluster -------------------------------------------------------

    def _hash(self, length: int) -> str:
        return "".join(self._rng.choice("bcdfghjklmnpqrstvwxz2456789") for _ in range(length))

    def _node(self, name, itype, capacity, group, zone, age, ready, cordoned) -> Node:
        conditions = [
            Condition("MemoryPressure", "False", "KubeletHasSufficientMemory"),
            Condition("DiskPressure", "False", "KubeletHasNoDiskPressure"),
            Condition("PIDPressure", "False", "KubeletHasSufficientPID"),
            Condition("Ready", "True", "KubeletReady", "kubelet is posting ready status")
            if ready
            else Condition("Ready", "Unknown", "NodeStatusUnknown", "Kubelet stopped posting node status."),
        ]
        arch = "arm64" if itype.startswith(("c6g", "m6g", "r6g", "c7g")) else "amd64"
        cpu, mem = {"m5.xlarge": ("3920m", "15147008Ki"), "m5.2xlarge": ("7910m", "31375968Ki")}.get(
            itype, ("1930m", "7291276Ki")
        )
        return Node(
            name=name,
            created=self._boot - age,
            labels={
                "node.kubernetes.io/instance-type": itype,
                "eks.amazonaws.com/capacityType": capacity,
                "eks.amazonaws.com/nodegroup": group,
                "topology.kubernetes.io/zone": zone,
                "kubernetes.io/arch": arch,
            },
            conditions=conditions,
            unschedulable=cordoned,
            kubelet_version="v1.31.2-eks-94953ac",
            cpu=cpu,
            memory=mem,
        )

    def _pod(self, spec: _Spec, template_hash: str, index: int, created: datetime, fault: str) -> Pod:
        name = f"{spec.name}-{template_hash}-{self._hash(5)}"
        node = self._ready_nodes[(index + len(spec.name)) % len(self._ready_nodes)]
        container = spec.name.split("-")[0] if spec.namespace != "kube-system" else spec.name
        status = ContainerStatus(container, True, 0, "running")
        phase = "Running"
        if fault == CRASHLOOP and index == 0:
            status = ContainerStatus(container, False, 37, "waiting", "CrashLoopBackOff", "Error")
        elif fault == IMAGE_PULL:
            status = ContainerStatus(container, False, 0, "waiting", "ImagePullBackOff")
            phase = "Pending"
        elif fault == PENDING and index == 1:
            status, phase, node = ContainerStatus(container), "Pending", ""
        elif self._rng.random() < 0.15:
            status = replace(status, restart_count=self._rng.randint(1, 3), last_terminated_reason="Completed")
        return Pod(
            namespace=spec.namespace,
            name=name,
            phase=phase,
            created=created,
            labels={"app": spec.name, "pod-template-hash": template_hash},
            node=node,
            containers=[status],
        )

    def _create(self, spec: _Spec) -> None:
        key = (spec.namespace, spec.name)
        created = self._boot - timedelta(days=spec.age_days, hours=self._rng.randint(0, 20))
        pod_created = self._boot - timedelta(hours=self._rng.randint(2, 70))
        if spec.fault == IMAGE_PULL:
            created = pod_created = self._boot - timedelta(minutes=48)
        if spec.fault == PENDING:
            pod_created = self._boot - timedelta(minutes=22)
        new_hash, old_hash = self._hash(10), self._hash(10)
        pods = []
        for i in range(spec.replicas):
            stale = spec.rolling and i == spec.replicas - 1
            pods.append(self._pod(spec, old_hash if stale else new_hash, i, pod_created, spec.fault))
        self._faults[key] = spec.fault
        self._pods[key] = pods
        self._deployments[key] = self._deployment_from(spec, created, pods, rolling=spec.rolling)

    def _deployment_from(self, spec: _Spec, created: datetime, pods: list[Pod], rolling: bool = False) -> Deployment:
        ready = sum(1 for p in pods if p.ready_count == len(p.containers))
        conditions = [
            Condition(
                "Available",
                "True" if ready >= max(spec.replicas - 1, 1) or spec.replicas == 0 else "False",
                "MinimumReplicasAvailable" if ready else "MinimumReplicasUnavailable",
            ),
            Condition("Progressing", "True", "NewReplicaSetAvailable"),
        ]
        if spec.fault == IMAGE_PULL:
            conditions[1] = Condition(
                "Progressing",
                "False",
                "ProgressDeadlineExceeded",
                f'ReplicaSet "{spec.name}" has timed out progressing.',
            )
        return Deployment(
            namespace=spec.namespace,
            name=spec.name,
            replicas=spec.replicas,
            ready=ready,
            available=ready,
            updated=spec.replicas - 1 if rolling else spec.replicas,
            created=created,
            selector=LabelSelector(match_labels={"app": spec.name}),
            conditions=conditions,
            images=[spec.image],
        )

    def _spec(self, namespace: str, name: str) -> _Spec:
        spec = next((s for s in _SPECS if s.namespace == namespace and s.name == name), None)
        if spec is None:
            raise BackendError(f'deployments.apps "{name}" not found', 404)
        return spec

    def _find_pod(self, namespace: str, pod: str) -> tuple[_Spec, Pod]:
        for (ns, dep), pods in self._pods.items():
            for p in pods:
                if ns == namespace and p.name == pod:
                    return self._spec(ns, dep), p
        raise BackendError(f'pods "{pod}" not found', 404)

    # ---- Backend contract ---------------------------------------------------

    def fetch(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                deployments=list(self._deployments.values()),
                pods=[p for pods in self._pods.values() for p in pods],
                nodes=list(self._nodes),
                taken_at=datetime.now(UTC),
            )

    def pod_events(self, namespace: str, pod: str) -> list[Event]:
        with self._lock:
            spec, p = self._find_pod(namespace, pod)
        now = datetime.now(UTC)
        image = spec.image
        container = p.containers[0].name
        if p.containers[0].reason == "ImagePullBackOff":
            return [
                Event(
                    "Warning",
                    "BackOff",
                    f'Back-off pulling image "{image}"',
                    196,
                    now - timedelta(seconds=20),
                    "kubelet",
                ),
                Event(
                    "Warning",
                    "Failed",
                    f'Failed to pull image "{image}": rpc error: code = NotFound desc = '
                    f'failed to pull and unpack image "{image}": {image}: not found',
                    12,
                    now - timedelta(minutes=2),
                    "kubelet",
                ),
                Event("Normal", "Pulling", f'Pulling image "{image}"', 12, now - timedelta(minutes=2), "kubelet"),
                Event(
                    "Normal",
                    "Scheduled",
                    f"Successfully assigned {namespace}/{pod} to {p.node}",
                    1,
                    p.created,
                    "default-scheduler",
                ),
            ]
        if p.phase == "Pending":
            return [
                Event(
                    "Warning",
                    "FailedScheduling",
                    "0/6 nodes are available: 1 node(s) had untolerated taint "
                    "{node.kubernetes.io/unreachable: }, 1 node(s) were unschedulable, 4 Insufficient cpu. "
                    "preemption: 0/6 nodes are available: 2 Preemption is not helpful for scheduling, "
                    "4 No preemption victims found for incoming pod.",
                    9,
                    now - timedelta(minutes=1),
                    "default-scheduler",
                ),
                Event(
                    "Normal",
                    "NotTriggerScaleUp",
                    "pod didn't trigger scale-up: 2 max node group size reached",
                    22,
                    now - timedelta(minutes=3),
                    "cluster-autoscaler",
                ),
            ]
        if p.containers[0].reason == "CrashLoopBackOff":
            return [
                Event(
                    "Warning",
                    "BackOff",
                    f"Back-off restarting failed container {container} in pod {pod}",
                    412,
                    now - timedelta(seconds=45),
                    "kubelet",
                ),
                Event(
                    "Normal",
                    "Pulled",
                    f'Container image "{image}" already present on machine',
                    38,
                    now - timedelta(minutes=5),
                    "kubelet",
                ),
                Event("Normal", "Created", f"Created container {container}", 38, now - timedelta(minutes=5), "kubelet"),
                Event("Normal", "Started", f"Started container {container}", 38, now - timedelta(minutes=5), "kubelet"),
            ]
        # Healthy pods: events expire after 1h on a real cluster, so there is usually nothing.
        if (now - p.created) > timedelta(hours=1):
            return []
        return [
            Event("Normal", "Started", f"Started container {container}", 1, p.created, "kubelet"),
            Event("Normal", "Created", f"Created container {container}", 1, p.created, "kubelet"),
            Event("Normal", "Pulled", f'Successfully pulled image "{image}" in 2.1s', 1, p.created, "kubelet"),
            Event(
                "Normal",
                "Scheduled",
                f"Successfully assigned {namespace}/{pod} to {p.node}",
                1,
                p.created,
                "default-scheduler",
            ),
        ]

    def pod_logs(self, namespace: str, pod: str, container: str | None, tail_lines: int, previous: bool) -> str:
        with self._lock:
            spec, p = self._find_pod(namespace, pod)
        c = p.containers[0]
        if c.state == "waiting" and c.reason != "CrashLoopBackOff" and not previous:
            raise BackendError(
                f'container "{c.name}" in pod "{pod}" is waiting to start: {c.reason or "ContainerCreating"}', 400
            )
        if previous and c.restart_count == 0:
            raise BackendError(f'previous terminated container "{c.name}" in pod "{pod}" not found', 400)
        rng = random.Random(pod)
        now = datetime.now(UTC)
        lines = []
        count = min(tail_lines, 60)
        for i in range(count):
            ts = (now - timedelta(seconds=(count - i) * 7)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            if spec.name == "ingress-nginx-controller":
                path = rng.choice(["/", "/api/cart", "/api/products?page=2", "/healthz", "/api/checkout"])
                code = rng.choice([200] * 12 + [201, 304, 404, 502])
                lines.append(
                    f'{ts} 10.0.{rng.randint(1, 3)}.{rng.randint(2, 250)} - - "GET {path} HTTP/1.1" {code} '
                    f"{rng.randint(120, 9000)} {rng.randint(1, 180)}ms"
                )
            else:
                msg = rng.choice(
                    [
                        "GET /healthz 200 0.4ms",
                        f"request completed path=/v1/{spec.name} status=200 duration={rng.randint(4, 90)}ms",
                        f"processed message id={rng.randint(10**6, 10**7)} queue={spec.namespace}-events",
                        f"cache hit ratio={rng.uniform(0.82, 0.99):.2f}",
                        "db pool stats open=12 idle=9 in_use=3",
                    ]
                )
                lines.append(f"{ts} level=info service={spec.namespace}-{spec.name} {msg}")
        if c.reason == "CrashLoopBackOff":
            ts = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            lines = lines[-8:] + [
                f'{ts} level=info msg="connecting to postgres" host=payments-db.payments.svc:5432',
                f'{ts} level=error msg="dial tcp 10.100.71.12:5432: connect: connection refused" attempt=5',
                f'{ts} level=fatal msg="could not connect to database after 5 attempts, exiting"',
                "panic: database unavailable",
                "",
                "goroutine 1 [running]:",
                "main.main()",
                "\t/src/cmd/worker/main.go:58 +0x2a5",
            ]
        return "\n".join(lines[-tail_lines:]) + "\n"

    def rollout_restart(self, namespace: str, name: str) -> str:
        with self._lock:
            spec = self._spec(namespace, name)
            key = (namespace, name)
            now = datetime.now(UTC)
            restarted_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            new_hash = self._hash(10)
            # A restart recreates the pods but fixes nothing that is really broken,
            # exactly like on a real cluster (a missing image stays missing).
            pods = [self._pod(spec, new_hash, i, now, self._faults[key]) for i in range(spec.replicas)]
            old = self._deployments[key]
            dep = self._deployment_from(spec, old.created, pods)
            dep.restarted_at = restarted_at
            self._pods[key] = pods
            self._deployments[key] = dep
            return restarted_at

    def ping(self) -> None:
        return None
