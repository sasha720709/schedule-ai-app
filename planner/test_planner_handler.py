"""Tests for the Planner Lambda itself, which had none.

Everything under `planner/` was tested through `plan.py` and the kinds, each
with its collaborators injected. That is the exact shape the project has
already been bitten by twice: a suite where every test hands in its
dependencies cannot see the wiring, and both live bugs of 2026-08-02 were in
the wiring. The two things asserted here are in `handler.py` and nowhere else.

**Which targets survive.** A watch has one condition, and it used to be applied
to every target whatever money that target quotes -- so `price < 2000`
(shekels) was true of Amazon's $34.99 and would have fired on it.

**Which reading the baseline is.** "10% cheaper than now" was measured against
whichever shop's page loaded first, and the other shops were then judged
against a threshold derived from a shop they have nothing to do with.

`handler.py` is loaded by path under a unique name: three Lambdas here have a
module called `handler`, and a plain import hands whichever one pytest reached
first to all of them, silently, with the tests still green.

The *file* needs a unique basename for the same reason, one layer up. This was
`planner/test_handler.py` for about a minute, until pytest refused to collect
it alongside `api/test_handler.py` -- which is the friendlier version of the
same collision, because it fails loudly instead of running the wrong module.
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

if not isinstance(sys.modules.get("boto3"), types.ModuleType) or not hasattr(
    sys.modules.get("boto3", object()), "resource"
):
    _boto3 = types.ModuleType("boto3")
    _boto3.resource = MagicMock()
    _boto3.client = MagicMock()
    sys.modules["boto3"] = _boto3

if "anthropic" not in sys.modules:
    _anthropic = types.ModuleType("anthropic")
    _anthropic.Anthropic = MagicMock()
    sys.modules["anthropic"] = _anthropic

os.environ.setdefault("WATCHES_TABLE", "watches")
os.environ.setdefault("WATCH_TARGETS_TABLE", "targets")
os.environ.setdefault("FETCHER_FUNCTION_ARN", "arn:aws:lambda:::function:fetcher")

sys.path.insert(0, os.path.join(_ROOT, "shared"))
sys.path.insert(0, _HERE)

_spec = importlib.util.spec_from_file_location(
    "planner_handler", os.path.join(_HERE, "handler.py")
)
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)


class FakeTable:
    def __init__(self):
        self.rows = {}
        self.updates = []

    def put_item(self, Item):  # noqa: N803 -- boto3's casing
        self.rows[Item["target_id"]] = Item

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None):  # noqa: N803
        self.updates.append({
            "key": next(iter(Key.values())),
            "values": ExpressionAttributeValues or {},
            "expression": UpdateExpression,
            "names": ExpressionAttributeNames or {},
        })

    def last(self):
        return self.updates[-1] if self.updates else None


class FakeKind:
    """A kind whose targets and readings the test writes out in full."""

    name = "product"
    repeating = False
    window = None
    # Every real kind inherits this from Kind. A double that lacks it would
    # let the handler's dispatch pass here and fail in the Lambda.
    trigger = "condition"

    def __init__(self, targets, condition, readings, relative_pct=None):
        self.targets = targets
        self.condition = condition
        self.readings = readings
        self.relative_pct = relative_pct

    def plan(self, request, symbol=None, **kwargs):
        return {
            "condition": dict(self.condition),
            "relative_change_pct": self.relative_pct,
            "check_interval_min": 360,
            "targets": self.targets,
        }

    def resolve(self, target, condition, **kwargs):
        shop = target["shop"]
        return {
            "url": target["url"],
            "extract_hint": f"offers on {shop}",
            "fetch_method": "http",
            "window": None,
            "extractor": {"kind": "offers", "parse": "float"},
            "verified_value": self.readings[shop],
            "verified_raw": str(self.readings[shop]),
            "verified_items": [
                {"id": f"{shop}-1", "text": f"a thing at {shop}",
                 "price": self.readings[shop]},
            ],
            "unfiltered_count": 1,
            "currency": target["currency"],
            "shop": shop,
            "why": f"{shop}; 1 offer",
        }


@pytest.fixture
def run(monkeypatch):
    """Drive the Lambda against fakes and hand back what it wrote."""

    def go(*, targets, condition, readings, relative_pct=None):
        watches, target_rows = FakeTable(), FakeTable()
        monkeypatch.setattr(
            h.dynamodb, "Table",
            lambda name: {"watches": watches, "targets": target_rows}[name],
        )
        kind = FakeKind(targets, condition, readings, relative_pct)
        monkeypatch.setattr(h.classify_mod, "classify",
                            lambda request, names: {"kind": "product"})
        monkeypatch.setattr(h.kinds, "get", lambda name: kind)
        # Questions cost a model call and are asserted elsewhere.
        monkeypatch.setattr(h, "build_questions", lambda request, found: ([], 0.0))

        result = h.lambda_handler(
            {"watch_id": "w_test", "request": "watch the xbox"}, None)
        return result, watches, target_rows

    return go


def shop(name, currency):
    return {"shop": name, "currency": currency,
            "url": f"https://{name}.example/search",
            "fetch_method": "http",
            "extractor": {"kind": "offers", "parse": "float"},
            "extract_hint": f"offers on {name}"}


ILS_UNDER_2000 = {"metric": "price", "op": "<", "value": 2000, "currency": "ILS"}


# --- the currency gate -------------------------------------------------------

def test_a_shop_in_another_currency_is_refused_rather_than_compared(run):
    """`price < 2000` shekels was true of Amazon's $34.99, and would have fired."""
    result, watches, targets = run(
        targets=[shop("ivory", "ILS"), shop("amazon", "USD"),
                 shop("bug", "ILS")],
        condition=ILS_UNDER_2000,
        readings={"ivory": 1890.0, "amazon": 34.99, "bug": 2100.0},
    )
    kept = {row["shop"] for row in targets.rows.values()}
    assert kept == {"ivory", "bug"}
    assert len(result["target_ids"]) == 2


