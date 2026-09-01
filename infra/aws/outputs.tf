##############################################################################
# Haunter — AWS Infrastructure Outputs
##############################################################################

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
