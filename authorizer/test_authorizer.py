"""Tests for the passcode authorizer.

This is the only thing standing between the public internet and an API that
can spend money -- every route creates, confirms or deletes watches, and a
watch bills an Anthropic account. It had no tests.

Most of what is worth checking here is not "does the right passcode work"
but the ways a comparison like this is usually got wrong: a prefix passing,
an empty string passing, a header the runtime never actually sends, and the
rejected guess ending up in CloudWatch Logs where more people can read it
than should ever see a passcode.
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))

# Replaced unconditionally, and loaded by path under a unique module name --
# see the same block in notifier/test_notifier.py for why. Short version:
# three Lambdas here have a module called `handler`, and a guarded stub makes
# the suite's behaviour depend on collection order.
_boto3 = types.ModuleType("boto3")
_boto3.client = MagicMock()
_boto3.resource = MagicMock()
sys.modules["boto3"] = _boto3

os.environ.setdefault("PASSCODE_PARAM", "/schedule-ai-app/passcode")

_spec = importlib.util.spec_from_file_location(
    "authorizer_handler", os.path.join(_HERE, "handler.py"))
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

PASSCODE = "correct-horse-battery-staple"


@pytest.fixture
def ssm(monkeypatch):
    """A fresh SSM stub and an empty cache for every test.

    The cache is a module-level global on purpose -- it is what stops a busy
    API paying for an SSM call per request -- so it leaks between tests unless
    it is reset here.
    """
    monkeypatch.setattr(handler, "_passcode_cache", None)
    client = MagicMock()
    client.get_parameter.return_value = {"Parameter": {"Value": PASSCODE}}
    monkeypatch.setattr(handler, "ssm", client)
    return client


def call(authorization=None, *, headers=..., route="GET /watches"):
    event = {"routeKey": route}
    if headers is not ...:
        event["headers"] = headers
    elif authorization is not None:
        event["headers"] = {"authorization": authorization}
    return handler.lambda_handler(event, None)


# --------------------------------------------------------------------------
# The decision itself
# --------------------------------------------------------------------------

def test_the_right_passcode_is_allowed(ssm):
    assert call(PASSCODE) == {"isAuthorized": True}


def test_a_wrong_passcode_is_denied(ssm):
    assert call("hunter2") == {"isAuthorized": False}


def test_a_correct_prefix_is_not_enough(ssm):
    """The failure mode of a hand-rolled comparison: something that starts
    right passing. Also the thing an attacker probes for one byte at a time,
    which is why the real code uses compare_digest."""
    assert call(PASSCODE[:-1]) == {"isAuthorized": False}


def test_a_longer_string_that_starts_correctly_is_denied(ssm):
    assert call(PASSCODE + "x") == {"isAuthorized": False}


def test_the_comparison_is_constant_time(ssm):
    """Asserted structurally rather than by timing, which would be flaky.

    A `==` here would leak how much of the prefix was right through how long
    the reply took. This test exists so that swapping it back is a failure
    rather than a silent regression nobody notices.
    """
    import inspect
    source = inspect.getsource(handler.lambda_handler)
    assert "compare_digest" in source


# --------------------------------------------------------------------------
# Nothing may pass by accident
# --------------------------------------------------------------------------

@pytest.mark.parametrize("supplied", ["", "   ", "Bearer ", "bearer   "])
def test_an_effectively_empty_passcode_is_denied(ssm, supplied):
    assert call(supplied) == {"isAuthorized": False}


def test_a_request_with_no_headers_at_all_is_denied(ssm):
    assert call(headers=...) == {"isAuthorized": False}


def test_a_null_headers_object_is_denied(ssm):
    """API Gateway sends `headers: null`, not `{}`, on a request that carried
    none -- so `event["headers"] or {}` is load-bearing, not defensive."""
    assert call(headers=None) == {"isAuthorized": False}


def test_an_empty_passcode_never_reaches_ssm(ssm):
    """Denying before the lookup keeps an unauthenticated flood from turning
    into a billed SSM call per request."""
    call("")
    ssm.get_parameter.assert_not_called()


# --------------------------------------------------------------------------
# How the passcode is allowed to be spelled
# --------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["Bearer ", "bearer ", "BEARER ", "BeArEr "])
def test_the_bearer_prefix_is_accepted_in_any_case(ssm, prefix):
    assert call(prefix + PASSCODE) == {"isAuthorized": True}


def test_surrounding_whitespace_is_forgiven(ssm):
    assert call(f"  {PASSCODE}  ") == {"isAuthorized": True}


def test_a_capitalised_header_name_is_not_read(ssm):
    """Documents a real dependency rather than endorsing it.

    HTTP APIs lowercase every header name, so reading only `authorization` is
    correct here. REST APIs and direct invokes do not, and this authorizer
    would silently deny everything if moved to one. The frontend and curl are
    unaffected -- they never see this key.
    """
    assert call(headers={"Authorization": PASSCODE}) == {"isAuthorized": False}


# --------------------------------------------------------------------------
# The secret must not leak, into logs or into extra API calls
# --------------------------------------------------------------------------

def test_a_rejected_guess_is_never_logged(ssm, capsys):
    """A wrong passcode is still a secret somebody typed -- very often their
    correct passcode for something else."""
    call("my-actual-banking-password")
    assert "my-actual-banking-password" not in capsys.readouterr().out


def test_the_real_passcode_is_never_logged(ssm, capsys):
    call(PASSCODE)
    assert PASSCODE not in capsys.readouterr().out


def test_a_denial_still_records_which_route_was_probed(ssm, capsys):
    call("wrong", route="DELETE /watches/{id}")
    assert "DELETE /watches/{id}" in capsys.readouterr().out


def test_the_passcode_is_fetched_once_and_reused(ssm):
    """Warm containers must not pay an SSM call per request."""
    for _ in range(5):
        call(PASSCODE)
    assert ssm.get_parameter.call_count == 1


def test_the_lookup_asks_for_decryption(ssm):
    """The parameter is a SecureString; without WithDecryption the value comes
    back as ciphertext and every request is denied."""
    call(PASSCODE)
    kwargs = ssm.get_parameter.call_args.kwargs
    assert kwargs["Name"] == "/schedule-ai-app/passcode"
    assert kwargs["WithDecryption"] is True


def test_a_rotated_passcode_is_not_picked_up_until_the_cache_clears(ssm):
    """Documents the stated trade-off: rotation takes effect on the next cold
    start. If this ever needs to be immediate, it is this test that changes."""
    assert call(PASSCODE) == {"isAuthorized": True}
    ssm.get_parameter.return_value = {"Parameter": {"Value": "rotated"}}

    assert call("rotated") == {"isAuthorized": False}
    assert call(PASSCODE) == {"isAuthorized": True}
