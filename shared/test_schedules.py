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


@pytest.mark.parametrize("minutes,expected", [
    (60, "cron(0 9-16 ? * MON-FRI *)"),
    (120, "cron(0 9-16/2 ? * MON-FRI *)"),
    (240, "cron(0 9-16/4 ? * MON-FRI *)"),
    (1440, "cron(0 9-16/24 ? * MON-FRI *)"),
])
def test_an_hour_or_more_steps_the_hours_field_not_the_minutes(minutes, expected):
    """`cron(*/60 9-16 ...)` does not mean "every 60 minutes" -- a minute step
    runs within the hour, so `*/60` and `*/240` both collapse to `0` and fire
    hourly, with no error ever raised. This used to be refused outright, which
    turned an ordinary hourly quote watch into a 500 at confirm time. Stepping
    the hours field expresses it exactly.
    """
    got = schedules.expression(minutes, "us_market_hours")
    assert got["ScheduleExpression"] == expected


@pytest.mark.parametrize("asked,snapped", [
    (7, 10),      # not a divisor of 60: */7 leaves a ragged 4-minute gap
    (45, 60),
    (51, 60),     # the budget-derived floor from 8a, arbitrary by construction
    (90, 120),
    (1441, 1500),
])
def test_an_inexpressible_interval_rounds_up_rather_than_failing(asked, snapped):
    """Up, never down: a longer interval is fewer checks, so a snapped
    schedule can only cost less than the estimate that was approved."""
    assert schedules.snap(asked, "us_market_hours") == snapped
    assert (schedules.expression(asked, "us_market_hours")
            == schedules.expression(snapped, "us_market_hours"))


def test_a_continuous_schedule_snaps_nothing():
    """`rate(...)` expresses any interval, so there is nothing to round."""
    for minutes in (7, 51, 90, 1441):
        assert schedules.snap(minutes) == minutes


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
@pytest.mark.parametrize("minutes", [1, 5, 15, 30, 60, 120, 240, 1440])
def test_the_count_matches_the_expression_it_describes(minutes, window):
    """Derive the count from the cron/rate string itself rather than from the
    same constants `checks_per_month` uses, so the two cannot drift together.
    """
    expr = schedules.expression(minutes, window)["ScheduleExpression"]

    if expr.startswith("rate("):
        expected = 60 * 24 * 30 / minutes
    else:
        # cron(<minutes> <hours> ? * MON-FRI *), where either field may step.
        minute_field, hour_field = expr[len("cron("):-1].split()[:2]
        if minute_field == "*":
            per_hour = 60
        elif minute_field == "0":
            per_hour = 1
        else:
            per_hour = len(range(0, 60, int(minute_field.lstrip("*/"))))
        span, _, hour_step = hour_field.partition("/")
        low, high = (int(x) for x in span.split("-"))
        hours = len(range(low, high + 1, int(hour_step or 1)))
        expected = per_hour * hours * schedules.TRADING_DAYS_PER_MONTH

    assert schedules.checks_per_month(minutes, window) == expected


# --------------------------------------------------------------------------
# When does it actually run next?
#
# The bug this closes was not in the schedule. It was that nothing ever said
# what the schedule meant, so a correct sixteen-hour wait was indistinguishable
# from a dead watch.
# --------------------------------------------------------------------------

from datetime import datetime, timezone as _tz


def _utc(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=_tz.utc)


def test_a_continuous_schedule_runs_one_interval_from_now():
    """`rate(...)` counts from creation, not from a clock boundary."""
    got = schedules.next_fire_after(_utc(2026, 8, 3, 20, 33), 30)
    assert got == _utc(2026, 8, 3, 21, 3)


def test_the_night_this_was_written_for():
    """The exact failure. 20:33 UTC on Monday 2026-08-03 is 16:33 in New York,
    three minutes past the last slot of a 9-16 window. The next check is 09:00
    Tuesday New York -- 13:00 UTC, sixteen and a half hours later.

    Nothing was wrong with the schedule. The watch was deleted the next
    morning because the product never said this number out loud.
    """
    got = schedules.next_fire_after(_utc(2026, 8, 3, 20, 33), 30, "us_market_hours")
    assert got == _utc(2026, 8, 4, 13, 0)


def test_inside_the_window_the_next_slot_is_minutes_away():
    """Same watch, confirmed during the session instead: 14:05 UTC is 10:05
    New York, so the next 30-minute slot is 10:30."""
    got = schedules.next_fire_after(_utc(2026, 8, 4, 14, 5), 30, "us_market_hours")
    assert got == _utc(2026, 8, 4, 14, 30)


def test_a_friday_evening_confirm_waits_for_monday():
    """Friday 2026-08-07 21:00 UTC is 17:00 New York, after the close. The
    window is MON-FRI, so the next check is Monday the 10th at 09:00 local."""
    got = schedules.next_fire_after(_utc(2026, 8, 7, 21, 0), 5, "us_market_hours")
    assert got == _utc(2026, 8, 10, 13, 0)


def test_before_the_open_it_waits_only_until_the_open():
    """08:00 UTC is 04:00 New York -- the morning the watch was deleted. It
    was four and a half hours from its first check, not broken."""
    got = schedules.next_fire_after(_utc(2026, 8, 4, 8, 24), 30, "us_market_hours")
    assert got == _utc(2026, 8, 4, 13, 0)


def test_an_hourly_windowed_watch_fires_on_the_hour():
    got = schedules.next_fire_after(_utc(2026, 8, 4, 14, 5), 60, "us_market_hours")
    assert got == _utc(2026, 8, 4, 15, 0)


def test_the_result_is_always_strictly_after_the_moment_asked_about():
    """Landing exactly on a slot must return the *next* one, or a confirm made
    on the minute would report a time already in the past."""
    on_the_slot = _utc(2026, 8, 4, 14, 30)
    got = schedules.next_fire_after(on_the_slot, 30, "us_market_hours")
    assert got > on_the_slot


def test_an_unknown_window_answers_continuously_like_everything_else():
    assert (schedules.next_fire_after(_utc(2026, 8, 3, 20, 33), 30, "not_a_window")
            == schedules.next_fire_after(_utc(2026, 8, 3, 20, 33), 30))


def test_a_snapped_interval_reports_the_time_it_will_really_fire():
    """51 minutes snaps to 60, so the answer must be the top of the hour --
    not 51 minutes after the question, which would be a time nothing runs."""
    got = schedules.next_fire_after(_utc(2026, 8, 4, 14, 5), 51, "us_market_hours")
    assert got == _utc(2026, 8, 4, 15, 0)
