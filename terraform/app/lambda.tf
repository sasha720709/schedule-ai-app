resource "aws_lambda_function" "planner" {
  function_name    = "schedule-ai-app-planner"
  role             = aws_iam_role.planner_lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  filename         = "${path.module}/../../planner/dist/planner.zip"
  source_code_hash = filebase64sha256("${path.module}/../../planner/dist/planner.zip")
  timeout          = 60
  memory_size      = 256

  # No CHECKER_FUNCTION_ARN or SCHEDULER_ROLE_ARN any more: the Planner
  # stopped creating schedules in Phase 4 and has no reason to know either.
  environment {
    variables = {
      ANTHROPIC_API_KEY   = var.anthropic_api_key
      WATCHES_TABLE       = aws_dynamodb_table.watches.name
      WATCH_TARGETS_TABLE = aws_dynamodb_table.watch_targets.name
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
    }
  }
}
