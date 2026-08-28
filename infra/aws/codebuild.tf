##############################################################################
# Haunter — AWS CodeBuild Sandbox (Phase 13)
#
# Provisions:
#   - SSM SecureString for GITHUB_TOKEN (never in CodeBuild env directly)
#   - IAM Role + inline policy (least-privilege, explicit deny on secretsmanager)
#   - CloudWatch Log Group (7-day retention)
#   - CodeBuild Project (general1.small EC2 — NOT Lambda; DinD supported)
#
# Security posture:
#   - Role can ONLY: StartBuild, BatchGetBuilds, CreateLogGroup,
#     CreateLogStream, PutLogEvents, ssm:GetParameters (scoped resource)
#   - Explicit Deny: secretsmanager:*, iam:*, ec2:*, sts:AssumeRole (any other)
#   - No cross-tenant resource access — project is scoped to Haunter builds only
#   - privileged_mode = true  (required for DinD when repo has Dockerfile; HAUNTER.md:131)
##############################################################################

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

locals {
  name = var.project_name
}

# ---------------------------------------------------------------------------
# SSM Parameter — GITHUB_TOKEN
# ---------------------------------------------------------------------------

resource "aws_ssm_parameter" "github_token" {
  name        = "/${local.name}/GITHUB_TOKEN"
  description = "GitHub token for Haunter CodeBuild sandbox repo cloning."
  type        = "SecureString"
  value       = var.github_token

  tags = {
    Project = local.name
    Phase   = "13"
  }
}

# ---------------------------------------------------------------------------
# CloudWatch Log Group
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "codebuild" {
  name              = "/aws/codebuild/${local.name}-sandbox"
  retention_in_days = var.log_retention_days

  tags = {
    Project = local.name
    Phase   = "13"
  }
}

# ---------------------------------------------------------------------------
# IAM Role for CodeBuild
# ---------------------------------------------------------------------------

resource "aws_iam_role" "codebuild" {
  name = "${local.name}-codebuild-sandbox-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "codebuild.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Project = local.name
    Phase   = "13"
  }
}

resource "aws_iam_role_policy" "codebuild_least_privilege" {
  name = "${local.name}-codebuild-policy"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # ----------------------------------------------------------------
      # ALLOW — CodeBuild self-management (scoped to own project)
      # ----------------------------------------------------------------
      {
        Sid    = "AllowCodeBuildSelf"
        Effect = "Allow"
        Action = [
          "codebuild:StartBuild",
          "codebuild:BatchGetBuilds",
        ]
        Resource = "arn:aws:codebuild:${var.region}:*:project/${local.name}-sandbox"
      },
      # ----------------------------------------------------------------
      # ALLOW — CloudWatch Logs (scoped to Haunter log group only)
      # ----------------------------------------------------------------
      {
        Sid    = "AllowCWLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = [
          aws_cloudwatch_log_group.codebuild.arn,
          "${aws_cloudwatch_log_group.codebuild.arn}:*",
        ]
      },
      # ----------------------------------------------------------------
      # ALLOW — SSM GetParameters (GITHUB_TOKEN only, exact resource)
      # ----------------------------------------------------------------
      {
        Sid    = "AllowSSMGithubToken"
        Effect = "Allow"
        Action = [
          "ssm:GetParameters",
        ]
        Resource = aws_ssm_parameter.github_token.arn
      },
      # ----------------------------------------------------------------
      # EXPLICIT DENY — secretsmanager, IAM, EC2, cross-role assumption
      # These must be denied even if a broader Allow is added by mistake.
      # ----------------------------------------------------------------
      {
        Sid    = "DenySecretsManagerAndHighPrivilege"
        Effect = "Deny"
        Action = [
          "secretsmanager:*",
          "iam:*",
          "ec2:*",
          "sts:AssumeRole",
        ]
        Resource = "*"
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# CodeBuild Project
# ---------------------------------------------------------------------------

resource "aws_codebuild_project" "sandbox" {
  name          = "${local.name}-sandbox"
  description   = "Haunter automated patch verification sandbox (Phase 13)."
  service_role  = aws_iam_role.codebuild.arn
  build_timeout = 10  # minutes — hard cap; mirrors Cloud Build 600s timeout
  queued_timeout = 5  # minutes — limits queue cost in case of burst

  # Buildspec is supplied per-build via buildspecOverride in start_build call.
  # The static source here is a placeholder so the project can be created.
  source {
    type      = "NO_SOURCE"
    buildspec = "version: 0.2\nphases:\n  build:\n    commands:\n      - echo 'placeholder — override via buildspecOverride'\n"
  }

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    type                        = "LINUX_CONTAINER"
    image                       = "aws/codebuild/standard:7.0"  # Ubuntu 22.04, Python 3.12, Node 20
    compute_type                = "BUILD_GENERAL1_SMALL"         # EC2 — 3 GB RAM, 2 vCPU; free tier eligible
    privileged_mode             = true                           # required for DinD: docker build/run when repo has Dockerfile (HAUNTER.md:131)

    # GITHUB_TOKEN injected from SSM — CodeBuild resolves it at build start.
    # Never passed as a PLAINTEXT environmentVariable.
    environment_variable {
      name  = "GITHUB_TOKEN"
      value = aws_ssm_parameter.github_token.name
      type  = "PARAMETER_STORE"
    }
  }

  logs_config {
    cloudwatch_logs {
      group_name  = aws_cloudwatch_log_group.codebuild.name
      stream_name = "build"
      status      = "ENABLED"
    }
    s3_logs {
      status = "DISABLED"
    }
  }

  tags = {
    Project = local.name
    Phase   = "13"
  }

  # Ensure IAM role is fully created before the project references it.
  depends_on = [aws_iam_role_policy.codebuild_least_privilege]
}
