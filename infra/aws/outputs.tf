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

# ---------------------------------------------------------------------------
# Lambda outputs (Phase 14)
# ---------------------------------------------------------------------------

output "lambda_function_name" {
  description = "Lambda function name — set as AWS_LAMBDA_FUNCTION_NAME in .env for local testing."
  value       = aws_lambda_function.haunter.function_name
}

output "lambda_function_arn" {
  description = "Lambda function ARN — used for IAM simulate-principal-policy verification."
  value       = aws_lambda_function.haunter.arn
}

output "lambda_function_url" {
  description = "Lambda Function URL — use as WEBHOOK_URL in GitHub repo settings. POST /webhooks/github."
  value       = aws_lambda_function_url.haunter.function_url
}

output "lambda_iam_role_arn" {
  description = "Lambda execution role ARN — verify with `aws iam simulate-principal-policy --action-names secretsmanager:GetSecretValue` (must return DENY)."
  value       = aws_iam_role.lambda.arn
}

output "lambda_cloudwatch_log_group" {
  description = "CloudWatch log group name for Lambda function logs."
  value       = aws_cloudwatch_log_group.lambda.name
}
