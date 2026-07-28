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
