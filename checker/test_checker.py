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
from datetime import datetime, timedelta, timezone
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

# `boto3.dynamodb.conditions.Key` is imported by handler.py for the sibling
# query. A stub `boto3` is a plain module, not a package, so the submodule has
# to be registered explicitly or the import fails at collection -- and it fails
# regardless of whether a real boto3 is installed, because the stub above
# replaced it. Same shape as notifier/test_notifier.py.
_conditions = types.ModuleType("boto3.dynamodb.conditions")


class Key:
    """Just enough of boto3's Key to record what a query asked for."""

    def __init__(self, name):
        self.name = name

    def eq(self, value):
        return {"key": self.name, "eq": value}


_conditions.Key = Key
sys.modules.setdefault("boto3.dynamodb", types.ModuleType("boto3.dynamodb"))
sys.modules["boto3.dynamodb.conditions"] = _conditions

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

class ConditionalCheckFailedException(Exception):
    """Named exactly as boto3 names it, because handler.py matches on the name.

    The resource API builds its exception classes dynamically off the service
    model, so there is no importable class a test can raise and no class the
    handler can catch. Matching the name is the honest way to bridge that, and
    this double exists to keep the test honest about it.
    """


class FakeTable:
    """A DynamoDB table that records what was done to it."""

    def __init__(self, items=None, pages=None):
        self.items = dict(items or {})
        self.updates = []
        self.queries = []
        # Successive query() responses, so pagination is assertable.
        self.pages = list(pages or [])

    def get_item(self, Key):  # noqa: N803 -- boto3's casing
        key = next(iter(Key.values()))
        item = self.items.get(key)
        return {"Item": item} if item is not None else {}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        if not self.pages:
            return {"Items": list(self.items.values())}
        return self.pages.pop(0)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None,
                    ConditionExpression=None):  # noqa: N803
        key = next(iter(Key.values()))
        values = ExpressionAttributeValues or {}
        # Only the one condition the handler uses is modelled: "the watch is
        # still active". Anything else would be a fake pretending to be
        # DynamoDB, which is a worse lie than not supporting it.
        if ConditionExpression == "#s = :active":
            current = (self.items.get(key) or {}).get("status")
            if current != values.get(":active"):
                raise ConditionalCheckFailedException(ConditionExpression)
            self.items[key] = {**self.items[key], "status": values.get(":s")}
        self.updates.append({
            "key": key,
            "expression": UpdateExpression,
            "values": values,
            "condition": ConditionExpression,
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


def extract_ids(html):
    """The ids the engine will produce for a page, so a test can pre-seed
    "already reported" without hardcoding a hash."""
    import extract as extract_mod
    return [i["id"] for i in extract_mod.extract(COUNT_SPEC, html).items]


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


# --- Tier 1 and Tier 2: the model returns, as repair only ---------------------
#
# The bargain that makes a compiled extractor safe to leave running for months:
# a selector will eventually stop matching, and something has to notice. What
# earns a repair is `failed` and never `unavailable` -- repairing a healthy
# extractor that is correctly reporting "not yet" would pay a model on every
# tick, forever, which is the cost this whole phase exists to remove.

BROKEN_SPEC = {**PRICE_SPEC, "scope": "#offer_v2"}
FIXED = {"extractor": PRICE_SPEC, "value": 429.00, "raw": "$429.00"}


def wire_repair(env, monkeypatch, outcome=FIXED, fail=False):
    calls = []

    def fake(old_spec, error, hint, payload, baseline=None):
        calls.append({"spec": old_spec, "error": error, "payload": payload,
                      "baseline": baseline})
        if fail:
            raise ValueError("could not find the value on the page")
        return outcome

    monkeypatch.setattr(h, "repair", fake)
    return calls


def test_a_broken_extractor_is_repaired_and_the_tick_continues(env, monkeypatch):
    calls = wire_repair(env, monkeypatch)
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor=BROKEN_SPEC, verified_value=Decimal("439.00"))

    result = run(env)

    assert len(calls) == 1
    assert result["status"] == "ok"
    assert result["condition_met"] is True
    # The repair was derived from the page already in hand, not a second fetch.
    assert calls[0]["payload"] == PAGE
    stored = [u for u in env.targets.updates if "extractor = :x" in u["expression"]]
    assert stored and stored[0]["values"][":x"] == PRICE_SPEC
    # The old spec is kept, so a bad repair can be seen rather than guessed at.
    assert stored[0]["values"][":p"] == BROKEN_SPEC


def test_a_repair_resets_the_failure_count(env, monkeypatch):
    wire_repair(env, monkeypatch)
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("400")})
    make_target(env, extractor=BROKEN_SPEC, consecutive_failures=Decimal("2"))

    run(env)

    assert values_of(env.targets.last_update())[":f"] == 0


def test_an_unavailable_reading_is_never_repaired(env, monkeypatch):
    """The single most expensive mistake available here. `unavailable` means
    the extractor works and there is legitimately nothing today."""
    calls = wire_repair(env, monkeypatch)
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor={**PRICE_SPEC, "selector": ".flash_price"})

    result = run(env)

    assert result["status"] == "unavailable"
    assert calls == []


def test_a_failed_repair_costs_money_and_counts_towards_giving_up(env, monkeypatch):
    wire_repair(env, monkeypatch, fail=True)
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor=BROKEN_SPEC)

    result = run(env)

    stored = values_of(env.targets.last_update())
    assert result["status"] == "failed"
    assert stored[":f"] == 1
    assert float(stored[":r"]) > 0


