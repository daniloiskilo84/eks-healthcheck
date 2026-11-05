"""The real backend's translation from client objects, using the client's own models."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from app.backend import BackendError
from app.k8s import RESTARTED_AT, KubernetesBackend, to_deployment, to_event, to_node, to_pod

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def test_deployment():
    d = client.V1Deployment(
        metadata=client.V1ObjectMeta(name="api", namespace="shop", creation_timestamp=T0),
        spec=client.V1DeploymentSpec(
            replicas=3,
            selector=client.V1LabelSelector(
                match_labels={"app": "api"},
                match_expressions=[client.V1LabelSelectorRequirement(key="tier", operator="In", values=["web"])],
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(annotations={RESTARTED_AT: "2026-11-02T10:00:00Z"}),
                spec=client.V1PodSpec(containers=[client.V1Container(name="api", image="repo/api:1")]),
            ),
        ),
        status=client.V1DeploymentStatus(
            ready_replicas=2,
            available_replicas=2,
            updated_replicas=3,
            conditions=[
                client.V1DeploymentCondition(type="Available", status="True", reason="MinimumReplicasAvailable")
            ],
        ),
    )
    m = to_deployment(d)
    assert (m.namespace, m.name, m.replicas, m.ready, m.updated) == ("shop", "api", 3, 2, 3)
    assert m.selector.match_labels == {"app": "api"}
    assert m.selector.match_expressions[0].values == ("web",)
    assert m.images == ["repo/api:1"]
    assert m.restarted_at == "2026-11-02T10:00:00Z"
    assert m.conditions[0].type == "Available"


def test_deployment_defaults_when_status_is_empty():
    d = client.V1Deployment(
        metadata=client.V1ObjectMeta(name="new", namespace="shop"),
        spec=client.V1DeploymentSpec(
            selector=client.V1LabelSelector(match_labels={"app": "new"}),
            template=client.V1PodTemplateSpec(spec=client.V1PodSpec(containers=[client.V1Container(name="c")])),
        ),
        status=client.V1DeploymentStatus(),
    )
    m = to_deployment(d)
    assert (m.replicas, m.ready, m.available, m.updated, m.restarted_at) == (1, 0, 0, 0, "")


def test_pod_with_crashing_container_and_init():
    p = client.V1Pod(
        metadata=client.V1ObjectMeta(name="api-x", namespace="shop", labels={"app": "api"}, creation_timestamp=T0),
        spec=client.V1PodSpec(
            node_name="node-1", containers=[client.V1Container(name="api"), client.V1Container(name="sidecar")]
        ),
        status=client.V1PodStatus(
            phase="Running",
            init_container_statuses=[
                client.V1ContainerStatus(
                    name="migrate",
                    ready=True,
                    restart_count=0,
                    image="i",
                    image_id="",
                    state=client.V1ContainerState(
                        terminated=client.V1ContainerStateTerminated(exit_code=0, reason="Completed")
                    ),
                )
            ],
            container_statuses=[
                client.V1ContainerStatus(
                    name="api",
                    ready=False,
                    restart_count=12,
                    image="i",
                    image_id="",
                    state=client.V1ContainerState(waiting=client.V1ContainerStateWaiting(reason="CrashLoopBackOff")),
                    last_state=client.V1ContainerState(
                        terminated=client.V1ContainerStateTerminated(exit_code=137, reason="OOMKilled")
                    ),
                )
            ],
        ),
    )
    m = to_pod(p)
    assert m.container_names == ["api", "sidecar"]  # sidecar has no status yet, still listed
    assert m.status_text == "CrashLoopBackOff"
    assert m.restarts == 12
    api = next(c for c in m.containers if c.name == "api")
    assert (api.state, api.reason, api.last_terminated_reason) == ("waiting", "CrashLoopBackOff", "OOMKilled")
    assert m.ready_count == 0 and not m.deleting


def test_pending_pod_without_statuses():
    p = client.V1Pod(
        metadata=client.V1ObjectMeta(name="p", namespace="ns", deletion_timestamp=T0),
        spec=client.V1PodSpec(containers=[client.V1Container(name="c")]),
        status=client.V1PodStatus(phase="Pending"),
    )
    m = to_pod(p)
    assert m.container_names == ["c"] and m.node == "" and m.deleting and m.status_text == "Terminating"


def test_node():
    n = client.V1Node(
        metadata=client.V1ObjectMeta(
            name="ip-10-0-1-1", labels={"node.kubernetes.io/instance-type": "m5.large"}, creation_timestamp=T0
        ),
        spec=client.V1NodeSpec(unschedulable=True),
        status=client.V1NodeStatus(
            allocatable={"cpu": "1930m", "memory": "7291276Ki"},
            conditions=[client.V1NodeCondition(type="Ready", status="True")],
            node_info=client.V1NodeSystemInfo(
                kubelet_version="v1.31.2",
                architecture="amd64",
                boot_id="",
                container_runtime_version="",
                kernel_version="",
                kube_proxy_version="",
                machine_id="",
                operating_system="linux",
                os_image="",
                system_uuid="",
            ),
        ),
    )
    m = to_node(n)
    assert (m.cpu, m.memory, m.kubelet_version, m.unschedulable) == ("1930m", "7291276Ki", "v1.31.2", True)
    assert m.condition("Ready").status == "True"


def test_event():
    e = client.CoreV1Event(
        metadata=client.V1ObjectMeta(name="e"),
        involved_object=client.V1ObjectReference(kind="Pod", name="p"),
        type="Warning",
        reason="BackOff",
        message=" Back-off restarting failed container \n",
        count=7,
        last_timestamp=T0,
        source=client.V1EventSource(component="kubelet"),
    )
    m = to_event(e)
    assert (m.type, m.reason, m.message, m.count, m.source, m.last_seen) == (
        "Warning",
        "BackOff",
        "Back-off restarting failed container",
        7,
        "kubelet",
        T0,
    )


def test_rollout_restart_patches_template_annotation():
    apps = MagicMock()
    backend = KubernetesBackend(MagicMock(), apps, MagicMock())
    restarted_at = backend.rollout_restart("shop", "api")
    name, namespace, body = apps.patch_namespaced_deployment.call_args.args
    assert (name, namespace) == ("api", "shop")
    assert body["spec"]["template"]["metadata"]["annotations"][RESTARTED_AT] == restarted_at


def test_api_errors_become_backend_errors():
    core = MagicMock()
    core.list_namespaced_event.side_effect = ApiException(status=403, reason="Forbidden")
    backend = KubernetesBackend(core, MagicMock(), MagicMock())
    with pytest.raises(BackendError) as exc:
        backend.pod_events("shop", "api-x")
    assert exc.value.status == 403 and "Forbidden" in exc.value.message


def test_events_are_filtered_by_pod_and_newest_first():
    core = MagicMock()
    older = client.CoreV1Event(
        metadata=client.V1ObjectMeta(), involved_object=client.V1ObjectReference(), reason="Old", last_timestamp=T0
    )
    newer = client.CoreV1Event(
        metadata=client.V1ObjectMeta(),
        involved_object=client.V1ObjectReference(),
        reason="New",
        last_timestamp=T0.replace(day=2),
    )
    core.list_namespaced_event.return_value = client.CoreV1EventList(items=[older, newer])
    events = KubernetesBackend(core, MagicMock(), MagicMock()).pod_events("shop", "api-x")
    assert [e.reason for e in events] == ["New", "Old"]
    assert (
        core.list_namespaced_event.call_args.kwargs["field_selector"]
        == "involvedObject.kind=Pod,involvedObject.name=api-x"
    )
