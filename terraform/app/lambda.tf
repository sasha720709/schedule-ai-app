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

  environment {
    variables = {
      ANTHROPIC_API_KEY    = var.anthropic_api_key
      WATCHES_TABLE        = aws_dynamodb_table.watches.name
      WATCH_TARGETS_TABLE  = aws_dynamodb_table.watch_targets.name
      CHECKER_FUNCTION_ARN = aws_lambda_function.checker.arn
      SCHEDULER_ROLE_ARN   = aws_iam_role.scheduler_invoke_checker.arn
    }
  }
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
      ANTHROPIC_API_KEY   = var.anthropic_api_key
      WATCHES_TABLE       = aws_dynamodb_table.watches.name
      WATCH_TARGETS_TABLE = aws_dynamodb_table.watch_targets.name
      EVENT_BUS_NAME      = aws_cloudwatch_event_bus.main.name
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
