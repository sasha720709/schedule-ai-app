# Phase 5: the alarm the AWS budget alarms cannot be.
#
# GATED OFF BY DEFAULT. The `schedule-ai-terraform` user cannot create SNS
# topics or CloudWatch alarms yet, and cannot grant itself the permission --
# it holds `iam:*Role` but deliberately not `iam:*Policy`, so it cannot edit
# the customer-managed policy that governs it. Without the gate, every
# `terraform apply` in the repo fails on a 403 for an unrelated change.
#
# To turn this on: add the SNS and CloudWatch statements to the
# `schedule-ai-app-terraform` policy (see docs/phase-5-plan.md), then set
# `enable_alarms = true`.
#
# The account has budget alarms at $50 / $100 / $200. They are blind to the
# thing that has always dominated this system's cost, because Anthropic bills
# Anthropic and AWS has no idea the calls happened. `EstimatedCostUSD` is the
# Checker's own accounting of what it spent, and this is what watches it.
#
# The alarm is on the DIMENSIONLESS series. The Checker publishes the same
# number twice -- once split by fetch method and model use, for reading, and
# once bare. A dimensioned metric cannot be alarmed on without naming its exact
# dimension set, so an alarm on {browser,false} would silently ignore every
# HTTP check and every Tier 1 repair.

resource "aws_sns_topic" "alarms" {
  count = var.enable_alarms ? 1 : 0

  name = "schedule-ai-app-alarms"
}

# Requires clicking a confirmation link in the first email AWS sends. Until
# that happens the subscription sits in "pending confirmation" and the alarm
# fires into nothing -- Terraform reports success either way, so check the
# subscription state rather than assuming.
resource "aws_sns_topic_subscription" "alarms_email" {
  count = var.enable_alarms ? 1 : 0

  topic_arn = aws_sns_topic.alarms[0].arn
  protocol  = "email"
  endpoint  = var.notify_email
}

# Daily spend across every watch. The per-watch budget is $5/month, so a single
# watch inside its budget contributes about $0.17 a day. This threshold is
# deliberately well above that and well below anything alarming: it is the
# "something is looping" tripwire, not a cost report.
#
# Phase 8 makes this quiet by construction -- an HTTP watch at one-minute
# intervals costs $0.18 a MONTH -- so if this fires, something has genuinely
# gone wrong rather than merely grown.
resource "aws_cloudwatch_metric_alarm" "daily_spend" {
  count = var.enable_alarms ? 1 : 0

  alarm_name        = "schedule-ai-app-daily-spend"
  alarm_description = <<-EOT
    Estimated Anthropic + AWS spend across all watches over 24h.
    AWS budget alarms cannot see the Anthropic half; this can.
    Published by the Checker from shared/cost.py.
  EOT

  namespace   = "ScheduleAI"
  metric_name = "EstimatedCostUSD"
  statistic   = "Sum"
  period      = 86400
  evaluation_periods  = 1
  threshold           = var.daily_spend_alarm_usd
  comparison_operator = "GreaterThanThreshold"

  # A day with no checks publishes no datapoints at all. Treating that as
  # breaching would page on an idle account, which is the fastest way to teach
  # someone to ignore an alarm.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms[0].arn]
  ok_actions    = [aws_sns_topic.alarms[0].arn]
}

# A watch that degrades is already emailed by the Notifier. This alarm is for
# the shape that email cannot cover: the Checker itself failing, which means no
# check ran at all and therefore nothing knows to complain.
resource "aws_cloudwatch_metric_alarm" "checker_errors" {
  count = var.enable_alarms ? 1 : 0

  alarm_name        = "schedule-ai-app-checker-errors"
  alarm_description = "The Checker is throwing. No tick means no watch is being read."

  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  statistic   = "Sum"
  period      = 900
  evaluation_periods  = 1
  threshold           = 3
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.checker.function_name
  }

  alarm_actions = [aws_sns_topic.alarms[0].arn]
}

# The retry cap above (events.tf) means a permanently-failing notification is
# dropped after 8 attempts instead of retried for a day. This alarm is the
# other half of that trade: dropping is only acceptable if somebody hears
# about it. One Notifier error is worth an email -- it means a triggered or
# degraded watch may not have reached the owner.
resource "aws_cloudwatch_metric_alarm" "notifier_errors" {
  count = var.enable_alarms ? 1 : 0

  alarm_name        = "schedule-ai-app-notifier-errors"
  alarm_description = "The Notifier is failing. A fired or degraded watch may not have reached the owner."

  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  statistic   = "Sum"
  period      = 900
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.notifier.function_name
  }

  alarm_actions = [aws_sns_topic.alarms[0].arn]
}

# A source that has started refusing us.
#
# This alarm is the entire reason `blocked` is a state of its own rather than
# another kind of failure. Without it, the day Amazon starts turning us away
# looks like N unrelated broken extractors: each watch pays for a repair that
# cannot work, each degrades separately three ticks later, and the owner gets N
# emails saying N different things broke. The one fact worth knowing -- *a
# source went dark* -- is the one thing nobody is told.
#
# On the DIMENSIONLESS series, for the reason recorded above: the Checker also
# publishes a `Host` dimension, which says *which* source it was, and a
# dimensioned metric cannot be alarmed on without naming its exact dimension
# set. An alarm on {amazon.com} would miss the day a different shop starts.
#
# The threshold is deliberately low. Blocking is probabilistic, so one refusal
# is noise -- but the Checker already tolerates ten consecutive refusals before
# it stops a watch, and this needs to speak long before that, while there is
# still something to do about it. Over an hour, more than five refusals across
# the whole system is a pattern rather than a bad render.
resource "aws_cloudwatch_metric_alarm" "blocked_fetches" {
  count = var.enable_alarms ? 1 : 0

  alarm_name        = "schedule-ai-app-blocked-fetches"
  alarm_description = <<-EOT
    A site is refusing our requests. Nothing is broken and no repair will
    help; the watches involved keep trying and stop themselves after
    BLOCKED_DEGRADE_AFTER consecutive refusals. Check the Host dimension of
    ScheduleAI/BlockedFetches to see which source it is.
  EOT

  namespace   = "ScheduleAI"
  metric_name = "BlockedFetches"
  statistic   = "Sum"
  period      = 3600

  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"

  # Not breaching: no data means nothing was refused, which is the normal and
  # overwhelmingly common state. `missing` would leave the alarm permanently
  # INSUFFICIENT_DATA and train the owner to ignore it.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms[0].arn]
  ok_actions    = [aws_sns_topic.alarms[0].arn]
}
