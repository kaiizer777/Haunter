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