def test_the_third_consecutive_failure_degrades_the_watch(env, monkeypatch):
    """Tier 2. A watch that cannot read its target has to say so -- silence is
    indistinguishable from a condition that is simply not met."""
    wire_repair(env, monkeypatch, fail=True)
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor=BROKEN_SPEC, consecutive_failures=Decimal("2"))

    result = run(env)

    assert result["status"] == "degraded"
    assert env.watches.last_update()["values"][":s"] == "degraded"
    env.module.events.put_events.assert_called_once()
    entry = env.module.events.put_events.call_args[1]["Entries"][0]
    assert entry["DetailType"] == "WatchDegraded"
    assert "scope matched nothing" in json.loads(entry["Detail"])["reason"]


def test_a_watch_that_cannot_afford_a_repair_degrades_immediately(env, monkeypatch):
    """Repairs share the watch's monthly budget rather than getting their own,
    so an expensive watch that breaks is escalated instead of quietly
    overspending."""
    calls = wire_repair(env, monkeypatch)
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor=BROKEN_SPEC,
                repair_spend_usd=Decimal("4.999"))

    result = run(env)

    assert calls == []
    assert result["status"] == "degraded"


def test_a_legacy_row_without_an_extractor_is_never_repaired(env, monkeypatch):
    """There is no spec to repair, and judge() reports its own trouble."""
    calls = wire_repair(env, monkeypatch)
    monkeypatch.setattr(h, "judge", MagicMock(return_value={
        "last_value": "$429.00", "condition_met": False, "note": ""}))
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env)

    run(env)
    assert calls == []


def test_degrading_records_why_and_what_it_cost(env, monkeypatch):
    wire_repair(env, monkeypatch, fail=True)
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor=BROKEN_SPEC, consecutive_failures=Decimal("2"),
                repair_spend_usd=Decimal("0.016"))

    run(env)

    detail = json.loads(
        env.module.events.put_events.call_args[1]["Entries"][0]["Detail"])
    assert detail["repair_spend_usd"] > 0.016
    assert env.watches.last_update()["values"][":r"]


# --- Did the number actually move? -------------------------------------------
#
# Nothing used to ask. A frozen feed -- a halt, a delisting, a plausible but
# dormant ticker -- read `ok` forever, a `!=` watch never fired, no error was
# recorded, and the price appeared simply never to have moved.

def test_a_first_reading_counts_as_a_change(env):
    """There is nothing to have differed from, so the value moved *now*."""
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("400")})
    make_target(env, extractor=PRICE_SPEC)

    run(env)
    stored = values_of(env.targets.last_update())

    assert stored[":u"] == 0
    assert stored[":c"] == stored[":t"]


def test_an_unchanged_reading_increments_the_counter_and_keeps_the_timestamp(env):
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("400")})
    make_target(env, extractor=PRICE_SPEC,
                last_raw="$429.00", last_value=Decimal("429.0"),
                last_changed_at="2026-08-01T09:00:00+00:00",
                unchanged_checks=6)

    run(env)
    stored = values_of(env.targets.last_update())

    assert stored[":u"] == 7
    # The whole point: the moment it last *moved* must not creep forward on
    # every check, or "unchanged since" would always read "just now".
    assert stored[":c"] == "2026-08-01T09:00:00+00:00"


def test_a_changed_reading_resets_both(env):
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("400")})
    make_target(env, extractor=PRICE_SPEC,
                last_raw="$999.99", last_value=Decimal("999.99"),
                last_changed_at="2026-08-01T09:00:00+00:00",
                unchanged_checks=41)

    run(env)
    stored = values_of(env.targets.last_update())

    assert stored[":u"] == 0
    assert stored[":c"] == stored[":t"]


def test_comparison_is_on_the_raw_text_not_the_decimal(env):
    """`Decimal("429.00") == 429.0` is fine, but `Decimal("306.49") == 306.49`
    is **False** -- the float is a binary approximation of a value the Decimal
    holds exactly. Comparing those would report a change on every check of a
    completely static price, which fails safe and makes the field useless.
    """
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("400")})
    make_target(env, extractor=PRICE_SPEC,
                last_raw="$429.00",
                # Deliberately a Decimal that does not compare equal to the
                # float the extractor will produce.
                last_value=Decimal("429.000000000000001"),
                unchanged_checks=2)

    run(env)

    assert values_of(env.targets.last_update())[":u"] == 3


def test_a_legacy_row_without_the_fields_does_not_crash(env):
    """Every target created before this existed has neither field."""
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("400")})
    make_target(env, extractor=PRICE_SPEC, last_raw="$429.00")

    run(env)

    assert values_of(env.targets.last_update())[":u"] == 1


def test_a_failed_check_leaves_the_movement_fields_alone(env):
    """A check that could not read anything has no opinion about whether the
    value moved. Incrementing on it would count outages as stillness."""
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("400")})
    make_target(env, extractor={"kind": "css", "selector": ".nope",
                                "parse": "currency"},
                unchanged_checks=5)

    run(env)
    stored = values_of(env.targets.last_update())

    assert ":u" not in stored
    assert ":c" not in stored


# --- Repeating watches: report each posting once, then keep going ------------

COUNT_SPEC = {"scope": ".results", "kind": "count",
              "selector": 'a.job:-soup-contains("Cloud")', "parse": "int"}

JOBS_HTML = """<div class="results">
  <a class="job" href="/jobs/1">Junior Cloud Engineer - Student</a>
  <a class="job" href="/jobs/2">Head Chef</a>
</div>"""


