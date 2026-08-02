"""Tests for the kind registry and for `quote`, the kind that compiles nothing.

`value` and `presence` are exercised through `test_planner.py`, which drives
them via the compile-and-verify pipeline they share. This file covers the two
things that are new in Phase 9 step 2: the registry itself, and a kind whose
whole point is that no model is involved.

The property worth protecting for `quote`: **it must still be verified.** The
temptation with a canned source is to trust it, because we wrote it. But a
registry entry can go stale -- CNBC reshaping its payload -- and the difference
between finding that out at plan time, in front of the user, and finding it out
at 3am three weeks later is the difference between a good watch and a lie.
"""

import importlib.util
import json
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

if "anthropic" not in sys.modules:
    _anthropic = types.ModuleType("anthropic")
    _anthropic.Anthropic = MagicMock()
    sys.modules["anthropic"] = _anthropic

sys.path.insert(0, os.path.join(_ROOT, "shared"))
sys.path.insert(0, _HERE)

import kinds  # noqa: E402
from kinds import quote as quote_mod  # noqa: E402

QUOTE = kinds.get("quote")


def cnbc(last="333.43"):
    """The shape shared/sources.py's canned jsonpath expects."""
    return json.dumps({
        "FormattedQuoteResult": {
            "FormattedQuote": [{"symbol": "AAPL", "last": last,
                                "name": "Apple Inc"}]
        }
    })


@pytest.fixture
def wire(monkeypatch):
    """Stand in for the network. Records what was asked for."""

    def build(body=None, fails=False):
        calls = []

        def fetch_raw(url):
            calls.append(url)
            if fails:
                raise RuntimeError("connection reset")
            return cnbc() if body is None else body

        monkeypatch.setattr(quote_mod, "fetch_raw", fetch_raw)
        return calls

    return build


def never(*_args, **_kwargs):
    raise AssertionError("this kind must not fetch pages that way")


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

def test_every_registered_kind_can_resolve():
    for name in kinds.names():
        assert hasattr(kinds.get(name), "resolve")


def test_an_unknown_kind_degrades_to_value_rather_than_raising():
    """The classifier is the newest and least-proven part of this design. A
    wrong guess must cost a suboptimal plan, never a rejected request."""
    assert kinds.get("nonsense").name == "value"
    assert kinds.get(None).name == "value"
    assert kinds.get("").name == "value"


def test_quote_is_not_a_compiled_kind():
    """The structural claim of step 2. If `quote` ever inherits the four
    compile methods, it has been bent to fit the wrong base class and the
    null-object problem is back."""
    assert isinstance(kinds.get("value"), kinds.CompiledKind)
    assert isinstance(kinds.get("presence"), kinds.CompiledKind)
    assert not isinstance(QUOTE, kinds.CompiledKind)
    assert not hasattr(QUOTE, "compile_prompt")


# --------------------------------------------------------------------------
# Resolving a quote
# --------------------------------------------------------------------------

def test_a_symbol_becomes_a_verified_target(wire):
    calls = wire()
    resolved = QUOTE.resolve({"known_source": "stock_quote", "symbol": "AAPL"},
                             {"metric": "price", "op": "<", "value": 300},
                             fetch_http=never, fetch_browser=never)

    assert resolved["verified_value"] == 333.43
    assert resolved["verified_raw"] == "333.43"
    assert resolved["fetch_method"] == "http"
    assert "AAPL" in calls[0]


def test_no_model_is_consulted(wire):
    """The whole point of the kind. `client` is accepted and must go unused --
    if a model call appears here, planning a stock watch has silently gone back
    to costing what it used to."""
    wire()
    client = MagicMock()
    QUOTE.resolve({"known_source": "stock_quote", "symbol": "AAPL"}, {},
                  fetch_http=never, fetch_browser=never, client=client)

    client.messages.create.assert_not_called()


def test_the_browser_is_never_reached(wire):
    """A quote is a JSON endpoint. Rendering one in Chromium would cost 45x
    for nothing, so the fetchers are accepted and deliberately ignored."""
    wire()
    QUOTE.resolve({"known_source": "stock_quote", "symbol": "AAPL"}, {},
                  fetch_http=never, fetch_browser=never)


def test_the_hint_that_survives_is_a_repair_instruction(wire):
    """The stored hint is no longer a reading instruction -- nothing reads it
    on a tick. It exists so a human can see where the number comes from."""
    wire()
    resolved = QUOTE.resolve({"known_source": "stock_quote", "symbol": "AAPL"},
                             {}, fetch_http=never, fetch_browser=never)

    assert "AAPL" in resolved["extract_hint"]
    assert "FormattedQuote" in resolved["extract_hint"]


# --------------------------------------------------------------------------
# It is canned, not trusted
# --------------------------------------------------------------------------

