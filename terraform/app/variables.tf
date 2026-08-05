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

variable "sender_domain" {
  description = <<-EOT
    A domain this account controls, used as the From address for every
    notification. Empty disables it and keeps sending from notify_email.

    Not cosmetic. Sending from the recipient's own gmail.com address means
    every message fails SPF and is filed as spam -- measured, 8 accepted and
    8 invisible. A domain with Easy DKIM is signed and aligned, which is what
    DMARC asks for.

    Whatever the domain is *for* does not matter; a website and a mail sender
    share a name and nothing else. It must have a public hosted zone in this
    account, because the DKIM records are written into it.
  EOT
  type        = string
  default     = ""
}

variable "sender_local_part" {
  description = "The mailbox half of the From address. Nothing receives here."
  type        = string
  default     = "notifications"
}

variable "sender_zone_id" {
  description = <<-EOT
    The Route 53 hosted zone that serves sender_domain, by id.

    Named rather than looked up: a data source would need ListHostedZones,
    GetHostedZone and ListTagsForResource across the account, to save typing
    an id that never changes.
  EOT
  type        = string
  default     = ""
}

variable "auth_enabled" {
  description = <<-EOT
    Whether Google sign-in via Cognito replaces the shared passcode.

    Off leaves the passcode path exactly as it is, so the auth work can be
    merged and deployed before the Google OAuth client exists without locking
    anyone out of a working API.
  EOT
  type        = bool
  default     = false
}

variable "auth_domain_prefix" {
  description = <<-EOT
    Prefix for the Cognito hosted sign-in domain, which becomes
    <prefix>.auth.<region>.amazoncognito.com. Globally unique across AWS.
  EOT
  type        = string
  default     = "schedule-ai-app"
}

variable "google_client_id" {
  description = "OAuth 2.0 client id from Google Cloud Console. Not a secret."
  type        = string
  default     = ""
}

variable "google_client_secret" {
  description = <<-EOT
    OAuth 2.0 client secret from Google Cloud Console.

    Used by Cognito server-side and never by the browser, which is why the app
    client itself has no secret. Note that `sensitive` hides a value from CLI
    output and NOT from the state file -- the same known gap as
    anthropic_api_key, recorded in CLAUDE.md.
  EOT
  type        = string
  sensitive   = true
  default     = ""
}

variable "allowed_emails" {
  description = <<-EOT
    Comma-separated addresses permitted to sign in. Everyone else is refused
    before an account is created.

    Not optional. Enabling Google sign-in enables it for everyone with a
    Google account; an empty list denies everybody, deliberately, because the
    other reading turns a misconfiguration into a public API.
  EOT
  type        = string
  default     = ""
}