def a_repeating_watch(env, monkeypatch, *, html=JOBS_HTML, **target):
    env.watches.items["w_1"] = {
        "watch_id": "w_1", "status": "active", "prompt": "a cloud job",
        "condition": {"metric": "count", "op": ">", "value": Decimal("0")},
        "repeating": True,
    }
    monkeypatch.setattr(env.module, "fetch_raw", lambda *a, **k: html)
    return make_target(env, extractor=COUNT_SPEC, **target)


def fired_detail(env):
    return json.loads(
        env.module.events.put_events.call_args[1]["Entries"][0]["Detail"])


def test_a_repeating_watch_reports_the_posting_not_the_count(env, monkeypatch):
    """The whole point. The email used to say "1" and link to the search
    page, leaving the user to go and find the job themselves."""
    a_repeating_watch(env, monkeypatch)

    run(env)
    detail = fired_detail(env)

    assert detail["repeating"] is True
    assert [i["text"] for i in detail["items"]] == [
        "Junior Cloud Engineer - Student"]
    assert detail["items"][0]["href"] == "/jobs/1"


def test_a_repeating_watch_stays_active(env, monkeypatch):
    """A job search is a stream. Going terminal on the first posting is the
    behaviour this feature exists to remove."""
    a_repeating_watch(env, monkeypatch)

    run(env)
    stored = values_of(env.watches.last_update())

    assert ":s" not in stored          # status untouched
    assert stored[":n"] == 1           # trigger_count


def test_the_same_posting_is_not_reported_twice(env, monkeypatch):
    """Without this a vacancy watch emails every tick for as long as the
    posting stays on the page -- worse than the silence it was built to fix."""
    seen = extract_ids(JOBS_HTML)
    a_repeating_watch(env, monkeypatch, seen_item_ids=seen)

    result = run(env)

    assert result["condition_met"] is True   # the condition is still met
    assert result["notified"] is False       # and nothing was sent
    env.module.events.put_events.assert_not_called()


def test_only_the_new_posting_is_reported(env, monkeypatch):
    two = """<div class="results">
      <a class="job" href="/jobs/1">Junior Cloud Engineer - Student</a>
      <a class="job" href="/jobs/5">Cloud Architect</a>
    </div>"""
    a_repeating_watch(env, monkeypatch, html=two,
                      seen_item_ids=extract_ids(JOBS_HTML))

    run(env)
    detail = fired_detail(env)

    assert [i["text"] for i in detail["items"]] == ["Cloud Architect"]


def test_what_was_reported_is_remembered(env, monkeypatch):
    a_repeating_watch(env, monkeypatch)
    run(env)

    stored = [u for u in env.targets.updates
              if "seen_item_ids" in u["expression"]]
    assert len(stored) == 1
    assert len(stored[0]["values"][":s"]) == 1


def test_the_memory_is_bounded(env, monkeypatch):
    """A board churns, and a target row stops at 400KB."""
    a_repeating_watch(env, monkeypatch,
                      seen_item_ids=[f"old{n:09d}" for n in range(600)])
    run(env)

    stored = [u for u in env.targets.updates
              if "seen_item_ids" in u["expression"]][0]
    assert len(stored["values"][":s"]) == h.MAX_SEEN_ITEMS


def test_a_repeating_watch_with_nothing_to_identify_falls_back_to_one_shot(env):
    """Deduplication needs items. Without them the safe direction is one-shot:
    it can fire once too few, never once per tick forever."""
    env.watches.items["w_1"] = {
        "watch_id": "w_1", "status": "active", "prompt": "a price",
        "condition": {"metric": "price", "op": "<", "value": Decimal("450")},
        "repeating": True,
    }
    make_target(env, extractor=PRICE_SPEC, verified_value=Decimal("439.00"))

    run(env)

    assert values_of(env.watches.last_update())[":s"] == "triggered"
    assert fired_detail(env)["repeating"] is False


def test_a_one_shot_watch_is_completely_unchanged(env):
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor=PRICE_SPEC, verified_value=Decimal("439.00"))

    run(env)

    assert values_of(env.watches.last_update())[":s"] == "triggered"
    assert fired_detail(env)["repeating"] is False


# --- Expiry: the only unbounded cost this system has ever had ----------------

def test_an_expired_watch_stops_before_it_fetches_anything(env, monkeypatch):
    called = []
    monkeypatch.setattr(env.module, "fetch_raw",
                        lambda *a, **k: called.append(1) or JOBS_HTML)
    a_repeating_watch(env, monkeypatch)
    env.watches.items["w_1"]["expires_at"] = "2020-01-01T00:00:00+00:00"

    result = run(env)

    assert result["status"] == "expired"
    assert called == []
    assert values_of(env.watches.last_update())[":s"] == "expired"


def test_expiry_says_it_is_not_a_fault(env, monkeypatch):
    """Reusing WatchDegraded is a plumbing decision -- both need "email, then
    tear down". The wording the user reads must not claim something broke."""
    a_repeating_watch(env, monkeypatch)
    env.watches.items["w_1"]["expires_at"] = "2020-01-01T00:00:00+00:00"
    env.watches.items["w_1"]["trigger_count"] = 4

    run(env)
    entry = env.module.events.put_events.call_args[1]["Entries"][0]
    detail = json.loads(entry["Detail"])

    assert entry["DetailType"] == "WatchDegraded"
    assert detail["reason_kind"] == "expired"
    assert detail["trigger_count"] == 4


