# ---------------------------------------------------------------------------
# Who is allowed to use this, decided 2026-08-05
#
# What replaces the shared passcode, and why the passcode had to go: it was one
# secret with no expiry, stored in localStorage, granting the ability to spend
# money -- so a screenshot, a browser extension or a shoulder-glance was
# permanent access until somebody remembered to rotate it by hand. It also
# carried no identity, so nothing in the system could say *who* made a watch.
#
# Google does the passwords and the second factor, Cognito holds the directory
# and issues the token, and API Gateway validates it natively. **We never see a
# credential and we no longer write any authentication code** -- the 76-line
# authorizer Lambda is deleted rather than improved, which is the largest
# security win here and the easiest one to overlook.
#
# Cognito rather than Google alone: the same security, plus a user directory
# this account owns, room for a second identity provider or ordinary passwords
# later, and a real AWS service in a project whose stated purpose is learning
# AWS. Free to 10,000 monthly active users on the Essentials tier, which this
# will not reach.
#
# Gated on `auth_enabled`. Off leaves the passcode path exactly as it is, so
# this file cannot lock anyone out of a working API by being merged.
# ---------------------------------------------------------------------------

resource "aws_cognito_user_pool" "users" {
  count = var.auth_enabled ? 1 : 0

  name = "schedule-ai-app-users"

  # Google is the only way in, so nothing here ever holds a password. The
  # policy is still set rather than left default: a pool that gains an
  # ordinary sign-up path later should not gain a weak one silently.
  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 1
  }

  # Federated users arrive with an email Google has already verified. Saying so
  # keeps the directory honest about which addresses are proven.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # Nobody may create their own account. Sign-up happens only through the
  # federated flow, and the trigger below decides whether that flow is allowed
  # to complete -- see `allowed_emails`.
  admin_create_user_config {
    allow_admin_create_user_only = false
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  dynamic "lambda_config" {
    for_each = var.auth_enabled ? [1] : []
    content {
      pre_sign_up = aws_lambda_function.gatekeeper[0].arn
    }
  }
}

# The hosted sign-in page. Its URL is what Google redirects back to, so it has
# to exist before the Google OAuth client can be configured -- which is why
# this is created in its own step and the redirect URI is an output.
resource "aws_cognito_user_pool_domain" "users" {
  count = var.auth_enabled ? 1 : 0

  domain       = var.auth_domain_prefix
  user_pool_id = aws_cognito_user_pool.users[0].id
}

resource "aws_cognito_identity_provider" "google" {
  count = var.auth_enabled ? 1 : 0

  user_pool_id  = aws_cognito_user_pool.users[0].id
  provider_name = "Google"
  provider_type = "Google"

  provider_details = {
    client_id        = var.google_client_id
    client_secret    = var.google_client_secret
    authorize_scopes = "openid email profile"
  }

  # Map Google's claims onto the pool's attributes. Without `email` the pool
  # has no address to key a user on, and every sign-in creates a new user.
  attribute_mapping = {
    email    = "email"
    username = "sub"
    name     = "name"
  }
}

resource "aws_cognito_user_pool_client" "web" {
  count = var.auth_enabled ? 1 : 0

  name         = "schedule-ai-app-web"
  user_pool_id = aws_cognito_user_pool.users[0].id

  # **No client secret.** A secret shipped inside a browser bundle is not a
  # secret, and pretending otherwise is worse than not having one. The
  # authorization-code flow with PKCE is what makes a public client safe, and
  # Cognito requires PKCE for clients without a secret.
  generate_secret = false

  supported_identity_providers = ["Google"]

  allowed_oauth_flows_user_pool_client = true
  # `code`, never `implicit`. Implicit puts the token in the URL fragment,
  # where it lands in browser history and in any referrer -- it is deprecated
  # for exactly that reason.
  allowed_oauth_flows  = ["code"]
  allowed_oauth_scopes = ["openid", "email", "profile"]

  callback_urls = compact([
    "https://${aws_cloudfront_distribution.frontend.domain_name}/",
    # `npm run dev` needs to sign in too, and a developer who cannot test the
    # signed-in path locally will test it in production instead.
    "http://localhost:5173/",
  ])
  logout_urls = compact([
    "https://${aws_cloudfront_distribution.frontend.domain_name}/",
    "http://localhost:5173/",
  ])

  # An hour of access, thirty days of silent renewal. Short access tokens are
  # the property that makes a leaked one worthless by tomorrow, which is the
  # single thing the shared passcode could never offer.
  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  # Rotate the refresh token on every use, so a stolen one is usable once and
  # its use is detectable rather than silent.
  enable_token_revocation       = true
  prevent_user_existence_errors = "ENABLED"
}

# ---------------------------------------------------------------------------
# The door itself
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "gatekeeper_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "gatekeeper" {
  count = var.auth_enabled ? 1 : 0

  name               = "schedule-ai-app-gatekeeper-lambda"
  assume_role_policy = data.aws_iam_policy_document.gatekeeper_assume_role.json
}

resource "aws_iam_role_policy_attachment" "gatekeeper_basic_execution" {
  count = var.auth_enabled ? 1 : 0

  role       = aws_iam_role.gatekeeper[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# It reads one environment variable and compares strings. No table, no network,
# no other permission -- the least-privileged thing in this account, which is
# appropriate for the piece that decides who gets in.
resource "aws_lambda_function" "gatekeeper" {
  count = var.auth_enabled ? 1 : 0

  function_name    = "schedule-ai-app-gatekeeper"
  role             = aws_iam_role.gatekeeper[0].arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  filename         = "${path.module}/../../gatekeeper/dist/gatekeeper.zip"
  source_code_hash = filebase64sha256("${path.module}/../../gatekeeper/dist/gatekeeper.zip")
  timeout          = 5
  memory_size      = 128

  environment {
    variables = {
      ALLOWED_EMAILS = var.allowed_emails
    }
  }
}

resource "aws_lambda_permission" "cognito_invoke_gatekeeper" {
  count = var.auth_enabled ? 1 : 0

  statement_id  = "AllowCognitoInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.gatekeeper[0].function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.users[0].arn
}
