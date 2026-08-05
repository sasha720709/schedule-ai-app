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

# The Planner creates target rows, and updates the watch row the api Lambda
# already created -- either to "proposed" with the plan on it, or to "failed"
# with the reason. It never touches a schedule any more: since Phase 4 that
# belongs to the api Lambda's confirm endpoint, which is why this role no
# longer has scheduler permissions or PassRole at all.
data "aws_iam_policy_document" "planner_lambda_dynamodb" {
  statement {
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.watch_targets.arn]
  }

  statement {
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.watches.arn]
  }
}

resource "aws_iam_role_policy" "planner_lambda_dynamodb" {
  name   = "dynamodb-write"
  role   = aws_iam_role.planner_lambda.id
  policy = data.aws_iam_policy_document.planner_lambda_dynamodb.json
}

# Since Phase 8b the Planner opens the pages it proposes, and verifies that the
# extractor it compiled really reads the value, before the plan is ever
# offered. For a JS-rendered target that means rendering it -- so the Planner
# needs the same Fetcher the Checker uses. Without this it could only verify
# plain-HTTP targets, and would reject every browser one it recommended.
data "aws_iam_policy_document" "planner_lambda_invoke_fetcher" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.fetcher.arn]
  }
}

resource "aws_iam_role_policy" "planner_lambda_invoke_fetcher" {
  name   = "invoke-fetcher"
  role   = aws_iam_role.planner_lambda.id
  policy = data.aws_iam_policy_document.planner_lambda_invoke_fetcher.json
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

  # Added 2026-08-05, with the GSI's own ARN, which a Query needs as well as
  # the table's. A watch on several shops reads its siblings when it fires, so
  # the email can say what every shop charges instead of only the one that
  # crossed the threshold.
  #
  # Granted before the code that needs it was deployed, deliberately. The Phase
  # 5 lesson was expensive: a per-action policy fails on the action you added
  # last, and it failed *after* the email had been sent, so EventBridge retried
  # the whole invocation and a real person got three copies. The read here is
  # wrapped so a missing permission degrades to an email without the summary,
  # but the permission is what makes the summary exist.
  statement {
    actions = ["dynamodb:Query"]
    resources = [
      aws_dynamodb_table.watch_targets.arn,
      "${aws_dynamodb_table.watch_targets.arn}/index/watch_id-index",
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

# ---------------------------------------------------------------------------
# API Lambda
#
# Every watch lifecycle operation, and the only thing that creates or
# destroys a schedule. This is where the Planner's old scheduler permissions
# moved to, along with the PassRole that lets a new schedule be handed the
# role it assumes to invoke the Checker.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "api_lambda" {
  name               = "schedule-ai-app-api-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "api_lambda_basic_execution" {
  role       = aws_iam_role.api_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "api_lambda" {
  # Scan is what GET /watches uses. The table is keyed on watch_id and there
  # is no index on user_id, so listing means scanning -- fine for one user
  # with a handful of rows, and a documented gap before there are two.
  statement {
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Scan",
    ]
    resources = [aws_dynamodb_table.watches.arn]
  }

  # Querying a GSI needs the index ARN as well as the table's.
  statement {
    actions = [
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
    ]
    resources = [
      aws_dynamodb_table.watch_targets.arn,
      "${aws_dynamodb_table.watch_targets.arn}/index/watch_id-index",
    ]
  }

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

  # POST /watches hands the slow work off asynchronously and returns 202.
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.planner.arn]
  }
}

resource "aws_iam_role_policy" "api_lambda" {
  name   = "watch-lifecycle"
  role   = aws_iam_role.api_lambda.id
  policy = data.aws_iam_policy_document.api_lambda.json
}

# The Checker only announces; it needs nothing but the right to publish.
data "aws_iam_policy_document" "checker_lambda_events" {
  statement {
    actions   = ["events:PutEvents"]
    resources = [aws_cloudwatch_event_bus.main.arn]
  }

  # Publishing what a check cost. PutMetricData takes no resource ARN -- the
  # namespace is the only scope available, and it is expressed as a condition
  # rather than a resource.
  statement {
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["ScheduleAI"]
    }
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
# Authorizer Lambda
#
# Reads one SSM parameter and nothing else. The parameter is a SecureString,
# so decrypting it needs KMS as well -- scoped by kms:ViaService so this role
# can only ever decrypt through SSM, not against arbitrary keys directly.
# ---------------------------------------------------------------------------


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
    # SendRawEmail is a separate action and is not implied by SendEmail. It is
    # what an attachment needs, and the attachment is a reminder's calendar
    # entry. Granted before the code that uses it ships -- the Phase 5 lesson,
    # where a permission added after the fact failed an invocation *after* the
    # email had gone out and EventBridge retried it into three copies.
    #
    # The Notifier falls back to a plain send if this call fails, so a missing
    # permission costs the attachment and never the notification. This grant
    # is what makes the attachment actually arrive.
    actions   = ["ses:SendEmail", "ses:SendRawEmail"]
    resources = ["*"]
  }

  # Querying a GSI needs the index ARN as well as the table's.
  #
  # UpdateItem is for clearing `schedule_arn` as each schedule is deleted, so
  # the table stops describing schedules that no longer exist. Added late, and
  # the omission was found in production rather than in review: the Phase 5
  # code change shipped without this line, the email had already been sent when
  # the AccessDenied hit, and EventBridge retried the whole invocation --
  # delivering a duplicate notification to a real person before it was caught.
  #
  # The handler no longer depends on this permission (the tidy-up is swallowed,
  # because nothing after the email may re-notify). This grant is what makes
  # the tidy-up actually work rather than merely fail quietly.
  statement {
    actions = ["dynamodb:Query", "dynamodb:UpdateItem"]
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
