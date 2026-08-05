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
  description = "Base URL for the watch lifecycle API. Every request needs an Authorization header carrying a Cognito ID token."
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

output "sender_email" {
  description = "The From address notifications are sent with, once a sender domain is configured."
  value       = var.sender_domain == "" ? "(none — sending from notify_email, which fails SPF)" : "${var.sender_local_part}@${var.sender_domain}"
}

# What Google's OAuth client must be told to redirect back to. It cannot be
# known until the hosted domain exists, which is why the sign-in work is two
# applies rather than one.
output "google_redirect_uri" {
  description = "Paste this into the Google OAuth client's Authorized redirect URIs."
  value       = var.auth_enabled ? "https://${aws_cognito_user_pool_domain.users[0].domain}.auth.${var.aws_region}.amazoncognito.com/oauth2/idpresponse" : "(auth disabled)"
}

output "cognito_hosted_ui" {
  description = "Where the frontend sends people to sign in."
  value       = var.auth_enabled ? "https://${aws_cognito_user_pool_domain.users[0].domain}.auth.${var.aws_region}.amazoncognito.com" : "(auth disabled)"
}

output "cognito_client_id" {
  description = "The app client id the frontend needs. Public by design."
  value       = var.auth_enabled ? aws_cognito_user_pool_client.web[0].id : "(auth disabled)"
}
