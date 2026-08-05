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

variable "daily_spend_alarm_usd" {
  description = <<-EOT
    Daily estimated spend across all watches that trips the alarm. Not a
    budget -- the per-watch $5/month budget in shared/cost.py is the budget.
    This is the "something is looping" tripwire, so it sits well above normal
    ($0.17/day for one watch at its full budget) and well below painful.
  EOT
  type    = number
  default = 2.0
}

variable "enable_alarms" {
  description = <<-EOT
    Create the SNS topic and CloudWatch alarms. Off until the deploy user is
    granted sns:* and cloudwatch:*Alarm* on schedule-ai-app-* resources -- it
    cannot grant itself, holding iam:*Role but not iam:*Policy on purpose.
    Granted 2026-07-31; on by default since.
  EOT
  type    = bool
  default = true
}

variable "default_timezone" {
  description = <<-EOT
    Where "9am" is, when a reminder request does not name a place.

    A deployment setting, exactly like notify_email, and a placeholder for the
    same missing thing: there is no user record, so the one person using this
    has to be described in configuration. The resolved local time and its zone
    are shown on the plan card before anything is confirmed, so a wrong value
    costs a glance rather than a reminder at the wrong hour.
  EOT
  type        = string
  default     = "Asia/Jerusalem"
}