def test_a_watch_with_no_expiry_runs_on(env):
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("400")})
    make_target(env, extractor=PRICE_SPEC)

    assert run(env)["checked"] is True


# --- Ranking: judge what appeared, once, against what was asked ---------------
#
# Two properties matter more than the ordering. Nothing may be re-reported that
# ranking set aside, and nothing about ranking may withhold a notification.

def wire_rank(env, monkeypatch, ranked=None, spend=0.003, fail=False):
    calls = []

    def fake(request, items, **kwargs):
        calls.append((request, items))
        if fail:
            return items, 0.0
        return (items if ranked is None else ranked), spend

    monkeypatch.setattr(env.module, "rank", fake)
    return calls


def test_ranking_sees_the_request_and_only_the_new_items(env, monkeypatch):
    a_repeating_watch(env, monkeypatch)
    calls = wire_rank(env, monkeypatch)

    run(env)

    assert len(calls) == 1
    request, items = calls[0]
    assert request == "a cloud job"
    assert [i["text"] for i in items] == ["Junior Cloud Engineer - Student"]


def test_only_the_ranked_items_are_emailed(env, monkeypatch):
    a_repeating_watch(env, monkeypatch)
    wire_rank(env, monkeypatch, ranked=[
        {"id": "x", "text": "Junior Cloud Engineer - Student", "href": "/1",
         "score": 9, "why": "student cloud role"}])

    run(env)
    detail = fired_detail(env)

    assert detail["items"][0]["score"] == 9
    assert detail["items"][0]["why"] == "student cloud role"


def test_what_ranking_set_aside_is_still_remembered(env, monkeypatch):
    """The bug this guards. Remembering only what was *reported* would bring
    every rejected posting back on the next tick, to be paid for and rejected
    again, for as long as it stayed on the board."""
    a_repeating_watch(env, monkeypatch)
    wire_rank(env, monkeypatch, ranked=[])      # everything judged irrelevant

    run(env)

    remembered = [u for u in env.targets.updates
                  if "seen_item_ids" in u["expression"]][0]
    assert len(remembered["values"][":s"]) == 1


def test_nothing_relevant_means_no_email(env, monkeypatch):
    """A successful outcome, not a silent failure: the postings were seen,
    judged, remembered and paid for, and none of them was the job."""
    a_repeating_watch(env, monkeypatch)
    wire_rank(env, monkeypatch, ranked=[])

    result = run(env)

    assert result["notified"] is False
    env.module.events.put_events.assert_not_called()


def test_a_watch_that_said_nothing_does_not_count_a_trigger(env, monkeypatch):
    """`trigger_count` is read back to the user by the expiry email -- "it told
    you about N things" -- so it counts emails, not ticks."""
    a_repeating_watch(env, monkeypatch)
    wire_rank(env, monkeypatch, ranked=[])

    run(env)

    assert env.watches.updates == []


def test_the_spend_is_recorded_against_the_watch(env, monkeypatch):
    a_repeating_watch(env, monkeypatch)
    wire_rank(env, monkeypatch, spend=0.004)

    run(env)

    assert float(values_of(env.watches.last_update())[":r"]) == 0.004


def test_spend_accumulates_across_notifications(env, monkeypatch):
    a_repeating_watch(env, monkeypatch)
    env.watches.items["w_1"]["rank_spend_usd"] = Decimal("0.010")
    wire_rank(env, monkeypatch, spend=0.004)

    run(env)

    assert float(values_of(env.watches.last_update())[":r"]) == 0.014


def test_a_watch_over_budget_stops_ranking_and_keeps_notifying(env, monkeypatch):
    """Ranking is what degrades when the money runs out, never the
    notification. That is the right way round."""
    a_repeating_watch(env, monkeypatch)
    env.watches.items["w_1"]["rank_spend_usd"] = Decimal("99")
    calls = wire_rank(env, monkeypatch)

    result = run(env)

    assert calls == []                       # never asked
    assert result["notified"] is True        # still emailed
    assert len(fired_detail(env)["items"]) == 1


def test_a_one_shot_watch_is_never_ranked(env, monkeypatch):
    """There is no stream to judge and no request-shaped criteria to judge
    against -- a price either crossed the threshold or it did not."""
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor=PRICE_SPEC, verified_value=Decimal("439.00"))
    calls = wire_rank(env, monkeypatch)

    run(env)

    assert calls == []
    assert values_of(env.watches.last_update())[":s"] == "triggered"


def test_the_answers_reach_the_ranker_as_criteria(env, monkeypatch):
    """The answers were given about the postings visible that day. They travel
    to the ranker as preferences, so a future job that misses one scores lower
    rather than disappearing -- the too-strict filter is the bug class this
    codebase keeps rediscovering."""
    a_repeating_watch(env, monkeypatch)
    env.watches.items["w_1"]["questions"] = [{
        "id": "seniority", "question": "Which level suits you?",
        "options": [{"value": "junior", "label": "Junior or student",
                     "items": ["x"]},
                    {"value": "senior", "label": "Senior", "items": ["y"]}],
    }]
    env.watches.items["w_1"]["answers"] = {"seniority": ["junior"]}

    seen = {}

    def fake(request, items, *, criteria="", **kwargs):
        seen["criteria"] = criteria
        return items, 0.001

    monkeypatch.setattr(env.module, "rank", fake)
    run(env)

    assert "Junior or student" in seen["criteria"]
    assert "Senior" not in seen["criteria"]


