variable "region" {
  description = "AWS region of the EKS cluster."
  type        = string
}

variable "cluster_name" {
  description = "Name of the existing EKS cluster to deploy healthcheck into."
  type        = string
}

variable "github_repository" {
  description = "Your fork, as owner/name (e.g. acme/eks-healthcheck). Only this repository can assume the deploy role."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use the owner/name format, e.g. acme/eks-healthcheck."
  }
}

variable "github_branch" {
  description = "Branch whose pipeline may push images."
  type        = string
  default     = "main"
}

variable "github_environment" {
  description = "GitHub environment used by the deploy job (add required reviewers to it to gate deploys)."
  type        = string
  default     = "production"
}

variable "create_github_oidc_provider" {
  description = "false if the account already has the token.actions.githubusercontent.com OIDC provider (only one per account is allowed)."
  type        = bool
  default     = true
}

variable "role_name" {
  description = "Name of the IAM role GitHub Actions assumes."
  type        = string
  default     = "github-eks-healthcheck-deploy"
}

variable "create_ecr_repository" {
  description = "Create a private ECR repository for the image (recommended on EKS: nodes already pull from ECR). false = publish to GHCR instead."
  type        = bool
  default     = true
}

variable "ecr_repository_name" {
  description = "Name of the ECR repository."
  type        = string
  default     = "eks-healthcheck"
}

variable "manage_eks_access" {
  description = "Create the EKS access entry for the deploy role. Needs the cluster authentication mode API or API_AND_CONFIG_MAP; set false if you map the role in aws-auth yourself."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Extra tags for every resource."
  type        = map(string)
  default     = {}
}
