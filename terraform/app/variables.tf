variable "aws_region" {
  description = "AWS region for app resources"
  type        = string
  default     = "us-east-1"
}

variable "anthropic_api_key" {
  description = "Claude API key for the Planner Lambda's own runtime calls. Supply via TF_VAR_anthropic_api_key -- never commit this."
  type        = string
  sensitive   = true
}

variable "notify_email" {
  description = "Address the Notifier sends to and from. Supply via TF_VAR_notify_email -- deliberately no default, since this repo is public."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Ceiling on the estimated running cost of a single watch. The minimum allowed check interval is derived from this, so it relaxes automatically when Phase 8 makes a check cheaper -- rather than being a hardcoded interval floor someone has to remember to change."
  type        = number
  default     = 5.0
}
