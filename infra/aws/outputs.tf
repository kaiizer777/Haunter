output "codebuild_project_name" {
  description = "Name of the CodeBuild project — set as AWS_CODEBUILD_PROJECT_NAME in backend .env."
  value       = aws_codebuild_project.sandbox.name
}

output "codebuild_project_arn" {
  description = "ARN of the CodeBuild project."
  value       = aws_codebuild_project.sandbox.arn
}

output "iam_role_arn" {
  description = "ARN of the CodeBuild IAM role — use with `aws iam simulate-principal-policy` to verify least-privilege."
  value       = aws_iam_role.codebuild.arn
}

output "ssm_github_token_name" {
  description = "SSM Parameter name holding the GITHUB_TOKEN SecureString."
  value       = aws_ssm_parameter.github_token.name
}

output "cloudwatch_log_group" {
  description = "CloudWatch log group name for CodeBuild build logs."
  value       = aws_cloudwatch_log_group.codebuild.name
}