def test_the_refusal_says_why_in_words(run, capsys):
    run(
        targets=[shop("ivory", "ILS"), shop("amazon", "USD")],
        condition=ILS_UNDER_2000,
        readings={"ivory": 1890.0, "amazon": 34.99},
    )
    printed = capsys.readouterr().out
    assert "rejected target amazon" in printed
    assert "USD" in printed and "ILS" in printed


def test_the_condition_takes_its_currency_from_the_first_shop_when_it_has_none(run):
    """Nothing is dropped when the request never named a currency -- the first
    verified shop defines it, exactly as before, and the rest must match it."""
    _, watches, targets = run(
        targets=[shop("ivory", "ILS"), shop("amazon", "USD")],
        condition={"metric": "price", "op": "<", "value": 2000},
        readings={"ivory": 1890.0, "amazon": 34.99},
    )
    stored = watches.last()["values"][":c"]
    assert stored["currency"] == "ILS"
    assert {row["shop"] for row in targets.rows.values()} == {"ivory"}


def test_a_single_shop_is_never_refused_by_this(run):
    _, _, targets = run(
        targets=[shop("amazon", "USD")],
        condition={"metric": "price", "op": "<", "value": 500},
        readings={"amazon": 449.0},
    )
    assert len(targets.rows) == 1


# --- the baseline ------------------------------------------------------------

def test_the_baseline_is_the_cheapest_shop_not_the_first(run):
    """Bug is cheapest; Ivory answered first. 10% off has to mean 10% off the
    price you would actually pay."""
    _, watches, _ = run(
        targets=[shop("ivory", "ILS"), shop("bug", "ILS")],
        condition={"metric": "price", "op": "<", "value": None, "currency": "ILS"},
        readings={"ivory": 2000.0, "bug": 1000.0},
        relative_pct=-10,
    )
    stored = watches.last()["values"][":c"]
    assert float(stored["baseline"]) == 1000.0
    assert float(stored["value"]) == 900.0


def test_a_rising_watch_measures_from_the_dearest_shop(run):
    """Same rule, other end: measure at the end the condition is judged at, so
    the threshold sits further away and the watch fires later, never sooner."""
    _, watches, _ = run(
        targets=[shop("ivory", "ILS"), shop("bug", "ILS")],
        condition={"metric": "price", "op": ">", "value": None, "currency": "ILS"},
        readings={"ivory": 1000.0, "bug": 2000.0},
        relative_pct=10,
    )
    stored = watches.last()["values"][":c"]
    assert float(stored["baseline"]) == 2000.0
    assert float(stored["value"]) == 2200.0


def test_the_rejected_shop_cannot_become_the_baseline(run):
    """$34.99 is the smallest number on the table and means nothing here."""
    _, watches, _ = run(
        targets=[shop("ivory", "ILS"), shop("amazon", "USD")],
        condition={"metric": "price", "op": "<", "value": None, "currency": "ILS"},
        readings={"ivory": 1890.0, "amazon": 34.99},
        relative_pct=-10,
    )
    assert float(watches.last()["values"][":c"]["baseline"]) == 1890.0


def test_a_single_target_baseline_is_unchanged(run):
    _, watches, _ = run(
        targets=[shop("ivory", "ILS")],
        condition={"metric": "price", "op": "<", "value": None, "currency": "ILS"},
        readings={"ivory": 306.4},
        relative_pct=0,
    )
    stored = watches.last()["values"][":c"]
    assert float(stored["baseline"]) == 306.4
    assert float(stored["value"]) == 306.4


# --- the across flag ---------------------------------------------------------