def test_an_unanswered_watch_ranks_exactly_as_before(env, monkeypatch):
    a_repeating_watch(env, monkeypatch)
    seen = {}

    def fake(request, items, *, criteria="", **kwargs):
        seen["criteria"] = criteria
        return items, 0.001

    monkeypatch.setattr(env.module, "rank", fake)
    run(env)

    assert seen["criteria"] == ""


# --- Watching the product, not the cheapest thing on the page ----------------

OFFERS_SPEC = {"kind": "offers", "parse": "float"}

SHOP_HTML = """<html><head>
<script type="application/ld+json">
{"@context":"http://schema.org/","@type":"ItemList","itemListElement":[
 {"@type":"ListItem","item":{"@type":"Product","name":"Xbox headset","sku":"H1",
  "offers":{"@type":"Offer","priceCurrency":"ILS","price":139,
  "availability":"http://schema.org/InStock","url":"https://shop/1"}}},
 {"@type":"ListItem","item":{"@type":"Product","name":"Xbox Series X 1TB","sku":"C9",
  "offers":{"@type":"Offer","priceCurrency":"ILS","price":1899,
  "availability":"http://schema.org/InStock","url":"https://shop/2"}}}
]}
</script></head><body></body></html>"""


def a_shop_watch(env, monkeypatch, *, html=SHOP_HTML, **target):
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("2000")})
    monkeypatch.setattr(env.module, "fetch_raw", lambda *a, **k: html)
    return make_target(env, extractor=OFFERS_SPEC, **target)


def console_id(html=SHOP_HTML):
    import extract as extract_mod
    return next(i["id"] for i in extract_mod.extract(OFFERS_SPEC, html).items
                if "Series X" in i["text"])


def test_without_a_pin_the_cheapest_thing_on_the_page_wins(env, monkeypatch):
    """The behaviour being fixed, kept as a test so the fix cannot silently
    regress: ILS 139 is a headset, and this watch would announce it as the
    price of a console."""
    a_shop_watch(env, monkeypatch)

    result = run(env)

    assert result["last_value"] == 139.0
    assert result["condition_met"] is True


def test_a_pinned_product_is_the_one_that_is_read(env, monkeypatch):
    a_shop_watch(env, monkeypatch, watched_ids=[console_id()])

    result = run(env)

    assert result["last_value"] == 1899.0
    assert values_of(env.targets.last_update())[":v"] == Decimal("1899.0")


def test_only_the_pinned_offers_are_carried_forward(env, monkeypatch):
    a_shop_watch(env, monkeypatch, watched_ids=[console_id()])
    run(env)

    stored = values_of(env.targets.last_update())[":i"]
    assert len(stored) == 1
    assert "Series X" in stored[0]["text"]


def test_a_pinned_product_that_is_not_listed_today_is_unavailable(env, monkeypatch):
    """Not `failed`. It is out of stock or delisted -- a legitimate state of
    the world, and the one thing 8d must never pay a model to repair."""
    a_shop_watch(env, monkeypatch, watched_ids=["nolongerhere"])

    result = run(env)

    assert result["status"] == "unavailable"
    assert result["condition_met"] is False
    assert values_of(env.targets.last_update()).get(":e") in (None, "")


def test_an_empty_pin_means_this_shop_has_none_of_it(env, monkeypatch):
    """Absent and empty are different, and conflating them was a real bug found
    by running this: a shop whose offers the answers ruled out entirely must
    not fall back to the cheapest thing on its page. Observed live -- bug.co.il
    and ivory.co.il were pinned to nothing for a console search and would have
    gone on reading a ILS 29 game."""
    a_shop_watch(env, monkeypatch, watched_ids=[])

    result = run(env)

    assert result["status"] == "unavailable"
    assert result["condition_met"] is False


def test_an_absent_pin_leaves_the_reading_alone(env, monkeypatch):
    """No answers were given, so there is no preference to apply."""
    a_shop_watch(env, monkeypatch)
    assert run(env)["last_value"] == 139.0


def test_a_pin_does_not_touch_a_watch_without_items(env, monkeypatch):
    """A price watch on one page has no offers to choose between."""
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor=PRICE_SPEC, watched_ids=["whatever"])

    assert run(env)["last_value"] == 429.0


# --- across shops: what the email can finally say -----------------------------
#
# A three-shop watch used to report one shop -- whichever ticked into the
# condition -- and left the obvious next question ("is that the cheapest?")
# unanswered by the one system that knew.
#
# The rule being protected in both directions: the summary must appear, and it
# must never be able to stop a notification.

ACROSS = {"metric": "price", "op": "<", "value": Decimal("2000"),
          "currency": "ILS", "across": "best"}


def a_multi_shop_watch(env, monkeypatch, *, siblings, condition=None,
                       interval=60, **target):
    make_watch(env, condition=condition or dict(ACROSS))
    env.watches.items["w_1"]["check_interval_min"] = Decimal(interval)
    monkeypatch.setattr(env.module, "fetch_raw", lambda *a, **k: SHOP_HTML)
    mine = make_target(env, extractor=OFFERS_SPEC, currency="ILS",
                       shop="ivory", watched_ids=[console_id()], **target)
    env.targets.pages = [{"Items": [mine] + siblings}]
    return mine


def sibling(shop, value, *, minutes=5, currency="ILS", status="ok"):
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    return {"target_id": f"t_{shop}", "watch_id": "w_1", "shop": shop,
            "currency": currency, "url": f"https://{shop}.example/s",
            "last_value": None if value is None else Decimal(str(value)),
            "last_status": status,
            "last_checked_at": when}


def emitted(env):
    detail = env.module.events.put_events.call_args[1]["Entries"][0]["Detail"]
    return json.loads(detail)


