from datetime import timedelta

import pytest

from app.format import cpu_cores, humanize_duration, memory_gib
from app.health import (
    NOK,
    OK,
    evaluate_deployment,
    evaluate_node,
    pod_problems,
    selector_matches,
    sort_deployments,
)
from app.models import Condition, ContainerStatus, LabelSelector, SelectorRequirement

from .conftest import make_deployment, make_node, make_pod


class TestSelector:
    def test_match_labels_must_all_match(self):
        sel = LabelSelector(match_labels={"app": "api", "tier": "web"})
        assert selector_matches(sel, {"app": "api", "tier": "web", "extra": "x"})
        assert not selector_matches(sel, {"app": "api"})

    def test_empty_selector_matches_nothing(self):
        assert not selector_matches(LabelSelector(), {"app": "api"})
        assert not selector_matches(None, {"app": "api"})

    @pytest.mark.parametrize(
        ("operator", "values", "labels", "expected"),
        [
            ("In", ("a", "b"), {"k": "a"}, True),
            ("In", ("a", "b"), {"k": "c"}, False),
            ("In", ("a",), {}, False),
            ("NotIn", ("a",), {"k": "b"}, True),
            ("NotIn", ("a",), {"k": "a"}, False),
            ("NotIn", ("a",), {}, True),
            ("Exists", (), {"k": ""}, True),
            ("Exists", (), {}, False),
            ("DoesNotExist", (), {}, True),
            ("DoesNotExist", (), {"k": "a"}, False),
        ],
    )
    def test_match_expressions(self, operator, values, labels, expected):
        sel = LabelSelector(match_expressions=(SelectorRequirement("k", operator, values),))
        assert selector_matches(sel, labels) is expected


class TestPodProblems:
    def test_healthy_pod_has_none(self, now):
        assert pod_problems(make_pod(), now, 300) == []

    @pytest.mark.parametrize("reason", ["CrashLoopBackOff", "ImagePullBackOff", "CreateContainerConfigError"])
    def test_stuck_waiting_reasons(self, now, reason):
        assert pod_problems(make_pod(ready=False, reason=reason), now, 300) == [reason]

    def test_container_creating_is_not_a_problem(self, now):
        pod = make_pod(ready=False, reason="ContainerCreating", phase="Pending", age=timedelta(seconds=40))
        assert pod_problems(pod, now, 300) == []

    def test_pending_only_after_grace_period(self, now):
        young = make_pod(ready=False, phase="Pending", age=timedelta(minutes=2))
        old = make_pod(ready=False, phase="Pending", age=timedelta(minutes=10))
        assert pod_problems(young, now, 300) == []
        assert pod_problems(old, now, 300) == ["Pending"]

    def test_oomkilled_while_not_ready(self, now):
        pod = make_pod(containers=[ContainerStatus("api", False, 4, "running", "", "OOMKilled")])
        assert pod_problems(pod, now, 300) == ["OOMKilled"]

    def test_terminating_pod_is_ignored(self, now):
        assert pod_problems(make_pod(ready=False, reason="CrashLoopBackOff", deleting=True), now, 300) == []

    def test_evicted_pod(self, now):
        pod = make_pod(phase="Failed")
        pod.reason = "Evicted"
        assert pod_problems(pod, now, 300) == ["Evicted"]


