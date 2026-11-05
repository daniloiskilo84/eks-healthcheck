# Deploy healthcheck on your own EKS cluster

Two ways, pick one:

| | What you get | Time |
|---|---|---|
| **A. Fork + pipeline** (recommended) | Every push to your `main` is tested and deployed to your cluster, image in your ECR, no AWS keys stored | ~15 min once |
| **B. Helm from your laptop** | Running in a few minutes, you redeploy by hand | ~5 min |

## Before you start

- An EKS cluster you can reach with `kubectl` as an admin.
- `aws` CLI v2, `kubectl`, `helm` ≥ 3.12, and for path A `terraform` ≥ 1.6 and `gh`.
- For path A, the GitHub runners must reach the cluster API: **public endpoint** (you can restrict it to [GitHub's IP ranges](https://api.github.com/meta)) or a **self-hosted runner** inside the VPC.
- Optional, for a URL instead of port-forward: the [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/).

---

## A. Fork + pipeline

### 1. Fork and clone

```bash
gh repo fork daniloiskilo84/eks-healthcheck --clone
cd eks-healthcheck
```

The CI (`ci.yml`) already works on your fork: lint, tests, chart checks and the end-to-end test on kind. The deploy job is skipped until step 3.

### 2. Create the AWS side with Terraform

[`infra/`](../infra) creates only what the pipeline needs, against your **existing** cluster:

| Resource | Why |
|---|---|
| GitHub OIDC provider (skip with `create_github_oidc_provider = false` if you already have one) | GitHub gets short-lived credentials, no access keys |
| IAM role, trusted **only** by your repository's `main` branch and `production` environment | Pull requests cannot deploy |
| ECR repository (scan on push, keeps the last 30 images) | Your nodes already pull from ECR |
| EKS access entry with cluster-admin for that role | Helm creates a ClusterRole for the app |

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # set region, cluster_name, github_repository
terraform init
terraform apply
```

If your cluster still uses only the `aws-auth` ConfigMap (authentication mode `CONFIG_MAP`), set `manage_eks_access = false` and map the role `deploy_role_arn` in `aws-auth` yourself, or switch the cluster to `API_AND_CONFIG_MAP`.

### 3. Point your fork at your cluster

Terraform prints the exact commands:

```bash
terraform output -raw github_setup | sh
cd ..
```

They set the repository variables `AWS_REGION`, `EKS_CLUSTER_NAME`, `AWS_DEPLOY_ROLE_ARN` and `ECR_REPOSITORY`. Optional: `HELM_NAMESPACE` (default `healthcheck`).

To approve every production deploy by hand, add required reviewers to the `production` environment (Settings → Environments).

### 4. Adjust to your environment

Edit [`deploy/values-production.yaml`](../deploy/values-production.yaml): watched namespaces, rollout on/off, internal ALB ingress and host, login, node placement.

### 5. Push

```bash
git commit -am "Configure healthcheck for my cluster"
git push
```

Actions → **ci** runs, then **deploy** builds the image into your ECR and runs `helm upgrade --atomic` on your cluster. If anything fails, Helm rolls back by itself. The run summary shows the image and namespace.

### 6. Open it

```bash
kubectl -n healthcheck port-forward svc/healthcheck-eks-healthcheck 8080:80
open http://localhost:8080          # TV: http://localhost:8080/tv
```

or the ingress host you configured.

---

## B. Helm from your laptop

Uses the public image `ghcr.io/daniloiskilo84/eks-healthcheck` (amd64 + arm64).

```bash
git clone https://github.com/daniloiskilo84/eks-healthcheck && cd eks-healthcheck
aws eks update-kubeconfig --name <cluster> --region <region>

helm upgrade --install healthcheck chart/eks-healthcheck \
  --namespace healthcheck --create-namespace \
  -f deploy/values-production.yaml \
  --set clusterName=<cluster> \
  --set image.tag=latest --set image.pullPolicy=Always \
  --wait

kubectl -n healthcheck port-forward svc/healthcheck-eks-healthcheck 8080:80
```

To use your own build: `docker buildx build --platform linux/amd64,linux/arm64 -t <your-registry>/eks-healthcheck:1 --push .` and add `--set image.repository=<your-registry>/eks-healthcheck --set image.tag=1`.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| deploy job: `Not authorized to perform sts:AssumeRoleWithWebIdentity` | `github_repository` in terraform.tfvars does not match your fork, or the job runs on another branch/environment |
| deploy job: `the server has asked for the client to provide credentials` | The role has no EKS access: check the access entry (step 2) or `aws-auth` |
| deploy job hangs on `Kubeconfig`/`helm` | Runners cannot reach a private API endpoint: public endpoint or self-hosted runner |
| Pod `ImagePullBackOff` with GHCR | The GHCR package is private: make it public (package settings) or use ECR |
| Page says *Kubernetes API 403: Forbidden* | The ServiceAccount lost its ClusterRoleBinding, or `demoMode` is still true |
| Page shows "demo data" | `demoMode: true` in your values |

## Remove it

```bash
helm -n healthcheck uninstall healthcheck && kubectl delete namespace healthcheck
terraform -chdir=infra destroy     # path A only; empty the ECR repository first
```
