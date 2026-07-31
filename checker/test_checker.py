"""Tests for the Checker, which until now had none at all.

This is the hot path -- the code that runs on every tick of every watch -- and
every verification of it so far has been a manual Lambda invoke against real
AWS. That cannot reach the cases that matter most, which are all failure
shapes: an extractor that stops matching, a value that is implausible rather
than absent, an op nobody can evaluate, a Fetcher older than its caller.

The organising assertion throughout: **a broken extractor and an unmet
condition must never produce the same row.** One has to be visible to Phase 8d
and the other has to be left alone.

`checker/handler.py` is loaded by explicit path rather than `import handler`,
because `api/handler.py` exists too and a bare import would resolve to
whichever directory reached sys.path first.
"""

import importlib.util
import json
import os
import sys
import types
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# --- stub boto3 before handler.py builds its clients at import time ----------
if not isinstance(sys.modules.get("boto3"), types.ModuleType) or not hasattr(
    sys.modules.get("boto3", object()), "resource"
):
    _boto3 = types.ModuleType("boto3")
    _boto3.resource = MagicMock()
    _boto3.client = MagicMock()
    sys.modules["boto3"] = _boto3

# check.py imports the Anthropic SDK at module scope. It is only ever
# constructed inside judge(), which these tests replace, so the module needs to
# exist but not to work -- and the suite stays offline and free, which is the
# whole point of having it.
if "anthropic" not in sys.modules:
    _anthropic = types.ModuleType("anthropic")
    _anthropic.Anthropic = MagicMock()
    sys.modules["anthropic"] = _anthropic

os.environ.setdefault("WATCHES_TABLE", "watches")
os.environ.setdefault("WATCH_TARGETS_TABLE", "targets")
os.environ.setdefault("EVENT_BUS_NAME", "bus")
os.environ.setdefault("FETCHER_FUNCTION_ARN", "arn:aws:lambda:::function:fetcher")

# build.sh vendors shared/*.py next to handler.py in the zip, so at runtime
# these are flat imports. Reproduce that rather than reach for a package
# layout the Lambda does not have.
sys.path.insert(0, os.path.join(_ROOT, "shared"))
sys.path.insert(0, _HERE)

_spec = importlib.util.spec_from_file_location(
    "checker_handler", os.path.join(_HERE, "handler.py")
)
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)


# --- doubles -----------------------------------------------------------------

class FakeTable:
    """A DynamoDB table that records what was done to it."""

    def __init__(self, items=None):
        self.items = dict(items or {})
        self.updates = []

    def get_item(self, Key):  # noqa: N803 -- boto3's casing
        key = next(iter(Key.values()))
        item = self.items.get(key)
        return {"Item": item} if item is not None else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None):  # noqa: N803
        self.updates.append({
            "key": next(iter(Key.values())),
            "expression": UpdateExpression,
            "values": ExpressionAttributeValues or {},
        })

    def last_update(self):
        return self.updates[-1] if self.updates else None


PAGE = """
<html><body>
  <div id="offer" class="box">
    <span class="price">$429.00</span>
  </div>
  <div id="jobs">
    <a href="1">Senior Go Engineer</a>
    <a href="2">Platform Engineer</a>
  </div>
</body></html>
"""

PRICE_SPEC = {"scope": "#offer", "kind": "css", "selector": ".price",
              "parse": "currency"}


@pytest.fixture
def env(monkeypatch):
    """Wire the handler to fakes and hand back the pieces tests assert on."""
    targets = FakeTable()
    watches = FakeTable()
    monkeypatch.setattr(
        h.dynamodb, "Table",
        lambda name: {"targets": targets, "watches": watches}[name],
    )
    monkeypatch.setattr(h, "events", MagicMock())
    monkeypatch.setattr(h, "cloudwatch", MagicMock())
    monkeypatch.setattr(h, "lambda_client", MagicMock())
    monkeypatch.setattr(h, "fetch_raw", lambda url: PAGE)
    monkeypatch.setattr(h, "fetch_text", lambda url: "text version")
    monkeypatch.setattr(h, "judge", MagicMock(side_effect=AssertionError(
        "the model must not be called on the deterministic path")))
    return types.SimpleNamespace(targets=targets, watches=watches, module=h)


