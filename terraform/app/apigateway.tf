# The HTTP front door.
#
# An HTTP API rather than a REST API: cheaper ($1 per million requests
# against $3.50), far less configuration, and it does everything this needs.
# The features REST APIs have that this gives up -- request validation,
# API keys, WAF, edge caching -- are all things a single-user app does not
# want.
#
# Deliberately not here: access logging. Terraform would have to create a
# CloudWatch log group, and the deploy user's policy grants only read actions
# on Logs, so adding it would mean another IAM round trip. The Lambdas' own
# logs are enough to debug this, and access logs belong with the rest of the
# observability work in Phase 5.

locals {
  # Named once, used by the authorizer's env var and its IAM policy. The
  # value behind this name is created outside Terraform, on purpose, so the
  # passcode never enters the state file.
  passcode_param_name = "/schedule-ai-app/passcode"
}

resource "aws_apigatewayv2_api" "main" {
  name          = "schedule-ai-app"
  protocol_type = "HTTP"
  description   = "Watch lifecycle API"

  # API Gateway answers OPTIONS preflight itself from this block, and does it
  # *without* invoking the authorizer -- which is the whole reason a browser
  # can call an authorized route at all. An authorizer that rejected the
  # preflight would make every cross-origin request fail before the real
  # request was ever sent.
  cors_configuration {
    # "*" until there is a CloudFront domain to name in 4b. Note this cannot
    # be combined with allow_credentials, which is fine: auth travels in a
    # header here, not a cookie.
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]
    allow_headers = ["content-type", "authorization"]
    max_age       = 300
  }
}

# AWS_PROXY hands the whole request to the Lambda and takes its response
# verbatim, which is why handler.py returns statusCode/headers/body itself.
# Payload format 2.0 is what puts `routeKey` and `pathParameters` on the
# event -- the shape the api Lambda was written and tested against.
resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

# A REQUEST authorizer with simple responses: the Lambda answers
# {"isAuthorized": true|false} rather than assembling an IAM policy document,
# which is all a shared passcode needs.
resource "aws_apigatewayv2_authorizer" "passcode" {
  api_id                            = aws_apigatewayv2_api.main.id
  name                              = "schedule-ai-app-passcode"
  authorizer_type                   = "REQUEST"
  authorizer_uri                    = aws_lambda_function.authorizer.invoke_arn
  authorizer_payload_format_version = "2.0"
  enable_simple_responses           = true
  identity_sources                  = ["$request.header.Authorization"]

  # Cache a decision per distinct header value. A wrong passcode is cached
  # too, so a typo keeps failing for five minutes -- acceptable, and it also
  # blunts brute forcing. Naming an identity source is what makes caching
  # possible at all; without one every request would invoke the authorizer.
  authorizer_result_ttl_in_seconds = 300
}

resource "aws_apigatewayv2_route" "routes" {
  for_each = toset([
    "POST /watches",
    "GET /watches",
    "GET /watches/{id}",
    "POST /watches/{id}/confirm",
    "PATCH /watches/{id}",
    "DELETE /watches/{id}",
  ])

  api_id             = aws_apigatewayv2_api.main.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type = "CUSTOM"
  authorizer_id      = aws_apigatewayv2_authorizer.passcode.id
}

# $default with auto_deploy: no explicit deployment step, and no stage name in
# the URL. A named stage would put "/prod" in front of every path for no
# benefit on a single-environment project.
resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    # A runaway frontend polling loop is a real risk with a $0.0057-per-check
    # backend behind it. This caps the damage.
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }
}

# Being a route target is not permission to invoke. These two statements are
# what actually let API Gateway call the functions, scoped to this one API.
resource "aws_lambda_permission" "apigw_invoke_api" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

resource "aws_lambda_permission" "apigw_invoke_authorizer" {
  statement_id  = "AllowApiGatewayInvokeAuthorizer"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.authorizer.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/authorizers/${aws_apigatewayv2_authorizer.passcode.id}"
}
