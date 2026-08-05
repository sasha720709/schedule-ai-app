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
    # Named origins rather than "*", now that there is a real domain to name.
    # localhost stays so `npm run dev` works; Vite's default port is 5173.
    # Note allow_credentials is deliberately absent -- auth travels in a
    # header here, not a cookie, so there is nothing to opt in for.
    allow_origins = [
      "https://${aws_cloudfront_distribution.frontend.domain_name}",
      "http://localhost:5173",
    ]
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

# A JWT authorizer, which is a configuration block rather than a function.
#
# **This is the largest security change in the project, and it is a deletion.**
# What it replaces was 76 lines of our own code comparing a shared secret --
# code that had to be right about timing attacks, about caching, about what a
# missing header means. API Gateway now verifies the signature, the issuer,
# the audience and the expiry itself, against Cognito's published keys, before
# the request reaches anything we wrote. Less of our code in the path is the
# point; the rest is a consequence.
#
# The **ID token** is what the browser sends, not the access token. Cognito's
# access tokens carry no `aud` claim, so an audience check -- the thing that
# stops a token minted for some other application being replayed at ours --
# cannot be made against them. The ID token also carries `email` and `sub`,
# which is what turns "somebody authenticated" into "this person".
resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.main.id
  name             = "schedule-ai-app-cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.web[0].id]
    issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.users[0].id}"
  }
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
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
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

# Being a route target is not permission to invoke. This is what actually lets
# API Gateway call the api Lambda, scoped to this one API. There used to be a
# second statement here for the passcode authorizer; a JWT authorizer is
# configuration rather than a function, so it needs no permission and there is
# nothing to invoke.
resource "aws_lambda_permission" "apigw_invoke_api" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}
