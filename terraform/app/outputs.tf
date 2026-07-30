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

output "api_endpoint" {
  description = "Base URL for the watch lifecycle API. Every request needs an Authorization header carrying the passcode from SSM at /schedule-ai-app/passcode."
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "frontend_url" {
  description = "Public URL of the React app."
  value       = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "frontend_bucket" {
  description = "Bucket frontend/deploy.sh syncs the build into."
  value       = aws_s3_bucket.frontend.id
}

output "frontend_distribution_id" {
  description = "Distribution frontend/deploy.sh invalidates after a sync."
  value       = aws_cloudfront_distribution.frontend.id
}
