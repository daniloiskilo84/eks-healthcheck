# Deploy healthcheck on your own EKS cluster

There is **no shared or public image**. You build this repository's `Dockerfile` and store the image in **your** registry; the pipeline and the chart read where from files in your copy of the code:

| File | What you set |
|---|---|
| [`deploy/pipeline.env`](../deploy/pipeline.env) | your image repository, cluster, region, deploy role, namespace |
| [`deploy/values-production.yaml`](../deploy/values-production.yaml) | app settings: namespaces, rollout, ingress, login, scheduling |

Two ways, pick one:

| | What you get | Time |
|---|---|---|
| **A. Fork + pipeline** (recommended) | Every push to your `main` is tested, built into your registry and deployed to your cluster; no AWS keys stored | ~15 min once |
| **B. Build and Helm from your laptop** | Running quickly; you rebuild and redeploy by hand | ~10 min |

## Before you start

- An EKS cluster you can reach with `kubectl` as an admin.
- `aws` CLI v2, `kubectl`, `helm` ≥ 3.12, Docker with `buildx`; for path A also `terraform` ≥ 1.6 and `gh`.
- For path A, the GitHub runners must reach the cluster API: **public endpoint** (you can restrict it to [GitHub's IP ranges](https://api.github.com/meta)) or a **self-hosted runner** inside the VPC.
- Optional, for a URL instead of port-forward: the [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/).

---

## A. Fork + pipeline

### 1. Fork and clone

```bash
gh repo fork daniloiskilo84/eks-healthcheck --clone
cd eks-healthcheck
```

CI (`ci.yml`) already runs on your fork: lint, tests, chart checks, Terraform tests and the end-to-end test on kind. The deploy workflow only prints *"Pipeline not configured"* until step 3.

### 2. Create the AWS side with Terraform

[`infra/`](../infra) creates only what the pipeline needs, against your **existing** cluster:

| Resource | Why |
|---|---|
| ECR repository (scan on push, keeps the last 30 images) | where **your** builds are stored; EKS nodes already pull from ECR |
| GitHub OIDC provider (skip with `create_github_oidc_provider = false` if the account has one) | GitHub gets short-lived credentials, no access keys |
| IAM role, trusted **only** by your fork's `main` branch and `production` environment | push to that ECR repository and deploy; pull requests can't |
| EKS access entry with cluster-admin for that role | Helm creates a ClusterRole for the app |

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # set region, cluster_name, github_repository
terraform init
terraform apply
```

If the cluster still uses only the `aws-auth` ConfigMap (authentication mode `CONFIG_MAP`), set `manage_eks_access = false` and map the role `deploy_role_arn` in `aws-auth` yourself, or switch the cluster to `API_AND_CONFIG_MAP`.

### 3. Write `deploy/pipeline.env`

Terraform prints it filled in for your account:

```bash
terraform output -raw pipeline_env > ../deploy/pipeline.env
cd ..
cat deploy/pipeline.env
```

```bash
IMAGE_REPOSITORY=123456789012.dkr.ecr.us-east-1.amazonaws.com/eks-healthcheck
EKS_CLUSTER_NAME=prod-eks
AWS_REGION=us-east-1
AWS_DEPLOY_ROLE_ARN=arn:aws:iam::123456789012:role/github-eks-healthcheck-deploy
HELM_NAMESPACE=healthcheck
```

Prefer another registry? Set `IMAGE_REPOSITORY` to it:

| Registry | `IMAGE_REPOSITORY` | Login used by the pipeline |
|---|---|---|
| Amazon ECR | `<account>.dkr.ecr.<region>.amazonaws.com/<name>` | the OIDC role |
| GitHub Container Registry | `ghcr.io/<your-user-or-org>/eks-healthcheck` | the workflow's `GITHUB_TOKEN` (must be your own namespace) |
| Anything else (Docker Hub, Harbor, Artifactory…) | `registry.example.com/team/eks-healthcheck` | secrets `REGISTRY_USERNAME` and `REGISTRY_PASSWORD` |

For GHCR or a private registry, the nodes need pull access too: create a `docker-registry` Secret in the namespace and set `imagePullSecrets` in `values-production.yaml` (or make the GHCR package public).

The role ARN contains your AWS account ID. If your fork is public and you would rather not show it, keep the fork private.

To approve every production deploy by hand, add required reviewers to the `production` environment (Settings → Environments).

### 4. Adjust the app to your environment

Edit [`deploy/values-production.yaml`](../deploy/values-production.yaml): watched namespaces, rollout on/off, internal ALB ingress and host, login, node placement.

### 5. Push

```bash
git add deploy/ && git commit -m "Deploy healthcheck to my cluster"
git push
```

Actions → **ci** runs, then **deploy**:

1. `config` reads `deploy/pipeline.env` and validates it (wrong owner on GHCR, missing role, image with a tag… fail with a clear message);
2. `image` builds amd64 + arm64 and pushes `IMAGE_REPOSITORY:<commit sha>` and `:latest` to your registry;
3. `eks` runs `helm upgrade --atomic` with that exact image (rolls back by itself on failure) and a smoke test.

### 6. Open it

```bash
kubectl -n healthcheck port-forward svc/healthcheck-eks-healthcheck 8080:80
open http://localhost:8080          # TV: http://localhost:8080/tv
```

or the ingress host you configured.

---

## B. Build and Helm from your laptop

```bash
git clone https://github.com/daniloiskilo84/eks-healthcheck && cd eks-healthcheck
export AWS_REGION=us-east-1 CLUSTER=prod-eks
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export REPO=$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/eks-healthcheck TAG=$(git rev-parse --short HEAD)

# 1. your registry
aws ecr create-repository --repository-name eks-healthcheck --image-scanning-configuration scanOnPush=true
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}"

# 2. your build (both architectures, in case you have Graviton nodes)
docker buildx build --platform linux/amd64,linux/arm64 -t "$REPO:$TAG" --push .

# 3. deploy it
aws eks update-kubeconfig --name "$CLUSTER" --region "$AWS_REGION"
helm upgrade --install healthcheck chart/eks-healthcheck \
  --namespace healthcheck --create-namespace \
  -f deploy/values-production.yaml \
  --set clusterName="$CLUSTER" --set image.repository="$REPO" --set image.tag="$TAG" \
  --wait

kubectl -n healthcheck port-forward svc/healthcheck-eks-healthcheck 8080:80
```

The chart refuses to render without `image.repository`: there is no default image to fall back to.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| deploy shows *Pipeline not configured* | `IMAGE_REPOSITORY` in `deploy/pipeline.env` is empty |
| `IMAGE_REPOSITORY points to ghcr.io/<x>, but this repository belongs to <you>` | Use your own GHCR namespace or another registry |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | `github_repository` in terraform.tfvars does not match your fork, or the run is not on `main`/`production` |
| `the server has asked for the client to provide credentials` | The role has no EKS access: check the access entry (step 2) or `aws-auth` |
| deploy hangs on `Kubeconfig`/`helm` | Runners cannot reach a private API endpoint: public endpoint or self-hosted runner |
| Pod `ImagePullBackOff` | The nodes cannot pull from your registry: ECR in another account/region, or GHCR/private registry without `imagePullSecrets` |
| `image.repository is required` from Helm | Set it to your registry (pipeline: `IMAGE_REPOSITORY`) |
| Page says *Kubernetes API 403: Forbidden* | The ServiceAccount lost its ClusterRoleBinding, or `demoMode` is still true |

## Remove it

```bash
helm -n healthcheck uninstall healthcheck && kubectl delete namespace healthcheck
terraform -chdir=infra destroy     # path A; delete the images in ECR first
```
