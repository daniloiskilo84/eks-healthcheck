IMAGE ?= eks-healthcheck:dev
PY    := docker run --rm -v $(CURDIR):/src -w /src python:3.12-slim sh -c

.PHONY: help test lint image demo kind-up kind-deploy e2e kind-down

help: ## Show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

test: ## Lint and run the tests (in a Python 3.12 container)
	$(PY) "pip install -q -r requirements-dev.txt && ruff format --check . && ruff check . && pytest"

image: ## Build the container image
	docker build -t $(IMAGE) .

demo: image ## Run with the fake cluster on http://localhost:8080
	docker run --rm -p 8080:8080 -e DEMO_MODE=true -e CLUSTER_NAME=demo-eks --read-only $(IMAGE)

kind-up: ## Create a local kind cluster
	kind create cluster --name healthcheck

kind-deploy: image ## Install the chart in kind against the real API
	kind load docker-image $(IMAGE) --name healthcheck
	helm upgrade --install hc chart/eks-healthcheck -n healthcheck --create-namespace \
	  --set image.repository=$(word 1,$(subst :, ,$(IMAGE))) --set image.tag=$(word 2,$(subst :, ,$(IMAGE))) \
	  --set image.pullPolicy=Never --set clusterName=kind-local --wait
	@echo "kubectl -n healthcheck port-forward svc/hc-eks-healthcheck 8080:80"

e2e: ## Run the end-to-end checks on the current kube context
	kind load docker-image $(IMAGE) --name healthcheck
	IMAGE=$(IMAGE) bash scripts/e2e-kind.sh

kind-down: ## Delete the kind cluster
	kind delete cluster --name healthcheck
