"""Plain data structures the UI works with.

Both backends (the real Kubernetes API and the demo cluster) produce these, so the
health rules and the pages never touch Kubernetes client objects directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SelectorRequirement:
    key: str
    operator: str  # In | NotIn | Exists | DoesNotExist
    values: tuple[str, ...] = ()


@dataclass
class LabelSelector:
    match_labels: dict[str, str] = field(default_factory=dict)
    match_expressions: tuple[SelectorRequirement, ...] = ()


@dataclass
class Condition:
    type: str
    status: str  # "True" | "False" | "Unknown", as the API returns it
    reason: str = ""
    message: str = ""


@dataclass
class ContainerStatus:
    name: str
    ready: bool = False
    restart_count: int = 0
    state: str = "waiting"  # running | waiting | terminated
    reason: str = ""
    last_terminated_reason: str = ""
    init: bool = False


@dataclass
class Pod:
    namespace: str
    name: str
    phase: str
    created: datetime
    labels: dict[str, str] = field(default_factory=dict)
    node: str = ""
    reason: str = ""
    containers: list[ContainerStatus] = field(default_factory=list)
    deleting: bool = False

    @property
    def main_containers(self) -> list[ContainerStatus]:
        return [c for c in self.containers if not c.init]

    @property
    def container_names(self) -> list[str]:
        return [c.name for c in self.main_containers]

    @property
    def ready_count(self) -> int:
        return sum(1 for c in self.main_containers if c.ready)

    @property
    def restarts(self) -> int:
        return sum(c.restart_count for c in self.containers)

    @property
    def status_text(self) -> str:
        """What `kubectl get pods` would show in the STATUS column."""
        if self.deleting:
            return "Terminating"
        for c in self.containers:
            if c.state == "waiting" and c.reason:
                return f"Init:{c.reason}" if c.init else c.reason
        for c in self.main_containers:
            if c.state == "terminated" and c.reason:
                return c.reason
        return self.reason or self.phase


@dataclass
class Deployment:
    namespace: str
    name: str
    replicas: int
    ready: int
    available: int
    updated: int
    created: datetime
    selector: LabelSelector
    conditions: list[Condition] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    restarted_at: str = ""


@dataclass
class Node:
    name: str
    created: datetime
    labels: dict[str, str] = field(default_factory=dict)
    conditions: list[Condition] = field(default_factory=list)
    unschedulable: bool = False
    kubelet_version: str = ""
    cpu: str = ""
    memory: str = ""

    def condition(self, type_: str) -> Condition | None:
        return next((c for c in self.conditions if c.type == type_), None)


@dataclass
class Event:
    type: str  # Normal | Warning
    reason: str
    message: str
    count: int
    last_seen: datetime | None
    source: str = ""


@dataclass
class Snapshot:
    deployments: list[Deployment]
    pods: list[Pod]
    nodes: list[Node]
    taken_at: datetime
