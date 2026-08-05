"""Cognito pre-sign-up trigger: decide whether this person may exist here.

## Why this is not optional

Turning on "sign in with Google" turns on sign-in for **everyone who has a
Google account**, which is most of the internet. Without this, replacing the
shared passcode would swap one weak door for no door at all -- and it would
look like an improvement, because the sign-in page would be Google's.

Cognito runs this once, when a federated user is about to be created. Raising
here means the account is never created: the person sees Google's own error
and no row appears in the directory. Checking later, in the API, would leave a
directory full of strangers and one more place to remember to check.

## The list is a list, deliberately

`ALLOWED_EMAILS` is an environment variable, not a table. This is a personal
project with one user; a table would be a schema, a migration and an admin
screen for a value that changes once a year. When it needs to change more
often than that, it has earned a table.

Comparison is case-insensitive and whitespace-tolerant, because an address
typed into a Terraform variable by a human is not a normalised string.
Gmail's dots-and-plus-addressing are **not** normalised away: treating
`a.b@gmail.com` and `ab@gmail.com` as one address is a Gmail-specific rule
that is wrong everywhere else, and guessing wrong here either lets in someone
who should not be here or locks out someone who should.
"""

import os


def allowed() -> set:
    raw = os.environ.get("ALLOWED_EMAILS", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def lambda_handler(event, context):
    email = ((event.get("request") or {})
             .get("userAttributes") or {}).get("email", "")
    email = email.strip().lower()

    permitted = allowed()

    # An empty list denies everyone. The other reading -- "unconfigured means
    # open" -- is how a misconfiguration becomes a public API, and a locked
    # door that should have been open is a five-minute fix while the reverse
    # is not.
    if not permitted:
        print("ALLOWED_EMAILS is empty; refusing every sign-up")
        raise Exception("This application is not accepting new users.")

    if email not in permitted:
        # Deliberately vague to the user, specific in the log. Telling a
        # stranger whether an address is on the list is an oracle.
        print(f"refused sign-up for {email!r}")
        raise Exception("This account is not permitted to use this application.")

    print(f"admitted {email!r}")

    # Google has already verified the address, and saying so here is what
    # stops Cognito emailing a confirmation code to somebody who did not ask
    # for one. Auto-confirm applies to the federated flow only, which is the
    # only flow this pool has.
    event["response"]["autoConfirmUser"] = True
    event["response"]["autoVerifyEmail"] = True
    return event