def test_a_firing_watch_reports_every_shop_best_first(env, monkeypatch):
    a_multi_shop_watch(env, monkeypatch, siblings=[
        sibling("bug", 2400.0), sibling("zap", 1500.0)])

    run(env)

    readings = emitted(env)["readings"]
    assert [r["shop"] for r in readings] == ["zap", "ivory", "bug"]
    assert [r["value"] for r in readings] == [1500.0, 1899.0, 2400.0]


def test_the_running_tick_reports_the_number_it_just_read(env, monkeypatch):
    """Not the one it is about to overwrite. Its own row is in the query result
    too, and reading that would be reading its own past."""
    a_multi_shop_watch(env, monkeypatch, siblings=[sibling("bug", 2400.0)],
                       last_value=Decimal("99999"), last_status="ok")
    run(env)

    mine = next(r for r in emitted(env)["readings"] if r["shop"] == "ivory")
    assert mine["value"] == 1899.0


def test_a_shop_in_another_currency_is_left_out_of_the_comparison(env, monkeypatch):
    a_multi_shop_watch(env, monkeypatch, siblings=[
        sibling("amazon", 34.99, currency="USD")])
    run(env)

    assert [r["shop"] for r in emitted(env)["readings"]] == ["ivory"]


def test_a_shop_that_has_gone_quiet_is_shown_but_never_first(env, monkeypatch):
    """Dropping it hides a shop the user asked about; letting it win reports a
    price nobody has confirmed for hours as though it were today's."""
    a_multi_shop_watch(env, monkeypatch,
                       siblings=[sibling("bug", 10.0, minutes=600)])
    run(env)

    readings = emitted(env)["readings"]
    assert [r["shop"] for r in readings] == ["ivory", "bug"]
    assert readings[1]["stale"] is True


def test_a_shop_that_read_nothing_is_not_in_the_comparison(env, monkeypatch):
    a_multi_shop_watch(env, monkeypatch,
                       siblings=[sibling("bug", None, status="unavailable")])
    run(env)

    assert [r["shop"] for r in emitted(env)["readings"]] == ["ivory"]


def test_a_single_target_watch_carries_no_summary(env, monkeypatch):
    """The email is exactly what it was before this existed."""
    a_multi_shop_watch(env, monkeypatch, siblings=[])
    run(env)

    assert emitted(env)["readings"] == []


def test_a_watch_without_across_never_queries_its_siblings(env, monkeypatch):
    """Paid per notification is already a saving; paid never is better. A
    single-shop watch has nothing to compare and must not pay for a query."""
    a_multi_shop_watch(env, monkeypatch, siblings=[sibling("bug", 2400.0)],
                       condition={"metric": "price", "op": "<",
                                  "value": Decimal("2000")})
    run(env)

    assert emitted(env)["readings"] == []
    assert env.targets.queries == []


def test_the_summary_is_only_paid_for_when_the_watch_speaks(env, monkeypatch):
    """A tick that finds nothing must not query the table to build a picture
    nobody will read."""
    a_multi_shop_watch(env, monkeypatch, siblings=[sibling("bug", 2400.0)],
                       condition=dict(ACROSS, value=Decimal("10")))

    assert run(env)["condition_met"] is False
    assert env.targets.queries == []


def test_a_failed_sibling_query_still_sends_the_email(env, monkeypatch):
    """Nothing about presentation may block a notification. The established
    rule, applied to the newest thing that could break one -- and the shape
    that matters, because the IAM permission for this query was added in the
    same change as the code that uses it."""
    a_multi_shop_watch(env, monkeypatch, siblings=[sibling("bug", 2400.0)])

    def denied(**kwargs):
        raise RuntimeError("AccessDeniedException: dynamodb:Query")

    monkeypatch.setattr(env.targets, "query", denied)

    result = run(env)

    assert result["notified"] is True
    assert emitted(env)["readings"] == []


def test_the_query_asks_the_index_for_this_watch(env, monkeypatch):
    a_multi_shop_watch(env, monkeypatch, siblings=[sibling("bug", 2400.0)])
    run(env)

    asked = env.targets.queries[0]
    assert asked["IndexName"] == "watch_id-index"
    assert asked["KeyConditionExpression"] == {"key": "watch_id", "eq": "w_1"}


# --- one event, however many shops cross at once ------------------------------

def test_a_watch_already_triggered_this_tick_does_not_email_twice(env, monkeypatch):
    """Every target of a watch is scheduled at the same interval and the
    schedules are created in the same second, so EventBridge fires them
    together. Two shops crossing the same threshold both flipped the watch to
    `triggered` and both published -- two emails about one event."""
    a_multi_shop_watch(env, monkeypatch, siblings=[sibling("bug", 2400.0)])
    env.watches.items["w_1"]["status"] = "active"

    # The other target got there first, between this one's read and its write.
    original = env.watches.update_item

    def race(**kwargs):
        env.watches.items["w_1"]["status"] = "triggered"
        env.watches.update_item = original
        return original(**kwargs)

    env.watches.update_item = race

    result = run(env)

    assert result["condition_met"] is True
    assert result["notified"] is False
    assert env.module.events.put_events.call_count == 0


def test_the_first_target_to_cross_still_fires_normally(env, monkeypatch):
    a_multi_shop_watch(env, monkeypatch, siblings=[sibling("bug", 2400.0)])

    assert run(env)["notified"] is True
    assert env.module.events.put_events.call_count == 1
    assert env.watches.items["w_1"]["status"] == "triggered"


