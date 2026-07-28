# Trust policy: only the Lambda service may assume this role.
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "planner_lambda" {
  name               = "schedule-ai-app-planner-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# CloudWatch Logs access every Lambda needs, regardless of what it does.
resource "aws_iam_role_policy_attachment" "planner_lambda_basic_execution" {
  role       = aws_iam_role.planner_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# The Planner only ever writes new plans; it never reads or updates existing
# rows (that's the Checker's job later), so PutItem is all it gets.
data "aws_iam_policy_document" "planner_lambda_dynamodb" {
  statement {
    actions = ["dynamodb:PutItem"]
    resources = [
      aws_dynamodb_table.watches.arn,
      aws_dynamodb_table.watch_targets.arn,
    ]
  }
}

resource "aws_iam_role_policy" "planner_lambda_dynamodb" {
  name   = "dynamodb-write"
  role   = aws_iam_role.planner_lambda.id
  policy = data.aws_iam_policy_document.planner_lambda_dynamodb.json
}

# ---------------------------------------------------------------------------
# Checker Lambda
# ---------------------------------------------------------------------------

resource "aws_iam_role" "checker_lambda" {
  name               = "schedule-ai-app-checker-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "checker_lambda_basic_execution" {
  role       = aws_iam_role.checker_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# The Checker reads both tables and updates them with what it found; unlike
# the Planner it never creates rows, so no PutItem.
data "aws_iam_policy_document" "checker_lambda_dynamodb" {
  statement {
    actions = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [
      aws_dynamodb_table.watches.arn,
      aws_dynamodb_table.watch_targets.arn,
    ]
  }
}

resource "aws_iam_role_policy" "checker_lambda_dynamodb" {
  name   = "dynamodb-read-update"
  role   = aws_iam_role.checker_lambda.id
  policy = data.aws_iam_policy_document.checker_lambda_dynamodb.json
}

# ---------------------------------------------------------------------------
# EventBridge Scheduler
#
# A schedule doesn't invoke a Lambda on its own authority -- it assumes a
# role to do it. This is that role: trusted by the Scheduler service, and
# allowed to invoke exactly one function.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "scheduler_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler_invoke_checker" {
  name               = "schedule-ai-app-scheduler-invoke-checker"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume_role.json
}

data "aws_iam_policy_document" "scheduler_invoke_checker" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.checker.arn]
  }
}

resource "aws_iam_role_policy" "scheduler_invoke_checker" {
  name   = "invoke-checker"
  role   = aws_iam_role.scheduler_invoke_checker.id
  policy = data.aws_iam_policy_document.scheduler_invoke_checker.json
}

# The Planner creates those schedules at runtime, so it needs to call
# Scheduler *and* to hand the role above to the schedules it creates.
# PassRole is the permission that allows that handoff.
data "aws_iam_policy_document" "planner_lambda_scheduler" {
  statement {
    actions = [
      "scheduler:CreateSchedule",
      "scheduler:GetSchedule",
      "scheduler:UpdateSchedule",
      "scheduler:DeleteSchedule",
    ]
    resources = ["arn:aws:scheduler:${var.aws_region}:${data.aws_caller_identity.current.account_id}:schedule/default/schedule-ai-app-*"]
  }

  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.scheduler_invoke_checker.arn]
  }
}

resource "aws_iam_role_policy" "planner_lambda_scheduler" {
  name   = "create-schedules"
  role   = aws_iam_role.planner_lambda.id
  policy = data.aws_iam_policy_document.planner_lambda_scheduler.json
}

# The Checker only announces; it needs nothing but the right to publish.
data "aws_iam_policy_document" "checker_lambda_events" {
  statement {
    actions   = ["events:PutEvents"]
    resources = [aws_cloudwatch_event_bus.main.arn]
  }
}

resource "aws_iam_role_policy" "checker_lambda_events" {
  name   = "put-events"
  role   = aws_iam_role.checker_lambda.id
  policy = data.aws_iam_policy_document.checker_lambda_events.json
}

# ---------------------------------------------------------------------------
# Browser Fetcher Lambda
#
# It only renders a page and returns text -- it touches no AWS service, so
# CloudWatch Logs is the entire permission set.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "fetcher_lambda" {
  name               = "schedule-ai-app-fetcher-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "fetcher_lambda_basic_execution" {
  role       = aws_iam_role.fetcher_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "checker_lambda_invoke_fetcher" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.fetcher.arn]
  }
}

resource "aws_iam_role_policy" "checker_lambda_invoke_fetcher" {
  name   = "invoke-fetcher"
  role   = aws_iam_role.checker_lambda.id
  policy = data.aws_iam_policy_document.checker_lambda_invoke_fetcher.json
}

# ---------------------------------------------------------------------------
# Notifier Lambda
# ---------------------------------------------------------------------------

resource "aws_iam_role" "notifier_lambda" {
  name               = "schedule-ai-app-notifier-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "notifier_lambda_basic_execution" {
  role       = aws_iam_role.notifier_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "notifier_lambda" {
  statement {
    actions   = ["ses:SendEmail"]
    resources = ["*"]
  }

  # Querying a GSI needs the index ARN as well as the table's.
  statement {
    actions = ["dynamodb:Query"]
    resources = [
      aws_dynamodb_table.watch_targets.arn,
      "${aws_dynamodb_table.watch_targets.arn}/index/watch_id-index",
    ]
  }

  statement {
    actions   = ["scheduler:DeleteSchedule"]
    resources = ["arn:aws:scheduler:${var.aws_region}:${data.aws_caller_identity.current.account_id}:schedule/default/schedule-ai-app-*"]
  }
}

resource "aws_iam_role_policy" "notifier_lambda" {
  name   = "notify-and-clean-up"
  role   = aws_iam_role.notifier_lambda.id
  policy = data.aws_iam_policy_document.notifier_lambda.json
}