def make_watch(env, *, condition, status="active"):
    env.watches.items["w_1"] = {
        "watch_id": "w_1", "status": status, "prompt": "watch it",
        "condition": condition,
    }


def make_target(env, **overrides):
    target = {
        "target_id": "t_1", "watch_id": "w_1",
        "url": "https://example.com/offer",
        "fetch_method": "http",
        "extract_hint": "the price",
    }
    target.update(overrides)
    env.targets.items["t_1"] = target
    return target


def run(env):
    return h.lambda_handler({"target_id": "t_1"}, None)


def values_of(update):
    return update["values"] if update else {}


# --- Tier 0: the point of the whole phase ------------------------------------

def test_a_met_condition_triggers_without_any_model_call(env):
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor=PRICE_SPEC, verified_value=Decimal("439.00"))

    result = run(env)

    assert result["condition_met"] is True
    assert result["last_value"] == 429.00
    env.module.events.put_events.assert_called_once()
    detail = json.loads(
        env.module.events.put_events.call_args[1]["Entries"][0]["Detail"])
    assert detail["last_value"] == "$429.00"  # the verbatim string, for the email
    assert env.watches.last_update()["values"][":s"] == "triggered"


def test_an_unmet_condition_records_the_value_and_does_not_trigger(env):
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("400")})
    make_target(env, extractor=PRICE_SPEC, verified_value=Decimal("439.00"))

    result = run(env)

    assert result["condition_met"] is False
    env.module.events.put_events.assert_not_called()
    stored = values_of(env.targets.last_update())
    assert stored[":v"] == Decimal("429.0")
    assert stored[":s"] == "ok"


def test_the_cost_metric_says_no_model_was_used(env):
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("400")})
    make_target(env, extractor=PRICE_SPEC)
    run(env)

    published = env.module.cloudwatch.put_metric_data.call_args[1]["MetricData"][0]
    dimensions = {d["Name"]: d["Value"] for d in published["Dimensions"]}
    assert dimensions["UsedModel"] == "false"
    # The whole justification for the phase: a check must now cost ~nothing.
    assert published["Value"] < 0.0001


# --- the distinction 8d depends on -------------------------------------------

def test_a_broken_extractor_is_recorded_as_failed(env):
    """The anchor is gone, so the page changed shape. This has to be visible."""
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor={**PRICE_SPEC, "scope": "#offer_v2"})

    result = run(env)

    assert result["status"] == "failed"
    stored = values_of(env.targets.last_update())
    assert stored[":s"] == "failed"
    assert "scope matched nothing" in stored[":e"]
    env.module.events.put_events.assert_not_called()


def test_an_absent_value_is_unavailable_and_carries_no_error(env):
    """Inside a proven anchor, "not there" means not there yet. 8d must not
    spend a repair call on this."""
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor={**PRICE_SPEC, "selector": ".flash_price"})

    result = run(env)

    assert result["status"] == "unavailable"
    assert result["condition_met"] is False
    stored = values_of(env.targets.last_update())
    assert stored[":s"] == "unavailable"
    assert ":e" not in stored
    assert "REMOVE last_error" in env.targets.last_update()["expression"]


def test_failed_and_unavailable_do_not_write_the_same_row(env):
    """Stated directly, because collapsing them is how a watch dies quietly."""
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})

    make_target(env, extractor={**PRICE_SPEC, "selector": ".flash_price"})
    run(env)
    absent = values_of(env.targets.last_update())

    make_target(env, extractor={**PRICE_SPEC, "scope": "#gone"})
    run(env)
    broken = values_of(env.targets.last_update())

    assert absent[":s"] != broken[":s"]
    assert ":e" in broken and ":e" not in absent


