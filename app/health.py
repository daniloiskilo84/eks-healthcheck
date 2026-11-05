"""Health rules: when is a deployment or a node OK, and why is it not.

Pure functions over app.models, so every rule is unit-tested without a cluster.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from .models import Deployment, LabelSelector, Node, Pod, Snapshot

OK = "OK"
NOK = "NOK"

# Container waiting reasons that never fix themselves without someone acting.
BAD_WAITING_REASONS = frozenset(
    {
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "InvalidImageName",
        "CreateContainerConfigError",
        "CreateContainerError",
        "RunContainerError",
    }
)

PRESSURE_CONDITIONS = ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")

INSTANCE_TYPE_LABELS = ("node.kubernetes.io/instance-type", "beta.kubernetes.io/instance-type")
CAPACITY_LABELS = ("eks.amazonaws.com/capacityType", "karpenter.sh/capacity-type")
NODEGROUP_LABELS = ("eks.amazonaws.com/nodegroup", "karpenter.sh/nodepool", "alpha.eksctl.io/nodegroup-name")
ZONE_LABELS = ("topology.kubernetes.io/zone", "failure-domain.beta.kubernetes.io/zone")


@dataclass
class DeploymentHealth:
    deployment: Deployment
    status: str
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    pods: list[Pod] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def restarts(self) -> int:
        return sum(p.restarts for p in self.pods)


@dataclass
class NodeHealth:
    node: Node
    status: str
    reasons: list[str]
    notes: list[str]
    instance_type: str
    family: str
    capacity_type: str
    zone: str
    nodegroup: str
    uptime_seconds: float
    pod_count: int = 0

    @property
    def ok(self) -> bool:
        return self.status == OK


@dataclass
class ClusterSummary:
    deployments_total: int
    deployments_ok: int
    deployments_nok: int
    nodes_total: int
    nodes_ready: int
    pods_by_phase: dict[str, int]
    problem_pods: int
    restarts: int
    families: dict[str, int]
    nok_deployments: list[DeploymentHealth]
    nok_nodes: list[NodeHealth]
    taken_at: datetime

    @property
    def healthy(self) -> bool:
        return not self.nok_deployments and not self.nok_nodes


def selector_matches(selector: LabelSelector | None, labels: dict[str, str]) -> bool:
    """Same semantics as a Kubernetes label selector; an empty selector selects nothing."""
    if selector is None or (not selector.match_labels and not selector.match_expressions):
        return False
    for key, value in selector.match_labels.items():
        if labels.get(key) != value:
            return False
    for req in selector.match_expressions:
        present = req.key in labels
        if req.operator == "In" and not (present and labels[req.key] in req.values):
            return False
        if req.operator == "NotIn" and present and labels[req.key] in req.values:
            return False
        if req.operator == "Exists" and not present:
            return False
        if req.operator == "DoesNotExist" and present:
            return False
    return True


def pod_problems(pod: Pod, now: datetime, pending_grace_seconds: int) -> list[str]:
    """Reasons this pod needs attention (empty when it is fine or still starting)."""
    if pod.deleting:
        return []
    found: list[str] = []
    for c in pod.containers:
        if c.state == "waiting" and c.reason in BAD_WAITING_REASONS:
            found.append(c.reason)
        elif c.last_terminated_reason == "OOMKilled" and not c.ready:
            found.append("OOMKilled")
    if pod.phase == "Failed":
        found.append(pod.reason or "Failed")
    if pod.phase == "Pending" and not found and (now - pod.created).total_seconds() > pending_grace_seconds:
        found.append("Pending")
    return list(dict.fromkeys(found))


def evaluate_deployment(
    dep: Deployment, pods: list[Pod], now: datetime, pending_grace_seconds: int = 300
) -> DeploymentHealth:
    owned = [p for p in pods if p.namespace == dep.namespace and selector_matches(dep.selector, p.labels)]
    reasons: list[str] = []

    if dep.replicas > 0 and dep.ready < dep.replicas:
        reasons.append(f"{dep.ready}/{dep.replicas} replicas ready")
    for cond in dep.conditions:
        if cond.type == "Progressing" and cond.status == "False":
            reasons.append(cond.reason or "ProgressDeadlineExceeded")
        elif cond.type == "ReplicaFailure" and cond.status == "True":
            reasons.append(cond.reason or "ReplicaFailure")

    # Pods can be broken while the deployment still looks "ready": a rollout whose
    # new pods crash keeps the old ones serving and stalls forever.
    problems = Counter(r for p in owned for r in pod_problems(p, now, pending_grace_seconds))
    for reason, count in problems.most_common():
        reasons.append(f"{count} pod{'s' if count > 1 else ''} {reason}")

    notes: list[str] = []
    if dep.replicas == 0:
        notes.append("scaled to zero")
    elif dep.updated < dep.replicas:
        notes.append("rollout in progress")

    owned.sort(key=lambda p: (not pod_problems(p, now, pending_grace_seconds), p.name))
    return DeploymentHealth(dep, NOK if reasons else OK, reasons, notes, owned)


def _first_label(labels: dict[str, str], keys: tuple[str, ...], default: str = "-") -> str:
    return next((labels[k] for k in keys if labels.get(k)), default)


def evaluate_node(node: Node, now: datetime, pod_count: int = 0) -> NodeHealth:
    labels = node.labels
    instance_type = _first_label(labels, INSTANCE_TYPE_LABELS)
    family = instance_type.split(".", 1)[0] if "." in instance_type else "-"
    capacity = _first_label(labels, CAPACITY_LABELS)
    capacity = {"spot": "SPOT", "on-demand": "ON_DEMAND"}.get(capacity, capacity)

    reasons: list[str] = []
    ready = node.condition("Ready")
    if ready is None or ready.status != "True":
        reasons.append("NotReady" if ready is None or ready.status == "False" else "Unknown")
    for name in PRESSURE_CONDITIONS:
        cond = node.condition(name)
        if cond is not None and cond.status == "True":
            reasons.append(name)

    notes = ["cordoned"] if node.unschedulable else []
    return NodeHealth(
        node=node,
        status=NOK if reasons else OK,
        reasons=reasons,
        notes=notes,
        instance_type=instance_type,
        family=family,
        capacity_type=capacity,
        zone=_first_label(labels, ZONE_LABELS),
        nodegroup=_first_label(labels, NODEGROUP_LABELS),
        uptime_seconds=(now - node.created).total_seconds(),
        pod_count=pod_count,
    )


def sort_deployments(items: list[DeploymentHealth]) -> list[DeploymentHealth]:
    """NOK first, then namespace and name: the screen is read top-down."""
    return sorted(items, key=lambda h: (h.ok, h.deployment.namespace, h.deployment.name))


def sort_nodes(items: list[NodeHealth]) -> list[NodeHealth]:
    return sorted(items, key=lambda h: (h.ok, h.nodegroup, h.node.name))


def summarize(
    snapshot: Snapshot, deployments: list[DeploymentHealth], nodes: list[NodeHealth], pending_grace_seconds: int
) -> ClusterSummary:
    phases = Counter(p.phase for p in snapshot.pods if not p.deleting)
    problem_pods = sum(1 for p in snapshot.pods if pod_problems(p, snapshot.taken_at, pending_grace_seconds))
    families = Counter(n.family for n in nodes)
    nok_deps = [d for d in deployments if not d.ok]
    nok_nodes = [n for n in nodes if not n.ok]
    return ClusterSummary(
        deployments_total=len(deployments),
        deployments_ok=len(deployments) - len(nok_deps),
        deployments_nok=len(nok_deps),
        nodes_total=len(nodes),
        nodes_ready=sum(1 for n in nodes if "NotReady" not in n.reasons and "Unknown" not in n.reasons),
        pods_by_phase=dict(phases),
        problem_pods=problem_pods,
        restarts=sum(p.restarts for p in snapshot.pods),
        families=dict(sorted(families.items())),
        nok_deployments=nok_deps,
        nok_nodes=nok_nodes,
        taken_at=snapshot.taken_at,
    )
