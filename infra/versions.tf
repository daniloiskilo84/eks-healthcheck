terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }

  # Local state is fine for this one-off bootstrap. To share it with a team, add e.g.:
  # backend "s3" { bucket = "my-tf-state" key = "eks-healthcheck/bootstrap.tfstate" region = "us-east-1" use_lockfile = true }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = merge({ "app.kubernetes.io/name" = "eks-healthcheck", "managed-by" = "terraform" }, var.tags)
  }
}
