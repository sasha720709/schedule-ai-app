"""Tests for the classify step.

This is the newest and least-proven part of the design, and it sits on the path
of every single request. So almost everything here is about its failure
behaviour rather than its success behaviour: **a misclassification must cost a
suboptimal plan, never a rejected request.**

The gates are what is being tested, not the model's judgement. A model that
answers "quote" for a garden hose is a quality problem to fix in the prompt; a
model that answers "quote" with a hallucinated ticker and gets it spliced into
a URL is a bug, and that is what these assert against.
"""

import json
import os
import sys
import types
from types import SimpleNamespace
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

import classify as classify_mod  # noqa: E402
import kinds  # noqa: E402

KNOWN = kinds.names()


class Answers:
    """A client that returns one canned reply, or explodes."""

    def __init__(self, payload=None, *, raises=None, text=None):
        self.payload = payload
        self.raises = raises
        self.text = text
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        body = self.text if self.text is not None else json.dumps(self.payload)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)])


def decide(payload=None, **kw):
    client = Answers(payload, **kw)
    return classify_mod.classify("watch something", KNOWN, client=client), client


# --------------------------------------------------------------------------
# The happy paths
# --------------------------------------------------------------------------

def test_a_quote_is_recognised_and_keeps_its_symbol():
    decision, _ = decide({"kind": "quote", "symbol": "AAPL"})
    assert decision == {"kind": "quote", "symbol": "AAPL"}


@pytest.mark.parametrize("kind", ["value", "presence"])
def test_the_other_kinds_pass_through(kind):
    decision, _ = decide({"kind": kind, "symbol": None})
    assert decision["kind"] == kind


def test_a_symbol_is_discarded_for_anything_that_is_not_a_quote():
    """Nothing else uses it, and carrying it forward invites a later reader to
    assume it means something."""
    decision, _ = decide({"kind": "value", "symbol": "AAPL"})
    assert decision["symbol"] is None


def test_classification_costs_one_small_cheap_call():
    """It runs on every request, including the ones it decides nothing about.
    If this ever reaches for Sonnet or web search, the saving is gone."""
    _, client = decide({"kind": "value", "symbol": None})
    call = client.calls[0]

    assert call["model"] == "claude-haiku-4-5-20251001"
    assert "tools" not in call
    assert call["max_tokens"] <= 512


# --------------------------------------------------------------------------
# The gates -- everything unrecognised becomes the path we already had
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["calendar", "stock", "", None, "VALUE ", 7])
def test_a_kind_that_is_not_registered_falls_back_to_value(kind):
    """Includes kinds that are planned but not built yet, and every shape a
    model can return that is not a string naming a real kind. A wrong guess
    must cost a suboptimal plan, never a rejected request."""
    decision, _ = decide({"kind": kind, "symbol": None})
    assert decision["kind"] == "value"


def test_reminder_is_a_registered_kind_now(): 
    """It was in the design doc and not in the registry for three phases, and
    this test asserted the fallback. Built 2026-08-05."""
    decision, _ = decide({"kind": "reminder", "symbol": None})
    assert decision["kind"] == "reminder"


@pytest.mark.parametrize("symbol", ["", None, "Apple Inc", "AAPL; rm -rf",
                                    "https://evil.example", "../../etc/passwd"])
def test_a_quote_whose_symbol_the_registry_would_refuse_degrades_to_a_search(symbol):
    """The symbol is about to be spliced into a URL. Validating it here means a
    hallucinated ticker becomes a web search instead of a request."""
    decision, _ = decide({"kind": "quote", "symbol": symbol})
    assert decision["kind"] == "value"
    assert decision["symbol"] is None


def test_a_model_outage_does_not_take_planning_down():
    """Classification is an optimisation. Losing it should cost the saving, not
    the request."""
    decision, _ = decide(raises=RuntimeError("overloaded"))
    assert decision == {"kind": "value", "symbol": None}


def test_a_reply_that_is_not_json_falls_back_rather_than_raising():
    decision, _ = decide(text="I think this is probably a stock?")
    assert decision["kind"] == "value"


def test_a_reply_with_no_kind_at_all_falls_back():
    decision, _ = decide({"symbol": "AAPL"})
    assert decision["kind"] == "value"


def test_the_fallback_is_a_kind_the_registry_actually_has():
    """Guards against the fallback drifting out of the registry -- a typo here
    would send every unclassifiable request to a kind that does not exist."""
    decision, _ = decide({"kind": "nonsense"})
    assert decision["kind"] in KNOWN


# --------------------------------------------------------------------------
# The prompt still has to say the things that were moved out of SEARCH_PROMPT
# --------------------------------------------------------------------------

def test_the_prompt_keeps_the_rule_that_a_product_is_not_a_quote():
    """Moved verbatim in spirit from SEARCH_PROMPT. "Apple" the company and an
    Apple product at a named shop are the case this exists to separate."""
    prompt = classify_mod.CLASSIFY_PROMPT
    assert "not a quote" in prompt.lower() or "NOT a quote" in prompt
    assert "where" in prompt


def test_the_prompt_tells_the_model_to_prefer_the_general_path_when_unsure():
    assert "unsure" in classify_mod.CLASSIFY_PROMPT
    assert "value" in classify_mod.CLASSIFY_PROMPT
