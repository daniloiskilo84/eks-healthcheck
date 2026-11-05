output "deploy_role_arn" {
  description = "IAM role assumed by GitHub Actions."
  value       = aws_iam_role.deploy.arn
}

output "ecr_repository_url" {
  description = "Where the pipeline pushes the image (empty when using GHCR)."
  value       = var.create_ecr_repository ? aws_ecr_repository.image[0].repository_url : ""
}

output "cluster_endpoint_public" {
  description = "GitHub-hosted runners need the public endpoint (restrict it with CIDRs) or a self-hosted runner inside the VPC."
  value       = data.aws_eks_cluster.this.vpc_config[0].endpoint_public_access
}

output "github_setup" {
  description = "Run these once to configure your fork."
  value = join("\n", compact([
    "gh variable set AWS_REGION --repo ${var.github_repository} --body ${var.region}",
    "gh variable set EKS_CLUSTER_NAME --repo ${var.github_repository} --body ${var.cluster_name}",
    "gh variable set AWS_DEPLOY_ROLE_ARN --repo ${var.github_repository} --body ${aws_iam_role.deploy.arn}",
    var.create_ecr_repository ? "gh variable set ECR_REPOSITORY --repo ${var.github_repository} --body ${aws_ecr_repository.image[0].repository_url}" : "",
  ]))
}
