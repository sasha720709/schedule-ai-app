"""Passcode authorizer for the HTTP API.

The app is single-user, so auth is one shared passcode rather than Cognito.
This Lambda is what API Gateway calls before any route runs: it compares the
Authorization header against a value in SSM Parameter Store and answers
yes or no.

Two things worth knowing about how this is written.

The passcode is read from SSM rather than passed in as an environment
variable, and the parameter is created outside Terraform. That keeps the
secret out of the Terraform state file entirely -- unlike ANTHROPIC_API_KEY,
which is a documented gap precisely because `sensitive = true` hides a value
from CLI output while still storing it in plaintext in state.

The comparison uses hmac.compare_digest, not ==. String equality returns as
soon as two bytes differ, so how long it takes to fail leaks how much of the
prefix was right. compare_digest takes the same time either way. That matters
very little for a passcode behind an API that also rate-limits, but it costs
nothing to get right.

This is not real authentication. Anyone holding the passcode has full
control, there is no expiry, and there is no per-user isolation -- user_id is
hardcoded to "default" everywhere. It needs replacing before a second person
touches the system.
"""

import hmac
import os

import boto3

ssm = boto3.client("ssm")

# Cached across warm invocations so a busy API isn't billed an SSM call per
# request. The cost is that rotating the passcode only takes effect once the
# execution environment recycles; force it sooner by updating the function's
# configuration, which starts a fresh one.
_passcode_cache = None


def _expected_passcode() -> str:
    global _passcode_cache
    if _passcode_cache is None:
        _passcode_cache = ssm.get_parameter(
            Name=os.environ["PASSCODE_PARAM"],
            WithDecryption=True,
        )["Parameter"]["Value"]
    return _passcode_cache


def _supplied_passcode(event) -> str:
    """Read the header. HTTP APIs lowercase every header name."""
    headers = event.get("headers") or {}
    value = headers.get("authorization", "")
    # Accept "Bearer <passcode>" as well as the bare value, since browsers and
    # curl users reach for either.
    prefix = "bearer "
    if value.lower().startswith(prefix):
        value = value[len(prefix):]
    return value.strip()


def lambda_handler(event, context):
    supplied = _supplied_passcode(event)
    if not supplied:
        print("deny: no Authorization header")
        return {"isAuthorized": False}

    authorized = hmac.compare_digest(supplied, _expected_passcode())
    if not authorized:
        # Never log the supplied value -- a rejected guess is still a secret
        # someone typed, and CloudWatch Logs is readable by more people than
        # the passcode should be.
        print(f"deny: wrong passcode for {event.get('routeKey', 'unknown route')}")
    return {"isAuthorized": authorized}
