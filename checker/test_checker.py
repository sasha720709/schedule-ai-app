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
