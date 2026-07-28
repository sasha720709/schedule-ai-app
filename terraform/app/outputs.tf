output "watches_table_name" {
  value = aws_dynamodb_table.watches.name
}

output "watch_targets_table_name" {
  value = aws_dynamodb_table.watch_targets.name
}

output "planner_lambda_name" {
  value = aws_lambda_function.planner.function_name
}

output "planner_lambda_arn" {
  value = aws_lambda_function.planner.arn
}
