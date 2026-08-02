# A custom bus rather than the account's default one: this project's events
# stay separate from everything else AWS emits, so rules here can't
# accidentally match unrelated traffic.
resource "aws_cloudwatch_event_bus" "main" {
  name = "schedule-ai-app-bus"
}

# The rule is the subscription: it watches the bus for one shape of event
# and forwards matches to the Notifier. The Checker knows nothing about it.
resource "aws_cloudwatch_event_rule" "watch_triggered" {
  name           = "schedule-ai-app-watch-triggered"
  event_bus_name = aws_cloudwatch_event_bus.main.name

  event_pattern = jsonencode({
    source        = ["schedule-ai-app.checker"]
    "detail-type" = ["WatchTriggered"]
  })
}

resource "aws_cloudwatch_event_target" "notifier" {
  rule           = aws_cloudwatch_event_rule.watch_triggered.name
  event_bus_name = aws_cloudwatch_event_bus.main.name

  # EventBridge's default is ~185 retries over 24 hours. For a notification
  # that can never succeed (SES identity revoked, template bug) that is a full
  # day of invoking a Lambda to re-learn the same failure. Eight attempts over
  # an hour covers every transient SES wobble worth surviving; anything still
  # failing after that is structural, and the Notifier-errors alarm -- not the
  # retry queue -- is the mechanism that reports it. A real DLQ would keep the
  # event itself, but needs SQS permissions the deploy user does not have; see
  # docs/phase-5-plan.md.
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 8
  }
  arn            = aws_lambda_function.notifier.arn
}

# Note the contrast with Scheduler: a schedule *assumes a role* to invoke a
# Lambda, but an EventBridge rule is granted access by the Lambda itself,
# via a resource-based policy. Two different directions of trust.
resource "aws_lambda_permission" "events_invoke_notifier" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.watch_triggered.arn
}

# A degraded watch needs the same two things a triggered one does -- tell the
# owner, then delete the schedules so it stops billing. Same Notifier, separate
# rule: "your watch fired" and "your watch broke" are opposite messages, and
# routing them through one detail-type would mean branching on a field instead
# of on the event's identity.
resource "aws_cloudwatch_event_rule" "watch_degraded" {
  name           = "schedule-ai-app-watch-degraded"
  event_bus_name = aws_cloudwatch_event_bus.main.name

  event_pattern = jsonencode({
    source        = ["schedule-ai-app.checker"]
    "detail-type" = ["WatchDegraded"]
  })
}

resource "aws_cloudwatch_event_target" "notifier_degraded" {
  rule           = aws_cloudwatch_event_rule.watch_degraded.name
  event_bus_name = aws_cloudwatch_event_bus.main.name

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 8
  }
  target_id      = "notifier"
  arn            = aws_lambda_function.notifier.arn
}

resource "aws_lambda_permission" "events_invoke_notifier_degraded" {
  statement_id  = "AllowExecutionFromEventBridgeDegraded"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.watch_degraded.arn
}
