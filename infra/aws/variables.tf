variable "region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefix for resource names (e.g. 'haunter')."
  type        = string
  default     = "haunter"
}

variable "github_token" {
  description = "GitHub Personal Access Token (or App Installation Token) for repo cloning. Stored in SSM SecureString — never in state plaintext."
  type        = string
  sensitive   = true
}

variable "log_retention_days" {
  description = "CloudWatch log group retention in days."
  type        = number
  default     = 7
}

# ---------------------------------------------------------------------------
# Lambda variables (Phase 14)
# ---------------------------------------------------------------------------

variable "lambda_zip_path" {
  description = "Path to the Lambda deployment zip file. Build with: cd backend && zip -r ../lambda.zip . -x '*.pyc' -x '__pycache__/*' -x '.venv/*' -x 'tests/*'"
  type        = string
  default     = "../../lambda.zip"
}

variable "database_url" {
  description = "Pooled Neon Postgres URL for Lambda app queries (NullPool — Neon's PgBouncer handles pooling)."
  type        = string
  sensitive   = true
}

variable "database_url_unpooled" {
  description = "Direct (unpooled) Neon Postgres URL for Alembic migrations only."
  type        = string
  sensitive   = true
}

variable "github_client_id" {
  description = "GitHub OAuth App client ID."
  type        = string
  sensitive   = true
}

variable "github_client_secret" {
  description = "GitHub OAuth App client secret."
  type        = string
  sensitive   = true
}

variable "callback_url" {
  description = "GitHub OAuth callback URL (hardcoded in config — never from request headers)."
  type        = string
}

variable "session_secret_key" {
  description = "itsdangerous TimestampSigner key for session cookies."
  type        = string
  sensitive   = true
}

variable "frontend_url" {
  description = "Frontend URL for CORS + OAuth redirect (e.g. https://haunter.pages.dev)."
  type        = string
}

variable "github_webhook_secret" {
  description = "HMAC-SHA256 secret for GitHub webhook signature verification."
  type        = string
  sensitive   = true
}

variable "opencode_zen_api_key" {
  description = "OpenCode Zen API key for LLM calls."
  type        = string
  sensitive   = true
  default     = ""
}

variable "token_encryption_key" {
  description = "Fernet key for encrypting users.access_token at rest. Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
  type        = string
  sensitive   = true
}

# ---------------------------------------------------------------------------
# GitHub Actions sandbox variables (Phase 1.6 of github.md)
# Used when SANDBOX_PROVIDER=github_actions. The private key itself lives
# in SSM Parameter Store as a SecureString — only the path is here.
# ---------------------------------------------------------------------------

variable "github_sandbox_app_id" {
  description = "GitHub App ID for the Haunter sandbox runner. Non-secret (visible in App's public metadata)."
  type        = string
  default     = ""
}

variable "github_sandbox_installation_id" {
  description = "GitHub App installation ID on the Haunter org. Non-secret (visible in the install URL)."
  type        = string
  default     = ""
}

variable "github_sandbox_app_private_key_ssm_path" {
  description = "SSM Parameter Store path (SecureString) holding the GitHub App PEM. Lambda reads it at cold start via boto3 — the PEM never enters Terraform state, .env, or lambda.zip."
  type        = string
  default     = "/haunter/GITHUB_SANDBOX_APP_PRIVATE_KEY"
}

variable "github_sandbox_org" {
  description = "GitHub org that owns the test-mirror repos for the GitHub Actions sandbox."
  type        = string
  default     = "haunter-sandboxes"
}

variable "github_sandbox_poll_interval_seconds" {
  description = "How often the runner polls the check-runs API while waiting for the test workflow to finish."
  type        = number
  default     = 10
}

variable "github_sandbox_poll_timeout_seconds" {
  description = "Max wall-clock seconds the runner will wait for the test workflow before giving up and treating the attempt as failed."
  type        = number
  default     = 120
}

# ---------------------------------------------------------------------------
# Sandbox provider selector (Phase 14 + GitHub Actions activation)
# Selects which SandboxRunner implementation the Lambda uses:
#   "gcp"             → app.sandbox.gcp_runner.GCPSandboxRunner (Cloud Build)
#   "aws"             → app.sandbox.aws_runner.AWSSandboxRunner (CodeBuild)
#   "github_actions"  → app.sandbox.github_actions_runner.GitHubActionsSandboxRunner
# Flipping this is the single switch that activates the GitHub Actions
# sandbox (see github.md §6.2). Default is "aws" to match pre-Phase-2 state.
# ---------------------------------------------------------------------------

variable "sandbox_provider" {
  description = "Sandbox backend to use for patch verification. One of: gcp, aws, github_actions."
  type        = string
  default     = "aws"
}