def test_a_payload_the_canned_spec_cannot_read_is_refused(wire):
    """CNBC reshaping its response must fail here, loudly, with nothing
    stored -- not silently at 3am three weeks later."""
    wire(body=json.dumps({"quotes": [{"price": "333.43"}]}))

    with pytest.raises(ValueError, match="canned extractor"):
        QUOTE.resolve({"known_source": "stock_quote", "symbol": "AAPL"}, {},
                      fetch_http=never, fetch_browser=never)


def test_an_endpoint_that_will_not_answer_is_not_swallowed(wire):
    wire(fails=True)

    with pytest.raises(RuntimeError, match="connection reset"):
        QUOTE.resolve({"known_source": "stock_quote", "symbol": "AAPL"}, {},
                      fetch_http=never, fetch_browser=never)


@pytest.mark.parametrize("symbol", ["", "not a ticker", "AAPL; DROP", "../etc"])
def test_a_symbol_that_is_not_a_symbol_is_rejected_before_any_request(wire, symbol):
    """The symbol arrives from a model and is spliced into a URL, so the
    character set is a hard gate rather than a formality."""
    calls = wire()

    with pytest.raises(ValueError, match="plausible market symbol"):
        QUOTE.resolve({"known_source": "stock_quote", "symbol": symbol}, {},
                      fetch_http=never, fetch_browser=never)
    assert calls == []


def test_an_unknown_registry_kind_is_rejected(wire):
    calls = wire()

    with pytest.raises(ValueError, match="unknown known_source"):
        QUOTE.resolve({"known_source": "weather", "symbol": "AAPL"}, {},
                      fetch_http=never, fetch_browser=never)
    assert calls == []


# --------------------------------------------------------------------------
# Self-healing
# --------------------------------------------------------------------------

def test_a_quote_does_not_self_heal():
    """8d exists because a site we do not control can be redesigned under a
    compiled extractor. Here the extractor is ours: if it breaks, the fix is
    one line in sources.py for every watch at once, and paying Haiku to
    rediscover it per watch would be slower and wrong."""
    assert QUOTE.self_heals is False
    assert kinds.get("value").self_heals is True
    assert kinds.get("presence").self_heals is True


# --------------------------------------------------------------------------
# Planning a quote: condition and cadence only, and no web search
# --------------------------------------------------------------------------

class Scripted:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text",
                                           text=json.dumps(self.payload))])


def quote_plan(payload, request="tell me when Apple goes down"):
    client = Scripted(payload)
    return QUOTE.plan(request, "AAPL", client=client), client


def test_planning_a_quote_never_reaches_the_web_search():
    """The saving that made classification worth doing on its own. A quote used
    to pay for Sonnet *with* web search before anyone noticed the answer was a
    registry lookup."""
    _, client = quote_plan({"condition": {"metric": "price", "op": "<"},
                            "relative_change_pct": 0,
                            "check_interval_min": 5})

    assert len(client.calls) == 1
    assert "tools" not in client.calls[0]
    assert client.calls[0]["model"] == "claude-haiku-4-5-20251001"


def test_the_target_is_the_symbol_and_nothing_else_is_chosen():
    result, _ = quote_plan({"condition": {}, "relative_change_pct": None,
                            "check_interval_min": 5})
    assert result["targets"] == [{"known_source": "stock_quote",
                                  "symbol": "AAPL"}]


def test_a_relative_request_leaves_the_threshold_unset():
    """Same rule as the searching path, and for the same reason: the model has
    no current price, so any absolute threshold it writes is invented."""
    result, _ = quote_plan({"condition": {"metric": "price", "op": "<",
                                          "value": None},
                            "relative_change_pct": 0,
                            "check_interval_min": 1})
    assert result["relative_change_pct"] == 0
    assert result["condition"]["value"] is None


@pytest.mark.parametrize("proposed,expected", [(1, 1), (30, 30), (59, 59),
                                               (60, 59), (240, 59), (1440, 59)])
def test_the_interval_is_clamped_below_an_hour(proposed, expected):
    """A windowed schedule is a cron step inside the hour, so `*/60` would
    silently fire hourly on the hour. Clamped rather than refused -- the model
    picked a cadence, not a contract."""
    result, _ = quote_plan({"condition": {}, "relative_change_pct": None,
                            "check_interval_min": proposed})
    assert result["check_interval_min"] == expected


def test_a_missing_interval_gets_a_usable_default():
    result, _ = quote_plan({"condition": {}, "relative_change_pct": None})
    assert 1 <= result["check_interval_min"] <= 59


def test_the_planned_interval_is_expressible_as_a_windowed_schedule():
    """Ties the clamp to the thing it protects: whatever the model proposes,
    shared/schedules.py must be able to build a cron for it."""
    import schedules
    result, _ = quote_plan({"condition": {}, "check_interval_min": 720})
    schedules.expression(result["check_interval_min"], QUOTE.window)