class TestDeployment:
    def test_all_ready_is_ok(self, now):
        pods = [make_pod("api-1"), make_pod("api-2")]
        h = evaluate_deployment(make_deployment(), pods, now)
        assert h.status == OK and h.reasons == [] and len(h.pods) == 2

    def test_not_enough_ready_replicas(self, now):
        h = evaluate_deployment(make_deployment(replicas=3, ready=1), [], now)
        assert h.status == NOK
        assert h.reasons == ["1/3 replicas ready"]

    def test_crashing_pod_is_nok_even_when_replicas_look_ready(self, now):
        # A stalled rollout: old pods keep serving, the new one crashes forever.
        pods = [make_pod("api-1"), make_pod("api-2"), make_pod("api-3", ready=False, reason="CrashLoopBackOff")]
        h = evaluate_deployment(make_deployment(replicas=2, ready=2, updated=1), pods, now)
        assert h.status == NOK
        assert "1 pod CrashLoopBackOff" in h.reasons
        assert h.pods[0].name == "api-3"  # problem pods are listed first

    def test_problems_are_counted(self, now):
        pods = [make_pod(f"api-{i}", ready=False, reason="ImagePullBackOff") for i in range(3)]
        h = evaluate_deployment(make_deployment(replicas=3, ready=0), pods, now)
        assert h.reasons == ["0/3 replicas ready", "3 pods ImagePullBackOff"]

    def test_progress_deadline_exceeded(self, now):
        cond = Condition("Progressing", "False", "ProgressDeadlineExceeded")
        h = evaluate_deployment(make_deployment(conditions=[cond]), [make_pod("a"), make_pod("b")], now)
        assert h.status == NOK and "ProgressDeadlineExceeded" in h.reasons

    def test_scaled_to_zero_is_ok(self, now):
        h = evaluate_deployment(make_deployment(replicas=0, ready=0), [], now)
        assert h.status == OK and h.notes == ["scaled to zero"]

    def test_rollout_in_progress_is_a_note_not_a_failure(self, now):
        h = evaluate_deployment(make_deployment(replicas=3, updated=2), [], now)
        assert h.status == OK and h.notes == ["rollout in progress"]

    def test_pods_of_other_namespace_or_app_are_ignored(self, now):
        pods = [
            make_pod("other-ns", namespace="other", ready=False, reason="CrashLoopBackOff"),
            make_pod("other-app", app="web", ready=False, reason="CrashLoopBackOff"),
        ]
        h = evaluate_deployment(make_deployment(), pods, now)
        assert h.status == OK and h.pods == []

    def test_sort_puts_nok_first(self, now):
        ok = evaluate_deployment(make_deployment("a", replicas=0), [], now)
        nok = evaluate_deployment(make_deployment("z", replicas=1, ready=0), [], now)
        assert [h.deployment.name for h in sort_deployments([ok, nok])] == ["z", "a"]


class TestNode:
    def test_ready_node_and_labels(self, now):
        h = evaluate_node(make_node(age=timedelta(days=2, hours=3)), now, pod_count=7)
        assert h.status == OK
        assert (h.instance_type, h.family, h.capacity_type, h.nodegroup, h.zone) == (
            "m5.xlarge",
            "m5",
            "ON_DEMAND",
            "general",
            "us-east-1a",
        )
        assert humanize_duration(h.uptime_seconds) == "2d 3h"
        assert h.pod_count == 7

    def test_karpenter_labels(self, now):
        labels = {
            "node.kubernetes.io/instance-type": "c7g.large",
            "karpenter.sh/capacity-type": "spot",
            "karpenter.sh/nodepool": "default",
        }
        h = evaluate_node(make_node(labels=labels), now)
        assert (h.family, h.capacity_type, h.nodegroup, h.zone) == ("c7g", "SPOT", "default", "-")

    @pytest.mark.parametrize(("ready", "reason"), [("False", "NotReady"), ("Unknown", "Unknown")])
    def test_not_ready(self, now, ready, reason):
        h = evaluate_node(make_node(ready=ready), now)
        assert h.status == NOK and h.reasons == [reason]

    def test_pressure_conditions(self, now):
        node = make_node(extra_conditions=[Condition("DiskPressure", "True"), Condition("MemoryPressure", "False")])
        h = evaluate_node(node, now)
        assert h.status == NOK and h.reasons == ["DiskPressure"]

    def test_cordoned_is_a_note(self, now):
        h = evaluate_node(make_node(unschedulable=True), now)
        assert h.status == OK and h.notes == ["cordoned"]


@pytest.mark.parametrize(
    ("seconds", "text"), [(5, "5s"), (125, "2m"), (3 * 3600 + 20 * 60, "3h 20m"), (12 * 86400 + 4 * 3600, "12d 4h")]
)
def test_humanize_duration(seconds, text):
    assert humanize_duration(seconds) == text


def test_quantities():
    assert memory_gib("16073824Ki") == "15.3 GiB"
    assert memory_gib("2Gi") == "2.0 GiB"
    assert memory_gib("") == "-"
    assert cpu_cores("3920m") == "3.92"
    assert cpu_cores("4") == "4"
