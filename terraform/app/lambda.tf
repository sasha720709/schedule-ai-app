resource "aws_lambda_function" "planner" {
  function_name    = "schedule-ai-app-planner"
  role             = aws_iam_role.planner_lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = "${path.module}/../../planner/dist/planner.zip"
  source_code_hash = filebase64sha256("${path.module}/../../planner/dist/planner.zip")

  # Phase 8b made planning much heavier on purpose: a web search, then per
  # target a fetch (a browser render is ~5-10s), a Haiku read and up to two
  # Sonnet compile-and-verify rounds. Three targets can plausibly reach two
  # minutes. This is invoked asynchronously so no API Gateway ceiling applies,
  # and it runs once per watch rather than once per tick -- the cost of the
  # headroom is nil, and the cost of timing out is a watch that never plans.
  timeout = 300

  # Raised with it, for CPU rather than for RAM: Lambda scales CPU with memory,
  # and this now parses ~1.5MB of HTML with BeautifulSoup. At once-per-watch
  # frequency the extra memory is worth well under a cent a month.
  memory_size = 1024

  # No CHECKER_FUNCTION_ARN or SCHEDULER_ROLE_ARN any more: the Planner
  # stopped creating schedules in Phase 4 and has no reason to know either.
  # FETCHER_FUNCTION_ARN is new: it verifies browser targets by rendering them.
  environment {
    variables = {
      ANTHROPIC_API_KEY    = var.anthropic_api_key
      WATCHES_TABLE        = aws_dynamodb_table.watches.name
      WATCH_TARGETS_TABLE  = aws_dynamodb_table.watch_targets.name
      MONTHLY_BUDGET_USD   = var.monthly_budget_usd
      FETCHER_FUNCTION_ARN = aws_lambda_function.fetcher.arn
      DEFAULT_TIMEZONE     = var.default_timezone
    }
  }
}

# The api Lambda invokes the Planner asynchronously, and Lambda retries a
# failed async invocation twice by default. A retry here would re-run the web
# search and write a second set of target rows, so retries are turned off --
# handler.py records the failure on the watch row instead of raising.
resource "aws_lambda_function_event_invoke_config" "planner" {
  function_name          = aws_lambda_function.planner.function_name
  maximum_retry_attempts = 0
}

resource "aws_lambda_function" "checker" {
  function_name    = "schedule-ai-app-checker"
  role             = aws_iam_role.checker_lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = "${path.module}/../../checker/dist/checker.zip"
  source_code_hash = filebase64sha256("${path.module}/../../checker/dist/checker.zip")
  timeout          = 60
  memory_size      = 256

  environment {
    variables = {
      ANTHROPIC_API_KEY    = var.anthropic_api_key
      WATCHES_TABLE        = aws_dynamodb_table.watches.name
      WATCH_TARGETS_TABLE  = aws_dynamodb_table.watch_targets.name
      EVENT_BUS_NAME       = aws_cloudwatch_event_bus.main.name
      FETCHER_FUNCTION_ARN = aws_lambda_function.fetcher.arn
      MONTHLY_BUDGET_USD   = var.monthly_budget_usd
    }
  }
}

# A container-image Lambda: no handler/runtime/filename here, because the
# image itself carries the interpreter, Chromium, and the entrypoint.
# Memory is high because Chromium genuinely needs it -- and on Lambda, CPU
# scales with memory, so this also buys the speed to render a page.
resource "aws_lambda_function" "fetcher" {
  function_name = "schedule-ai-app-fetcher"
  role          = aws_iam_role.fetcher_lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.fetcher.repository_url}:latest"
  architectures = ["x86_64"]
  timeout       = 60
  memory_size   = 2048
}

# Every watch lifecycle route behind one function. No ANTHROPIC_API_KEY here
# -- this Lambda never talks to a model, it only reads and writes rows and
# creates or deletes schedules.
resource "aws_lambda_function" "api" {
  function_name    = "schedule-ai-app-api"
  role             = aws_iam_role.api_lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = "${path.module}/../../api/dist/api.zip"
  source_code_hash = filebase64sha256("${path.module}/../../api/dist/api.zip")
  # API Gateway gives up at 30s, so there is no point waiting longer.
  timeout     = 30
  memory_size = 256

  environment {
    variables = {
      WATCHES_TABLE        = aws_dynamodb_table.watches.name
      WATCH_TARGETS_TABLE  = aws_dynamodb_table.watch_targets.name
      PLANNER_FUNCTION_ARN = aws_lambda_function.planner.arn
      CHECKER_FUNCTION_ARN = aws_lambda_function.checker.arn
      SCHEDULER_ROLE_ARN   = aws_iam_role.scheduler_invoke_checker.arn
      MONTHLY_BUDGET_USD   = var.monthly_budget_usd
    }
  }
}

# Called by API Gateway before any route runs. Reads the passcode from SSM at
# runtime rather than taking it as an env var, which keeps the secret out of
# Terraform state -- deliberately unlike ANTHROPIC_API_KEY, whose presence in
# state is a documented gap.
resource "aws_lambda_function" "authorizer" {
  function_name    = "schedule-ai-app-authorizer"
  role             = aws_iam_role.authorizer_lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = "${path.module}/../../authorizer/dist/authorizer.zip"
  source_code_hash = filebase64sha256("${path.module}/../../authorizer/dist/authorizer.zip")
  timeout          = 10
  memory_size      = 128

  environment {
    variables = {
      # The name only. The value is never handled by Terraform.
      PASSCODE_PARAM = local.passcode_param_name
    }
  }
}

resource "aws_lambda_function" "notifier" {
  function_name    = "schedule-ai-app-notifier"
  role             = aws_iam_role.notifier_lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = "${path.module}/../../notifier/dist/notifier.zip"
  source_code_hash = filebase64sha256("${path.module}/../../notifier/dist/notifier.zip")
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      NOTIFY_EMAIL        = var.notify_email
      WATCH_TARGETS_TABLE = aws_dynamodb_table.watch_targets.name
      # Who the mail is FROM. Empty until a sender domain exists, and the
      # Notifier falls back to the recipient -- which is what it always did,
      # and what made every notification fail SPF. See terraform/app/ses.tf.
      SENDER_EMAIL = var.sender_domain == "" ? "" : "${var.sender_local_part}@${var.sender_domain}"
    }
  }
}
