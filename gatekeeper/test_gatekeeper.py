"""Tests for the door.

This Lambda is the entire access control of the application once Google
sign-in is on. Every other security property -- short tokens, no passwords,
Google's own second factor -- protects an account; this decides whether the
account may exist at all.

The organising assertion: **every unexpected shape denies.** An empty list, a
missing attribute, a malformed event. A door that fails open is not a door,
and this one fails open in exactly one way -- by returning normally -- so
every path that is not an explicit admission has to raise.
"""

import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))

_spec = importlib.util.spec_from_file_location(
    "gatekeeper_handler", os.path.join(_HERE, "handler.py"))
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)


def event(email):
    return {
        "triggerSource": "PreSignUp_ExternalProvider",
        "request": {"userAttributes": {"email": email}},
        "response": {},
    }


@pytest.fixture(autouse=True)
def allow(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "owner@example.com, second@example.com")


def test_a_listed_address_is_admitted():
    result = h.lambda_handler(event("owner@example.com"), None)
    assert result["response"]["autoConfirmUser"] is True


def test_a_verified_google_address_is_not_asked_to_verify_again():
    """Otherwise Cognito emails a confirmation code to someone who never asked
    for one, for an address Google has already proven."""
    result = h.lambda_handler(event("owner@example.com"), None)
    assert result["response"]["autoVerifyEmail"] is True


def test_a_stranger_is_refused():
    """Turning on Google sign-in turns it on for everyone who has a Google
    account. Without this, replacing the passcode is not an improvement."""
    with pytest.raises(Exception, match="not permitted"):
        h.lambda_handler(event("someone@example.com"), None)


def test_the_refusal_does_not_say_whether_the_address_is_known():
    """Telling a stranger whether an address is on the list is an oracle."""
    with pytest.raises(Exception) as caught:
        h.lambda_handler(event("someone@example.com"), None)
    assert "someone@example.com" not in str(caught.value)


def test_case_and_whitespace_do_not_lock_someone_out(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "  Owner@Example.COM ,x@y.z ")
    assert h.lambda_handler(event("owner@example.com"), None)
    assert h.lambda_handler(event("OWNER@EXAMPLE.COM"), None)


def test_gmail_dots_are_not_normalised_away(monkeypatch):
    """`a.b@gmail.com` and `ab@gmail.com` are the same mailbox at Gmail and
    different mailboxes almost everywhere else. Guessing either way lets in
    someone who should not be here, or locks out someone who should."""
    monkeypatch.setenv("ALLOWED_EMAILS", "ab@gmail.com")
    with pytest.raises(Exception):
        h.lambda_handler(event("a.b@gmail.com"), None)


# --- every unexpected shape denies -------------------------------------------

def test_an_empty_list_denies_everyone(monkeypatch):
    """The other reading -- unconfigured means open -- is how a
    misconfiguration becomes a public API."""
    monkeypatch.setenv("ALLOWED_EMAILS", "")
    with pytest.raises(Exception, match="not accepting"):
        h.lambda_handler(event("owner@example.com"), None)


def test_an_absent_variable_denies_everyone(monkeypatch):
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    with pytest.raises(Exception):
        h.lambda_handler(event("owner@example.com"), None)


def test_a_list_of_only_separators_denies_everyone(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", " , , ")
    with pytest.raises(Exception, match="not accepting"):
        h.lambda_handler(event("owner@example.com"), None)


@pytest.mark.parametrize("payload", [
    {},
    {"request": {}},
    {"request": {"userAttributes": {}}},
    {"request": {"userAttributes": {"email": ""}}},
    {"request": None},
    {"request": {"userAttributes": None}},
])
def test_a_malformed_event_denies(payload):
    """A door that fails open is not a door."""
    payload.setdefault("response", {})
    with pytest.raises(Exception):
        h.lambda_handler(payload, None)
