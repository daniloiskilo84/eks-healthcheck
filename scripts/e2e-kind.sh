#!/usr/bin/env bash
# End-to-end test against a REAL Kubernetes API (a kind cluster in CI).
# Installs the chart, deploys a healthy and a broken workload, then checks what the
# app reports, that rollout restart really patches the deployment, and that RBAC
# does not let the app do more than it should.
set -euo pipefail

IMAGE="${IMAGE:-eks-healthcheck:e2e}"
NS=healthcheck
RELEASE=hc
SVC="$RELEASE-eks-healthcheck"
SA="system:serviceaccount:$NS:$SVC"
URL=http://localhost:18080

step() { printf '\n==> %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

step "deploy test workloads"
kubectl apply -f tests/e2e/workloads.yaml

step "install chart with image $IMAGE"
helm upgrade --install "$RELEASE" chart/eks-healthcheck -n "$NS" --create-namespace \
  --set image.repository="${IMAGE%:*}" --set image.tag="${IMAGE##*:}" --set image.pullPolicy=Never \
  --set clusterName=kind-e2e --set cacheTtlSeconds=0 --set pendingGraceSeconds=30 \
  --wait --timeout 3m
kubectl -n e2e rollout status deploy/good --timeout=180s

kubectl -n "$NS" port-forward "svc/$SVC" 18080:80 >/tmp/port-forward.log 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
for _ in $(seq 30); do curl -sf "$URL/readyz" >/dev/null && break; sleep 2; done
curl -sf "$URL/readyz" >/dev/null || fail "app never became ready"

step "healthy deployment is OK"
for _ in $(seq 30); do
  [ "$(curl -sf "$URL/api/deployments/e2e/good" | jq -r .status)" = OK ] && break; sleep 3
done
curl -sf "$URL/api/deployments/e2e/good" | jq -e '.status == "OK" and (.pods | length) == 2' >/dev/null \
  || fail "good should be OK with 2 pods: $(curl -s "$URL/api/deployments/e2e/good")"

step "broken deployment is NOK with an image pull reason"
for _ in $(seq 40); do
  curl -sf "$URL/api/deployments/e2e/broken" \
    | jq -e '.status == "NOK" and (.reasons | any(test("ImagePull|ErrImagePull")))' >/dev/null && break
  sleep 3
done
curl -sf "$URL/api/deployments/e2e/broken" | jq -e '.reasons | any(test("ImagePull|ErrImagePull"))' >/dev/null \
  || fail "broken should report the image pull: $(curl -s "$URL/api/deployments/e2e/broken")"
curl -sf "$URL/api/summary" | jq -e '.healthy == false and .deployments.nok >= 1' >/dev/null || fail "summary"

step "events and logs come from the API"
BROKEN_POD=$(curl -sf "$URL/api/deployments/e2e/broken" | jq -r '.pods[0].name')
curl -sf "$URL/api/pods/e2e/$BROKEN_POD/events" | jq -e 'any(.reason == "Failed" or .reason == "BackOff")' >/dev/null \
  || fail "expected pull failure events"
GOOD_POD=$(curl -sf "$URL/api/deployments/e2e/good" | jq -r '.pods[0].name')
curl -sf "$URL/api/pods/e2e/$GOOD_POD/logs?tail=20" >/dev/null || fail "logs of a running pod"

step "rollout restart needs the CSRF header"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL/api/deployments/e2e/good/rollout")
[ "$CODE" = 403 ] || fail "rollout without header returned $CODE"

step "rollout restart patches the pod template and the rollout completes"
RESTARTED=$(curl -sf -X POST -H 'X-Requested-With: healthcheck' "$URL/api/deployments/e2e/good/rollout" | jq -r .restartedAt)
ANNOTATION=$(kubectl -n e2e get deploy good -o jsonpath='{.spec.template.metadata.annotations.kubectl\.kubernetes\.io/restartedAt}')
[ "$RESTARTED" = "$ANNOTATION" ] || fail "annotation $ANNOTATION != $RESTARTED"
kubectl -n e2e rollout status deploy/good --timeout=180s

step "RBAC is least privilege"
kubectl auth can-i patch deployments --as="$SA" -n e2e | grep -q yes || fail "should patch deployments"
for verb_res in "delete pods" "create deployments" "get secrets" "delete deployments" "patch nodes"; do
  # shellcheck disable=SC2086
  if kubectl auth can-i $verb_res --as="$SA" -A | grep -q yes; then fail "must not: $verb_res"; fi
done

step "pages render"
for page in /deployments /deployments/e2e/broken /nodes /tv; do
  curl -sf "$URL$page" | grep -q kind-e2e || fail "page $page"
done

printf '\nAll end-to-end checks passed.\n'
