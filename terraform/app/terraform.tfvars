# Committed on purpose, and only the values that are not secrets.
#
# This file exists because of a footgun, not for convenience. Every variable
# here has a default that means "off", and `terraform apply` without them
# would have silently destroyed the Cognito pool and the DKIM records that
# make email arrive -- reporting it as an ordinary plan. A default that
# disables a live feature is a landmine when the value lives only in someone's
# shell history.
#
# The two real secrets are NOT here and are read from the environment:
#
#   TF_VAR_anthropic_api_key      the app's own runtime key
#   TF_VAR_google_client_secret   used by Cognito, never by the browser
#
# `notify_email` and `allowed_emails` are also absent, deliberately: this repo
# is public, and an address in it is an address in a scraper's list. They are
# passed at apply time. `allowed_emails` failing closed -- an empty list denies
# everyone -- is what makes forgetting it safe rather than dangerous.

auth_enabled       = true
auth_domain_prefix = "schedule-ai-app"

sender_domain  = "beer7.click"
sender_zone_id = "Z0836567CAF8A0JME55L"

# Not a secret, and committed for the same reason `frontend/.env.production`
# is: it is visible in every sign-in request the browser makes and it is in
# the frontend bundle. Google documents it as public. The *secret* half is not
# here and never will be.
google_client_id = "754785323307-6gj6ukuujujkumlnagfo2mvghld37ioc.apps.googleusercontent.com"
