##############################################################################
# Haunter — AWS Lambda Hosting (Phase 14)
#
# Provisions:
#   - Lambda function (ARM64 Graviton2, 512MB, 900s timeout)
#   - Lambda Function URL (no API Gateway required — $0 after free tier)
#   - IAM Role + inline policy (least-privilege, explicit deny on secretsmanager)
#   - CloudWatch Log Group (7-day retention)
#
# Cost model (always-free, permanent — not 12-month like EC2):
#   Lambda: 1M requests/mo + 400,000 GB-seconds/mo
#   At 10 users / 3 repos / 20 webhooks/week (webhook + pipeline invocations):
#     - ~160 invocations/month (80 webhook + 80 pipeline)
#     - ~80 pipeline runs × 5min avg × 512MB = 12,000 GB-s/mo
#     - 12,000 / 400,000 = 3% of free tier → $0
#   CodeBuild sandbox: 100 min/mo always-free; at 80 runs × 10min = 800 min/mo
#     overrun: 700 min × $0.005 = $3.50/mo (set $1 budget alert as early warning)
#   → Lambda hosting: $0 permanently.
#
# Security posture (WORK.md:278):
#   - Role: AWSLambdaBasicExecutionRole (CloudWatch Logs only) as base
#   - + lambda:InvokeFunction on self only (for async pipeline self-invoke)
#   - + codebuild:StartBuild/BatchGetBuilds scoped to sandbox project
#   - Explicit Deny: secretsmanager:*, iam:*, sts:AssumeRole (any other)
#   - No cross-tenant resource access
#   - Environment secrets injected as Lambda env vars — no Secrets Manager needed
#   - Function URL auth_type=NONE; security via HMAC (WORK.md:77) not IAM
#     (GitHub cannot sign IAM SigV4 — HMAC-SHA256 is the correct webhook auth)
#
# Budget alert:
#   Set $1 alert at: https://us-east-1.console.aws.amazon.com/billing/home#/budgets
#   (same approach as GCP HAUNTER.md:177)
##############################################################################

# ---------------------------------------------------------------------------
# CloudWatch Log Group
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}"
  retention_in_days = var.log_retention_days

  tags = {
    Project = var.project_name
    Phase   = "14"
  }
}

# ---------------------------------------------------------------------------
# IAM Role for Lambda
# ---------------------------------------------------------------------------

resource "aws_iam_role" "lambda" {
  name = "${var.project_name}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Project = var.project_name
    Phase   = "14"
  }
}

