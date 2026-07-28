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