# --- guards against reading the wrong thing ----------------------------------

def test_an_implausible_reading_is_failed_not_reported(env):
    """A selector that survives a redesign but now points at a review count
    fails silently in a way a missing selector does not."""
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor=PRICE_SPEC, verified_value=Decimal("42999.00"))

    result = run(env)

    assert result["status"] == "failed"
    assert "implausible" in values_of(env.targets.last_update())[":e"]


def test_a_count_is_exempt_from_the_plausibility_check(env):
    """0 against a baseline of 2 is the normal life of a vacancy watch, and
    would look wildly implausible to a ratio test built for prices."""
    make_watch(env, condition={"metric": "vacancies", "op": ">=",
                               "value": Decimal("1")})
    make_target(
        env,
        extractor={"scope": "#jobs", "kind": "count",
                   "selector": 'a:-soup-contains("Rust")'},
        verified_value=Decimal("2"),
    )

    result = run(env)

    assert result["status"] == "ok"
    assert result["last_value"] == 0
    assert result["condition_met"] is False


def test_a_vacancy_appearing_fires_the_watch(env):
    make_watch(env, condition={"metric": "vacancies", "op": ">=",
                               "value": Decimal("1")})
    make_target(env, extractor={"scope": "#jobs", "kind": "count",
                                "selector": 'a:-soup-contains("Platform")'})

    result = run(env)

    assert result["condition_met"] is True
    env.module.events.put_events.assert_called_once()


def test_an_unevaluable_op_is_failed_rather_than_not_met(env):
    """Answering "not met" would produce a watch that is alive, billed, checked
    on schedule and structurally incapable of ever firing."""
    make_watch(env, condition={"metric": "price", "op": "vaguely_near",
                               "value": Decimal("450")})
    make_target(env, extractor=PRICE_SPEC)

    result = run(env)

    assert result["status"] == "failed"
    assert "not evaluable" in values_of(env.targets.last_update())[":e"]


def test_a_malformed_spec_is_failed_not_a_crash(env):
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor={"kind": "telepathy", "selector": ".price"})

    result = run(env)

    assert result["status"] == "failed"
    assert "invalid extractor spec" in values_of(env.targets.last_update())[":e"]


# --- compatibility -----------------------------------------------------------

def test_a_row_without_an_extractor_still_uses_the_model(env, monkeypatch):
    """Rows written before Phase 8b exist. They must keep working rather than
    start failing the moment the Checker is redeployed."""
    monkeypatch.setattr(h, "judge", MagicMock(return_value={
        "last_value": "$429.00", "condition_met": True, "note": "found it"}))
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env)  # no extractor

    result = run(env)

    assert result["condition_met"] is True
    h.judge.assert_called_once()
    dimensions = {
        d["Name"]: d["Value"]
        for d in env.module.cloudwatch.put_metric_data.call_args[1]
        ["MetricData"][0]["Dimensions"]
    }
    assert dimensions["UsedModel"] == "true"


def test_a_browser_fetcher_without_html_fails_loudly(env, monkeypatch):
    """Feeding stripped text to a CSS extractor reads as "selector matched
    nothing" -- a broken plan. Say what actually happened instead."""
    payload = MagicMock()
    payload.read.return_value = json.dumps({"text": "just words", "url": "u"})
    monkeypatch.setattr(h.lambda_client, "invoke",
                        MagicMock(return_value={"Payload": payload}))
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor=PRICE_SPEC, fetch_method="browser")

    result = run(env)

    assert result["checked"] is False
    assert "update-function-code" in values_of(env.targets.last_update())[":e"]


def test_a_watch_that_is_not_active_is_never_checked(env):
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")}, status="paused")
    make_target(env, extractor=PRICE_SPEC)

    result = run(env)

    assert result == {"skipped": True, "status": "paused"}
    assert env.targets.updates == []
