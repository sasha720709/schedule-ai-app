"""Tests for schedule shapes and how often they actually run.

Two things are worth protecting here.

The default must not move. Every existing watch is `rate(N minutes)`, and a
refactor that changed its cadence would be invisible in review and obvious
only in the bill.

And `checks_per_month` has to agree with `expression`. They are the same fact
told twice -- once to EventBridge and once to the cost estimate -- and if they
drift, the plan card confidently quotes a number for a schedule that does not
exist.
"""

import pytest

import schedules


# --------------------------------------------------------------------------
# The continuous default, unchanged since Phase 2
# --------------------------------------------------------------------------

@pytest.mark.parametrize("minutes,expected", [
    (1, "rate(1 minute)"),
    (10, "rate(10 minutes)"),
    (60, "rate(60 minutes)"),
    (1440, "rate(1440 minutes)"),
])
def test_the_plain_rate_expression_is_untouched(minutes, expected):
    """Including the singular at 1, which EventBridge Scheduler requires."""
    assert schedules.expression(minutes) == {"ScheduleExpression": expected}


def test_a_continuous_schedule_carries_no_timezone():
    """`rate(...)` is interval arithmetic from the moment of creation -- a
    timezone would be meaningless, and Scheduler rejects the pairing."""
    assert "ScheduleExpressionTimezone" not in schedules.expression(5)


def test_continuous_checks_match_the_figure_cost_py_was_built_on():
    assert schedules.checks_per_month(1) == 43200
    assert schedules.checks_per_month(5) == 8640


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

@pytest.mark.parametrize("minutes,expected", [
    (1, "cron(* 9-16 ? * MON-FRI *)"),
    (5, "cron(*/5 9-16 ? * MON-FRI *)"),
    (30, "cron(*/30 9-16 ? * MON-FRI *)"),
])
def test_a_windowed_schedule_is_a_cron_within_trading_hours(minutes, expected):
    got = schedules.expression(minutes, "us_market_hours")
    assert got["ScheduleExpression"] == expected


def test_the_window_carries_its_timezone():
    """Not decoration. A UTC cron drifts an hour twice a year at DST and would
    spend a week each time reading a market that is shut."""
    got = schedules.expression(5, "us_market_hours")
    assert got["ScheduleExpressionTimezone"] == "America/New_York"


def test_the_window_brackets_the_session_rather_than_matching_it():
    """09:00-16:59 rather than the exact 09:30-16:00.

    Scheduler takes one expression per schedule and the exact session needs
    two, which would mean two schedules per target and two of everything that
    creates, pauses and deletes them. The margin costs ~90 minutes a day of
    reading a value that has not moved, which is harmless: a frozen quote is
    the last real price, not a wrong one. Missing the opening bell or the
    closing print would not be harmless, so the margin goes outwards.
    """
    hours = schedules.US_MARKET.hours
    assert hours == "9-16"
    assert schedules.US_MARKET.hours_per_day == 8


def test_an_interval_of_an_hour_or_more_is_refused_rather_than_silently_wrong():
    """`cron(*/60 9-16 ...)` does not mean "every 60 minutes" -- steps run
    within the hour, so it would fire hourly on the hour and no error would
    ever be raised. Refusing is the only way this stays honest."""
    for minutes in (60, 90, 1440):
        with pytest.raises(ValueError, match="windowed schedule"):
            schedules.expression(minutes, "us_market_hours")


def test_a_window_cuts_the_number_of_checks_by_about_four():
    assert schedules.checks_per_month(1, "us_market_hours") == 10080
    ratio = schedules.checks_per_month(1) / schedules.checks_per_month(1, "us_market_hours")
    assert 4.2 < ratio < 4.4


def test_an_unknown_window_runs_continuously_rather_than_failing():
    """Same principle as an unknown kind: degrade to today's behaviour. A
    watch that checks too often is a cost problem; one that never checks is a
    broken product."""
    assert schedules.expression(5, "not_a_window") == schedules.expression(5)
    assert schedules.checks_per_month(5, "not_a_window") == 8640


@pytest.mark.parametrize("window", [None, "us_market_hours"])
def test_a_nonpositive_interval_is_refused(window):
    for minutes in (0, -1):
        with pytest.raises(ValueError):
            schedules.expression(minutes, window)
        with pytest.raises(ValueError):
            schedules.checks_per_month(minutes, window)


# --------------------------------------------------------------------------
# The two must agree
# --------------------------------------------------------------------------

@pytest.mark.parametrize("window", [None, "us_market_hours"])
@pytest.mark.parametrize("minutes", [1, 5, 15, 30])
def test_the_count_matches_the_expression_it_describes(minutes, window):
    """Derive the count from the cron/rate string itself rather than from the
    same constants `checks_per_month` uses, so the two cannot drift together.
    """
    expr = schedules.expression(minutes, window)["ScheduleExpression"]

    if expr.startswith("rate("):
        expected = 60 * 24 * 30 / minutes
    else:
        # cron(<step> <hours> ? * MON-FRI *)
        step, hours = expr[len("cron("):-1].split()[:2]
        per_hour = 60 if step == "*" else 60 / int(step.lstrip("*/"))
        low, high = (int(x) for x in hours.split("-"))
        expected = per_hour * (high - low + 1) * schedules.TRADING_DAYS_PER_MONTH

    assert schedules.checks_per_month(minutes, window) == expected