def test_any_other_write_failure_is_still_an_error(env, monkeypatch):
    """The guard recognises one specific losing race. Everything else has to
    keep failing loudly -- swallowing a write error is how a watch quietly
    stops working."""
    a_multi_shop_watch(env, monkeypatch, siblings=[])

    def boom(**kwargs):
        raise RuntimeError("throughput exceeded")

    env.watches.update_item = boom

    with pytest.raises(RuntimeError, match="throughput"):
        run(env)


def test_a_repeating_watch_is_not_guarded_because_it_never_transitions(env, monkeypatch):
    """It stays `active` and speaks again next time; there is no once-only
    transition to protect, and a condition on `status` would be a lie about
    what the write means."""
    a_repeating_watch(env, monkeypatch)

    run(env)

    assert env.watches.last_update()["condition"] is None


# --- a source that refuses us -------------------------------------------------
#
# The third member of the family `unavailable`/`failed` belongs to, and it
# earns its place by what it prevents: without it, the day a large shop turns
# us away, every watch on that shop pays Haiku to repair an extractor that was
# never broken, then degrades separately -- N emails saying N things broke, and
# nothing saying the one true thing.

CAPTCHA = ("<html><title>Robot Check</title><body>"
           "<h1>Enter the characters you see below</h1></body></html>")


def a_blocked_watch(env, monkeypatch, *, page=CAPTCHA, **target):
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    monkeypatch.setattr(env.module, "fetch_raw", lambda *a, **k: page)
    return make_target(env, extractor=PRICE_SPEC,
                       url="https://www.amazon.com/s?k=xbox", **target)


def test_a_refusal_is_its_own_status_not_a_failure(env, monkeypatch):
    a_blocked_watch(env, monkeypatch)

    result = run(env)

    assert result["status"] == "blocked"
    assert values_of(env.targets.last_update())[":s"] == "blocked"


def test_a_refusal_never_pays_for_a_repair(env, monkeypatch):
    """The extractor is fine. Rewriting a selector against a captcha page
    cannot work, and would be paid on every tick of every watch on that shop
    for as long as the block lasted."""
    a_blocked_watch(env, monkeypatch)
    monkeypatch.setattr(env.module, "repair", MagicMock(side_effect=AssertionError(
        "a refusal must never be repaired")))

    assert run(env)["status"] == "blocked"


def test_a_refusal_does_not_count_towards_degrading(env, monkeypatch):
    """`consecutive_failures` is what 8d escalates on. Mixing refusals into it
    would stop a healthy watch three ticks after a site had a bad afternoon."""
    a_blocked_watch(env, monkeypatch, consecutive_failures=2)

    run(env)

    assert ":f" not in values_of(env.targets.last_update())
    assert values_of(env.targets.last_update())[":b"] == 1


def test_refusals_accumulate_on_their_own_counter(env, monkeypatch):
    a_blocked_watch(env, monkeypatch, consecutive_blocks=Decimal("4"))

    assert run(env)["blocked"] == 5


def test_the_reason_says_which_wall_and_whose(env, monkeypatch):
    a_blocked_watch(env, monkeypatch)
    run(env)

    recorded = values_of(env.targets.last_update())[":e"]
    assert "amazon.com" in recorded
    assert "bot check" in recorded


def test_the_block_is_published_as_one_metric(env, monkeypatch):
    """The whole point: an alarm on this is what turns "Amazon started
    blocking" from N broken watches into a single thing the owner is told."""
    a_blocked_watch(env, monkeypatch)
    run(env)

    published = [c for c in env.module.cloudwatch.put_metric_data.call_args_list
                 if any(m["MetricName"] == "BlockedFetches"
                        for m in c.kwargs["MetricData"])]
    assert len(published) == 1
    metrics = published[0].kwargs["MetricData"]
    # Once with the host, so it says which source; once bare, because a
    # dimensioned metric cannot be alarmed on without naming its dimensions.
    assert any(m.get("Dimensions") == [{"Name": "Host", "Value": "amazon.com"}]
               for m in metrics)
    assert any("Dimensions" not in m for m in metrics)


def test_a_watch_keeps_going_while_the_block_could_still_be_a_bad_afternoon(
        env, monkeypatch):
    """Blocking is probabilistic -- Amazon served twenty-one good renders
    across two days. Being hasty here costs a watch the owner has to
    recreate; being patient costs a fetch and no model at all."""
    a_blocked_watch(env, monkeypatch,
                    consecutive_blocks=Decimal(h.BLOCKED_DEGRADE_AFTER - 2))

    run(env)

    assert env.watches.items["w_1"]["status"] == "active"
    assert env.module.events.put_events.call_count == 0


def test_a_source_that_will_not_relent_stops_the_watch(env, monkeypatch):
    a_blocked_watch(env, monkeypatch,
                    consecutive_blocks=Decimal(h.BLOCKED_DEGRADE_AFTER - 1))

    result = run(env)

    assert result["status"] == "degraded"
    assert values_of(env.watches.last_update())[":s"] == "degraded"


def test_the_degraded_event_says_it_was_a_refusal(env, monkeypatch):
    """"Your watch broke" and "the shop shut us out" ask different things of
    the person reading them, and only one of them is true."""
    a_blocked_watch(env, monkeypatch,
                    consecutive_blocks=Decimal(h.BLOCKED_DEGRADE_AFTER - 1))
    run(env)

    assert emitted(env)["reason_kind"] == "blocked"


