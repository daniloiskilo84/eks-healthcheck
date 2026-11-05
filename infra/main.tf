# Everything GitHub Actions needs to build and deploy healthcheck into an EXISTING
# EKS cluster, with short-lived credentials only (no access keys anywhere).

data "aws_partition" "current" {}

data "aws_eks_cluster" "this" {
  name = var.cluster_name
}

# ---- GitHub OIDC provider (one per AWS account) ------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

locals {
  oidc_provider_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
  ecr_repository    = var.create_ecr_repository ? aws_ecr_repository.image[0] : null
}

# ---- deploy role -------------------------------------------------------------------

data "aws_iam_policy_document" "trust" {
  statement {
    sid     = "GitHubActionsFromThisRepositoryOnly"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # The image job runs on the branch; the deploy job runs in the environment.
    # Pull requests and other branches cannot get these credentials.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repository}:ref:refs/heads/${var.github_branch}",
        "repo:${var.github_repository}:environment:${var.github_environment}",
      ]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = var.role_name
  description          = "GitHub Actions (${var.github_repository}) deploys eks-healthcheck to ${var.cluster_name}"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "Kubeconfig"
    actions   = ["eks:DescribeCluster"]
    resources = [data.aws_eks_cluster.this.arn]
  }

  dynamic "statement" {
    for_each = var.create_ecr_repository ? [1] : []
    content {
      sid       = "EcrLogin"
      actions   = ["ecr:GetAuthorizationToken"]
      resources = ["*"]
    }
  }

  dynamic "statement" {
    for_each = var.create_ecr_repository ? [1] : []
    content {
      sid = "EcrPushThisRepositoryOnly"
      actions = [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:CompleteLayerUpload",
        "ecr:GetDownloadUrlForLayer",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
      ]
      resources = [local.ecr_repository.arn]
    }
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# ---- image registry ----------------------------------------------------------------

resource "aws_ecr_repository" "image" {
  count                = var.create_ecr_repository ? 1 : 0
  name                 = var.ecr_repository_name
  image_tag_mutability = "MUTABLE" # the pipeline also moves the "latest" tag

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "image" {
  count      = var.create_ecr_repository ? 1 : 0
  repository = aws_ecr_repository.image[0].name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 30 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 30 }
      action       = { type = "expire" }
    }]
  })
}

# ---- access to the cluster ---------------------------------------------------------

resource "aws_eks_access_entry" "deploy" {
  count         = var.manage_eks_access ? 1 : 0
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.deploy.arn
  type          = "STANDARD"
}

# The chart creates a ClusterRole and a ClusterRoleBinding, which needs cluster admin.
# The app itself runs with its own least-privilege ServiceAccount, not with this role.
resource "aws_eks_access_policy_association" "deploy" {
  count         = var.manage_eks_access ? 1 : 0
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.deploy.arn
  policy_arn    = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.deploy]
}
