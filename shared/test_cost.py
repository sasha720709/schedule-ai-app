"""Tests for the cost model.

The important property is not any single figure -- rates change -- but the
*relationships*, which are what the guardrails depend on:

- the model call dominates a check today
- once it is gone, the browser dominates
- a budget expressed in dollars automatically relaxes the interval floor when
  a check gets cheaper, with no constant to remember to change

That last one is the whole reason 8a uses a budget rather than a hardcoded
minimum interval, so it is asserted directly.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cost  # noqa: E402


# --- the relationships that matter -------------------------------------------

def test_the_model_call_dominates_a_check_today():
    total = cost.cost_per_check("browser", uses_model=True)
    assert cost.judge_cost() / total > 0.95


def test_removing_the_model_makes_a_check_at_least_thirty_times_cheaper():
    with_model = cost.cost_per_check("browser", uses_model=True)
    without = cost.cost_per_check("browser", uses_model=False)
    assert with_model / without > 30


def test_without_the_model_the_browser_becomes_the_dominant_cost():
    """Which is what redefines the Planner's job as finding a cheap source."""
    browser = cost.cost_per_check("browser", uses_model=False)
    http = cost.cost_per_check("http", uses_model=False)
    assert browser / http > 20


def test_a_json_endpoint_beats_today_by_three_orders_of_magnitude():
    today = cost.cost_per_check("browser", uses_model=True)
    target = cost.cost_per_check("http", uses_model=False)
    assert today / target > 1000


# --- the budget-derived floor ------------------------------------------------

def test_the_budget_forces_long_intervals_while_the_model_is_in_the_loop():
    floor = cost.min_interval_for_budget(5.0, fetch_method="browser", uses_model=True)
    assert 30 <= floor <= 90


def test_the_same_budget_permits_minute_checks_once_the_model_is_gone():
    """The point of a budget rather than a hardcoded interval floor: Phase 8b
    relaxes this automatically."""
    floor = cost.min_interval_for_budget(5.0, fetch_method="http", uses_model=False)
    assert floor == cost.MIN_INTERVAL_MIN


def test_the_floor_is_always_affordable():
    """Rounding down would return an interval the budget cannot actually pay
    for, so the floor must round up."""
    for method in ("http", "browser"):
        for uses_model in (True, False):
            floor = cost.min_interval_for_budget(5.0, 1, method, uses_model)
            assert cost.monthly_cost(floor, 1, method, uses_model) <= 5.0


def test_more_targets_means_a_longer_floor():
    one = cost.min_interval_for_budget(5.0, targets=1, fetch_method="browser")
    three = cost.min_interval_for_budget(5.0, targets=3, fetch_method="browser")
    assert three > one


def test_the_floor_is_clamped_to_the_allowed_range():
    assert cost.min_interval_for_budget(0.000001, fetch_method="browser") == cost.MAX_INTERVAL_MIN
    assert cost.min_interval_for_budget(1_000_000.0) == cost.MIN_INTERVAL_MIN


# --- monthly cost ------------------------------------------------------------

def test_halving_the_interval_doubles_the_cost():
    assert cost.monthly_cost(30) == pytest.approx(cost.monthly_cost(60) * 2)


def test_cost_scales_with_target_count():
    assert cost.monthly_cost(60, targets=3) == pytest.approx(
        cost.monthly_cost(60, targets=1) * 3
    )


def test_a_three_minute_browser_watch_is_shockingly_expensive_today():
    """The number that made Phase 8 the priority."""
    assert cost.monthly_cost(3, 1, "browser", True) > 50


def test_a_zero_interval_is_rejected_rather_than_dividing_by_zero():
    with pytest.raises(ValueError):
        cost.monthly_cost(0)


# --- budget from the environment ---------------------------------------------

def test_the_budget_comes_from_the_environment_when_set(monkeypatch):
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "25")
    assert cost.monthly_budget_usd() == 25.0


@pytest.mark.parametrize("bad", ["", "abc", "0", "-5"])
def test_a_nonsense_budget_falls_back_to_the_default(monkeypatch, bad):
    """A malformed env var must not become an unlimited budget."""
    monkeypatch.setenv("MONTHLY_BUDGET_USD", bad)
    assert cost.monthly_budget_usd() == cost.DEFAULT_MONTHLY_BUDGET_USD


# --- the estimate payload ----------------------------------------------------

def test_estimate_flags_an_interval_over_budget():
    est = cost.estimate(1, 1, "browser", True)
    assert est["within_budget"] is False
    assert est["estimated_monthly_usd"] > est["monthly_budget_usd"]
    assert est["min_interval_min"] > 1


def test_estimate_accepts_its_own_suggested_floor():
    """Whatever min_interval_min says must itself pass the budget check, or the
    error message would send a caller to a value that is also refused."""
    over = cost.estimate(1, 1, "browser", True)
    ok = cost.estimate(over["min_interval_min"], 1, "browser", True)
    assert ok["within_budget"] is True


def test_estimate_is_json_safe():
    import json
    json.dumps(cost.estimate(60, 2, "http", True))