def test_a_degraded_refusal_does_not_leave_the_row_saying_failed(env, monkeypatch):
    """`failed` is what 8d escalates and what the UI paints as a broken
    extractor. The row has to keep telling the truth after the watch stops."""
    a_blocked_watch(env, monkeypatch,
                    consecutive_blocks=Decimal(h.BLOCKED_DEGRADE_AFTER - 1))
    run(env)

    written = [u for u in env.targets.updates if ":d" in u["values"]][-1]
    assert written["values"][":s"] == "blocked"


def test_a_good_read_clears_the_run_of_refusals(env, monkeypatch):
    """One successful render genuinely is the end of it, because the blocking
    is probabilistic rather than a decision about us."""
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor=PRICE_SPEC, consecutive_blocks=Decimal("6"))

    run(env)

    assert values_of(env.targets.updates[0])[":f"] == 0
    assert "consecutive_blocks = :f" in env.targets.updates[0]["expression"]


def test_a_refusal_still_records_what_the_tick_cost(env, monkeypatch):
    """It fetched. Spend that is invisible is the thing Phase 5 exists to
    stop, and a blocked fetch is still a fetch."""
    a_blocked_watch(env, monkeypatch)
    run(env)

    assert any(m["MetricName"] == "EstimatedCostUSD"
               for c in env.module.cloudwatch.put_metric_data.call_args_list
               for m in c.kwargs["MetricData"])


def test_a_normal_empty_page_is_still_an_ordinary_failure(env, monkeypatch):
    """The bar for calling something a refusal is high on purpose: a false
    positive costs a working watch."""
    a_blocked_watch(env, monkeypatch, page="<html><body>nothing here</body></html>")

    assert run(env)["status"] == "failed"


# --- a watch whose trigger is the clock ---------------------------------------
#
# Phase 9 step 3b. Everything else in this Lambda answers "is it true yet";
# here the answer arrived with the invocation. The dispatch is on which key the
# payload carries, which is what lets a reminder exist without a target row
# that describes nothing -- see docs/phase-9-watch-kinds.md §8.

def a_timed_watch(env, *, status="active", **extra):
    env.watches.items["w_1"] = {
        "watch_id": "w_1", "status": status,
        "prompt": "remind me to call the dentist", **extra,
    }


def fire(env, watch_id="w_1"):
    return h.lambda_handler({"watch_id": watch_id}, None)


def test_the_schedule_firing_is_the_whole_event(env):
    a_timed_watch(env)

    result = fire(env)

    assert result["notified"] is True
    assert result["trigger_kind"] == "time"


def test_a_timed_watch_reads_no_page_and_calls_no_model(env, monkeypatch):
    """There is nothing to read. A fetch here would be a bill for confirming
    what the clock already said."""
    monkeypatch.setattr(env.module, "fetch_raw", MagicMock(
        side_effect=AssertionError("a reminder must not fetch anything")))
    a_timed_watch(env)

    fire(env)

    assert env.module.lambda_client.invoke.call_count == 0


def test_a_timed_watch_needs_no_target_row(env):
    """The point of the whole plumbing change: a synthetic target would make
    the table describe something that does not exist."""
    a_timed_watch(env)
    assert env.targets.items == {}

    assert fire(env)["checked"] is True


def test_firing_marks_the_watch_triggered(env):
    a_timed_watch(env)
    fire(env)

    assert env.watches.items["w_1"]["status"] == "triggered"


def test_the_event_says_the_trigger_was_time(env):
    """The Notifier has to write a reminder, not "your watch came true".
    Nothing was found, and nothing was being looked for."""
    a_timed_watch(env)
    fire(env)

    detail = emitted(env)
    assert detail["trigger_kind"] == "time"
    assert detail["target_id"] is None
    assert detail["url"] is None
    assert detail["repeating"] is False


def test_a_reminder_note_travels_to_the_email(env):
    a_timed_watch(env, reminder_note="passport expires next month")
    fire(env)

    assert emitted(env)["note"] == "passport expires next month"


def test_a_watch_that_already_fired_does_not_fire_again(env):
    """`at(...)` deletes its own schedule, but EventBridge can still retry the
    invocation after a downstream failure -- and firing twice means telling a
    person twice."""
    a_timed_watch(env, status="triggered")

    result = fire(env)

    assert result["skipped"] is True
    assert env.module.events.put_events.call_count == 0


def test_a_paused_timed_watch_stays_quiet(env):
    a_timed_watch(env, status="paused")

    assert fire(env)["skipped"] is True
    assert env.module.events.put_events.call_count == 0


def test_a_retry_that_loses_the_race_says_nothing(env):
    a_timed_watch(env)
    original = env.watches.update_item

    def race(**kwargs):
        env.watches.items["w_1"]["status"] = "triggered"
        env.watches.update_item = original
        return original(**kwargs)

    env.watches.update_item = race

    assert fire(env)["skipped"] is True
    assert env.module.events.put_events.call_count == 0


def test_a_missing_watch_is_an_error_not_a_silent_no_op(env):
    with pytest.raises(RuntimeError, match="No such watch"):
        fire(env, "w_gone")


def test_a_condition_watch_is_completely_unaffected_by_the_new_dispatch(env,
                                                                       monkeypatch):
    """The `target_id` shape is the one every existing watch uses, and it must
    behave exactly as it did before the branch existed."""
    make_watch(env, condition={"metric": "price", "op": "<",
                               "value": Decimal("450")})
    make_target(env, extractor=PRICE_SPEC)

    result = run(env)

    assert result["last_value"] == 429.0
    assert result["condition_met"] is True