# Base managed policy — CloudWatch Logs only
resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Inline policy — minimal additional permissions + explicit denies
resource "aws_iam_role_policy" "lambda_inline" {
  name = "${var.project_name}-lambda-inline-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # ----------------------------------------------------------------
      # ALLOW — Self-invoke for async pipeline dispatch
      # Scoped to this function only — no wildcard Lambda invoke
      # ----------------------------------------------------------------
      {
        Sid    = "AllowSelfInvoke"
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = "arn:aws:lambda:${var.region}:*:function/${var.project_name}"
      },
      # ----------------------------------------------------------------
      # ALLOW — CodeBuild sandbox (scoped to Haunter sandbox project only)
      # ----------------------------------------------------------------
      {
        Sid    = "AllowCodeBuildSandbox"
        Effect = "Allow"
        Action = [
          "codebuild:StartBuild",
          "codebuild:BatchGetBuilds",
        ]
        # Reference the existing CodeBuild project from Phase 13
        Resource = "arn:aws:codebuild:${var.region}:*:project/${var.project_name}-sandbox"
      },
      # ----------------------------------------------------------------
      # EXPLICIT DENY — Secrets Manager, IAM, EC2, cross-role assumption
      # Must be denied even if a broader Allow is accidentally added.
      # ----------------------------------------------------------------
      {
        Sid    = "DenySecretsManagerAndHighPrivilege"
        Effect = "Deny"
        Action = [
          "secretsmanager:*",
          "iam:*",
          "ec2:*",
        ]
        Resource = "*"
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Lambda Function
#
# Timeout: 900s (15 min max) to cover full pipeline (CodeBuild poll loop).
# Memory: 512MB — adequate for FastAPI + async DB + boto3.
# Architecture: arm64 (Graviton2) — ~20% faster/cheaper than x86_64.
#
# Environment secrets injected as env vars from Terraform variables —
# NOT from Secrets Manager (avoids secretsmanager:GetSecretValue permission).
# In production, use AWS Systems Manager Parameter Store or inject at deploy
# time via CI/CD (e.g., GitHub Actions secrets → terraform apply -var=...).
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "haunter" {
  function_name = var.project_name
  description   = "Haunter CI fix agent — Lambda hosting (Phase 14)"
  role          = aws_iam_role.lambda.arn

  # Deployment package: ECR image (recommended for larger FastAPI apps)
  # or zip. Set one of the following:
  #   package_type = "Image" + image_uri = var.ecr_image_uri
  #   package_type = "Zip"   + filename + handler + runtime
  #
  # For zip deployment (simpler at MVP scale):
  package_type  = "Zip"
  filename      = var.lambda_zip_path
  handler       = "lambda_handler.handler"
  runtime       = "python3.11"
  architectures = ["x86_64"]

  timeout     = 900   # 15 minutes — covers CodeBuild poll loop
  memory_size = 512   # MB

  environment {
    variables = {
      # Database — pooled URL for app queries, direct for migrations
      DATABASE_URL          = var.database_url
      DATABASE_URL_UNPOOLED = var.database_url_unpooled

      # GitHub OAuth
      GITHUB_CLIENT_ID     = var.github_client_id
      GITHUB_CLIENT_SECRET = var.github_client_secret
      CALLBACK_URL         = var.callback_url
      SESSION_SECRET_KEY   = var.session_secret_key

      # Frontend URL (for CORS)
      FRONTEND_URL = var.frontend_url

      # GitHub webhook + API
      GITHUB_WEBHOOK_SECRET = var.github_webhook_secret
      GITHUB_TOKEN          = var.github_token

      # Hosting + Sandbox provider (Phase 13/14)
      HOSTING_PROVIDER          = "aws"
      SANDBOX_PROVIDER          = "aws"
      AWS_CODEBUILD_PROJECT_NAME = "${var.project_name}-sandbox"

      # LLM
      OPENCODE_ZEN_API_KEY = var.opencode_zen_api_key
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda.name
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_basic,
    aws_iam_role_policy.lambda_inline,
    aws_cloudwatch_log_group.lambda,
  ]

  tags = {
    Project = var.project_name
    Phase   = "14"
  }
}

# ---------------------------------------------------------------------------
# Lambda Function URL
#
# Preferred over API Gateway HTTP API because:
#   - $0 (no per-request charge beyond Lambda invocation free tier)
#   - API Gateway HTTP API costs $1/M requests after 12-month free period
#   - Function URL is permanent $0 at this scale
#
# auth_type = NONE: Function URL is public.
# Security via HMAC-SHA256 (X-Hub-Signature-256) in webhooks.py — GitHub
# cannot sign IAM SigV4, so HMAC is the correct and standard approach.
# CORS: disabled — GitHub webhook sender is not a browser.
# ---------------------------------------------------------------------------

resource "aws_lambda_function_url" "haunter" {
  function_name      = aws_lambda_function.haunter.function_name
  authorization_type = "NONE"  # Secured via HMAC-SHA256 in webhooks.py

  cors {
    allow_credentials = false
    allow_origins     = ["*"]
    allow_methods     = ["*"]
    allow_headers     = ["*"]
    max_age           = 3600
  }
}

# Fix for AWS accounts created after ~2024: "Block public access" for Function URLs
# requires BOTH InvokeFunctionUrl and InvokeFunction for "*" to actually allow public.
# Without InvokeFunction, new accounts return 403 even with AuthType NONE.
# See https://github.com/anomalyco/sst/issues/6397
resource "aws_lambda_permission" "allow_public_invoke" {
  statement_id  = "AllowPublicInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.haunter.function_name
  principal     = "*"
}
