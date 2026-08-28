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