def test_several_shops_make_the_watch_one_reading(run):
    _, watches, _ = run(
        targets=[shop("ivory", "ILS"), shop("bug", "ILS")],
        condition=ILS_UNDER_2000,
        readings={"ivory": 1890.0, "bug": 2100.0},
    )
    assert watches.last()["values"][":c"]["across"] == "best"


def test_one_shop_has_no_across(run):
    _, watches, _ = run(
        targets=[shop("ivory", "ILS")],
        condition=ILS_UNDER_2000,
        readings={"ivory": 1890.0},
    )
    assert "across" not in watches.last()["values"][":c"]


def test_an_equality_watch_has_no_best_however_many_shops(run):
    """"Cheapest" means nothing for `!=`, so the watch keeps the per-target shape."""
    _, watches, _ = run(
        targets=[shop("ivory", "ILS"), shop("bug", "ILS")],
        condition={"metric": "price", "op": "!=", "value": 2000, "currency": "ILS"},
        readings={"ivory": 1890.0, "bug": 2100.0},
    )
    assert "across" not in watches.last()["values"][":c"]


def test_only_shops_that_verified_count_towards_across(run):
    """Two targets planned, one refused -- that is one reading, not two."""
    _, watches, _ = run(
        targets=[shop("ivory", "ILS"), shop("amazon", "USD")],
        condition=ILS_UNDER_2000,
        readings={"ivory": 1890.0, "amazon": 34.99},
    )
    assert "across" not in watches.last()["values"][":c"]


# --- the row itself ----------------------------------------------------------

def test_the_shop_name_is_stored_on_the_target(run):
    _, _, targets = run(
        targets=[shop("ivory", "ILS")],
        condition=ILS_UNDER_2000,
        readings={"ivory": 1890.0},
    )
    row = next(iter(targets.rows.values()))
    assert row["shop"] == "ivory"
    assert row["currency"] == "ILS"


def test_every_shop_refused_is_a_failed_plan_not_an_empty_one(run):
    """Better a plan that says why than a watch with nothing to check."""
    _, watches, _ = run(
        targets=[shop("amazon", "USD")],
        condition={"metric": "price", "op": "<", "value": 2000, "currency": "ILS"},
        readings={"amazon": 34.99},
    )
    assert watches.last()["values"][":s"] == "failed"


# --- what happened to the shop that is not there -----------------------------

def test_a_refused_shop_is_recorded_on_the_watch_not_just_logged(run):
    """A watch that quietly became two shops instead of three is the
    silence-reads-as-broken failure again: the user asked about Amazon, Amazon
    is not there, and a CloudWatch log is not a place a person will look."""
    _, watches, _ = run(
        targets=[shop("ivory", "ILS"), shop("amazon", "USD")],
        condition=ILS_UNDER_2000,
        readings={"ivory": 1890.0, "amazon": 34.99},
    )
    rejected = watches.last()["values"][":rej"]
    assert [r["url"] for r in rejected] == ["amazon"]
    assert "USD" in rejected[0]["reason"]


def test_nothing_refused_is_an_empty_list_not_a_missing_field(run):
    _, watches, _ = run(
        targets=[shop("ivory", "ILS")],
        condition=ILS_UNDER_2000,
        readings={"ivory": 1890.0},
    )
    assert watches.last()["values"][":rej"] == []


def test_reserved_words_are_aliased_in_the_update(run, monkeypatch):
    """`repeat` is a DynamoDB reserved word. Writing it bare fails the whole
    update with a ValidationException -- and no test double knows the reserved
    list, so this was found by planning a real reminder and getting a watch
    stuck in `failed`. Aliasing every one of them removes the need to remember
    which words are on the list."""
    kind = FakeKind([shop("ivory", "ILS")], ILS_UNDER_2000, {"ivory": 1.0})
    kind.plan = lambda request, symbol=None, **kw: {
        "condition": {}, "relative_change_pct": None,
        "check_interval_min": 1440, "targets": [],
        "fire_at": "2026-08-06T21:00:00+03:00", "repeat": "daily",
    }
    kind.trigger = "time"

    watches = FakeTable()
    monkeypatch.setattr(h.dynamodb, "Table",
                        lambda name: {"watches": watches,
                                      "targets": FakeTable()}[name])
    monkeypatch.setattr(h.classify_mod, "classify",
                        lambda request, names: {"kind": "reminder"})
    monkeypatch.setattr(h.kinds, "get", lambda name: kind)
    monkeypatch.setattr(h, "build_questions", lambda request, found: ([], 0.0))
    h.lambda_handler({"watch_id": "w_t", "request": "remind me"}, None)

    written = watches.updates[-1]
    assert " repeat = " not in written["expression"]
    assert "#repeat = :repeat" in written["expression"]
