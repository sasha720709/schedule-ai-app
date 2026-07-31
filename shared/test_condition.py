"""Tests for deterministic condition evaluation.

The asymmetry worth stating: a false "met" sends one wrong email, which the
owner notices in seconds. A false "not met" is invisible -- the watch simply
never fires, and there is nothing to look at. So most of what is asserted here
is that ambiguity raises rather than quietly answering False.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from condition import (  # noqa: E402
    ConditionError,
    describe,
    evaluate,
    normalise_op,
)


# --- the ops the Planner is told to emit -------------------------------------

@pytest.mark.parametrize("op,value,target,expected", [
    ("<", 429.0, 450, True),
    ("<", 629.0, 450, False),
    ("<=", 450.0, 450, True),
    (">", 5, 1, True),
    (">=", 1, 1, True),
    ("==", 0, 0, True),
    ("!=", 3, 0, True),
])
def test_absolute_comparisons(op, value, target, expected):
    assert evaluate({"metric": "price", "op": op, "value": target}, value) is expected


# --- synonyms, because a model writes prose even when asked not to -----------

@pytest.mark.parametrize("spelling", [
    "<", "lt", "below", "under", "less_than", "LESS THAN", "Less-Than",
])
def test_the_same_idea_spelled_several_ways(spelling):
    assert normalise_op(spelling) == "<"


def test_an_unknown_op_raises_rather_than_answering_not_met():
    """The single most important test in this file.

    Answering False here would produce a watch that is alive, billed, checked
    on schedule, and structurally incapable of ever firing.
    """
    with pytest.raises(ConditionError):
        evaluate({"metric": "price", "op": "roughly_around", "value": 450}, 429.0)


def test_a_non_string_op_raises():
    with pytest.raises(ConditionError):
        evaluate({"op": 7, "value": 1}, 1)


# --- the vacancy shape: count and booleans -----------------------------------

def test_a_count_reaching_one_meets_the_condition():
    cond = {"metric": "matching_vacancies", "op": ">=", "value": 1}
    assert evaluate(cond, 0) is False
    assert evaluate(cond, 1) is True
    assert evaluate(cond, 4) is True


def test_a_boolean_reading_compares_against_a_boolean_target():
    cond = {"metric": "in_stock", "op": "==", "value": True}
    assert evaluate(cond, True) is True
    assert evaluate(cond, False) is False


def test_a_boolean_still_lines_up_with_a_numeric_target():
    """`count` with parse:bool against a plan that wrote 1 rather than true."""
    assert evaluate({"op": "==", "value": 1}, True) is True
    assert evaluate({"op": "==", "value": 0}, False) is True


# --- text metrics ------------------------------------------------------------

def test_text_compares_case_and_space_insensitively():
    cond = {"metric": "status", "op": "==", "value": "Shipped"}
    assert evaluate(cond, "  shipped ") is True
    assert evaluate(cond, "pending") is False


def test_a_number_against_text_raises_instead_of_coercing():
    """A `text` extractor reading "629.00" against a numeric threshold means the
    spec should have used `currency`. Coercing here would hide a real bug."""
    with pytest.raises(ConditionError) as caught:
        evaluate({"op": "<", "value": 450}, "629.00")
    assert "parse" in str(caught.value)


# --- malformed input ---------------------------------------------------------

def test_a_missing_value_is_not_silently_false():
    with pytest.raises(ConditionError):
        evaluate({"metric": "price", "op": "<"}, 429.0)


def test_no_reading_is_not_something_to_judge():
    """An `unavailable` extraction must be handled by the caller as "not yet",
    never routed here as if it were a value."""
    with pytest.raises(ConditionError):
        evaluate({"op": "<", "value": 450}, None)


def test_a_non_object_condition_raises():
    with pytest.raises(ConditionError):
        evaluate("price under 450", 429.0)


# --- description -------------------------------------------------------------

def test_describe_is_readable():
    assert describe(
        {"metric": "price", "op": "below", "value": 450, "currency": "USD"}
    ) == "price < 450 USD"


def test_describe_survives_an_op_it_cannot_normalise():
    assert "roughly" in describe({"metric": "price", "op": "roughly", "value": 1})
