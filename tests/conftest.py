from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import Condition, ContainerStatus, Deployment, LabelSelector, Node, Pod

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def now() -> datetime:
    return NOW


def make_deployment(name="api", namespace="shop", replicas=2, ready=None, updated=None, conditions=None, **kw):
    ready = replicas if ready is None else ready
    return Deployment(
        namespace=namespace,
        name=name,
        replicas=replicas,
        ready=ready,
        available=ready,
        updated=replicas if updated is None else updated,
        created=NOW - timedelta(days=10),
        selector=kw.pop("selector", LabelSelector(match_labels={"app": name})),
        conditions=conditions or [],
        images=kw.pop("images", [f"repo/{name}:1.0"]),
        **kw,
    )


def make_pod(
    name="api-abc-1",
    namespace="shop",
    app="api",
    ready=True,
    reason="",
    phase="Running",
    restarts=0,
    age=timedelta(hours=1),
    labels=None,
    **kw,
):
    state = "running" if ready else ("waiting" if reason else "running")
    return Pod(
        namespace=namespace,
        name=name,
        phase=phase,
        created=NOW - age,
        labels=labels if labels is not None else {"app": app},
        node=kw.pop("node", "node-1"),
        containers=kw.pop("containers", [ContainerStatus(app, ready, restarts, state, reason)]),
        **kw,
    )


def make_node(name="node-1", ready="True", labels=None, age=timedelta(days=3), extra_conditions=(), **kw):
    return Node(
        name=name,
        created=NOW - age,
        labels=labels
        if labels is not None
        else {
            "node.kubernetes.io/instance-type": "m5.xlarge",
            "eks.amazonaws.com/capacityType": "ON_DEMAND",
            "eks.amazonaws.com/nodegroup": "general",
            "topology.kubernetes.io/zone": "us-east-1a",
        },
        conditions=[Condition("Ready", ready), *extra_conditions],
        **kw,
    )
