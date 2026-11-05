# `terraform test` with a mocked AWS provider: no account, no credentials, runs in CI.

mock_provider "aws" {
  # Mocks return random strings; IAM resources validate that policies are JSON.
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/github-eks-healthcheck-deploy" }
  }
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = {
      arn            = "arn:aws:ecr:us-east-1:123456789012:repository/eks-healthcheck"
      repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/eks-healthcheck"
    }
  }

  override_data {
    target = data.aws_eks_cluster.this
    values = {
      arn        = "arn:aws:eks:us-east-1:123456789012:cluster/demo"
      vpc_config = [{ endpoint_public_access = true }]
    }
  }
  override_data {
    target = data.aws_partition.current
    values = { partition = "aws" }
  }
}

variables {
  region            = "us-east-1"
  cluster_name      = "demo"
  github_repository = "acme/eks-healthcheck"
}

run "defaults_create_everything" {
  command = apply

  assert {
    condition     = length(aws_iam_openid_connect_provider.github) == 1 && length(aws_ecr_repository.image) == 1
    error_message = "OIDC provider and ECR repository are created by default"
  }

  assert {
    condition     = length(aws_eks_access_entry.deploy) == 1 && length(aws_eks_access_policy_association.deploy) == 1
    error_message = "the deploy role gets an EKS access entry"
  }

  assert {
    condition     = aws_eks_access_policy_association.deploy[0].policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
    error_message = "access policy ARN must use the cluster-access-policy path"
  }

  assert {
    condition = toset(flatten([
      for c in data.aws_iam_policy_document.trust.statement[0].condition : c.values if c.variable == "token.actions.githubusercontent.com:sub"
    ])) == toset(["repo:acme/eks-healthcheck:ref:refs/heads/main", "repo:acme/eks-healthcheck:environment:production"])
    error_message = "only the main branch and the production environment of this repository may assume the role"
  }

  assert {
    condition     = strcontains(output.github_setup, "ECR_REPOSITORY") && strcontains(output.github_setup, "--repo acme/eks-healthcheck")
    error_message = "github_setup prints the variables for the fork"
  }
}

run "ghcr_instead_of_ecr" {
  command = apply

  variables {
    create_ecr_repository = false
  }

  assert {
    condition     = length(aws_ecr_repository.image) == 0 && output.ecr_repository_url == ""
    error_message = "no ECR repository when GHCR is used"
  }

  assert {
    condition     = !strcontains(output.github_setup, "ECR_REPOSITORY")
    error_message = "ECR_REPOSITORY must not be set when GHCR is used"
  }

  assert {
    condition     = length([for s in data.aws_iam_policy_document.deploy.statement : s if startswith(s.sid, "Ecr")]) == 0
    error_message = "the role gets no ECR permissions without ECR"
  }
}

run "existing_oidc_provider_and_aws_auth" {
  command = apply

  variables {
    create_github_oidc_provider = false
    manage_eks_access           = false
  }

  override_data {
    target = data.aws_iam_openid_connect_provider.github
    values = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }

  assert {
    condition     = length(aws_iam_openid_connect_provider.github) == 0 && length(aws_eks_access_entry.deploy) == 0
    error_message = "reuses the account's OIDC provider and leaves cluster access to aws-auth"
  }
}

run "rejects_bad_repository_name" {
  command = plan

  variables {
    github_repository = "not-a-repo"
  }

  expect_failures = [var.github_repository]
}
