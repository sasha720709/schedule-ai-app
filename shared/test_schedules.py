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

from datetime import datetime
from zoneinfo import ZoneInfo

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


# --------------------------------------------------------------------------
# Tel Aviv trades Sunday to Thursday
#
# This is why the window stopped being one hardcoded constant. A MON-FRI
# schedule on a TASE symbol is not slightly off: it misses a whole session
# every week and polls a shut exchange on Friday.
# --------------------------------------------------------------------------

def test_tel_aviv_runs_sunday_to_thursday():
    expr = schedules.expression(5, "tase_hours")
    assert expr["ScheduleExpression"] == "cron(*/5 9-18 ? * SUN,MON,TUE,WED,THU *)"
    assert expr["ScheduleExpressionTimezone"] == "Asia/Jerusalem"


def test_a_sunday_is_a_trading_day_in_tel_aviv_and_not_in_new_york():
    """2026-08-09 is a Sunday. 07:00 UTC is 10:00 in Jerusalem -- mid-session
    on TASE, and a New York window would not run for another day."""
    sunday = _utc(2026, 8, 9, 7, 0)
    assert schedules.in_window(sunday, "tase_hours")
    assert not schedules.in_window(sunday, "us_market_hours")


def test_friday_is_the_weekend_in_tel_aviv():
    """2026-08-07 is a Friday. 14:00 UTC is 17:00 in Jerusalem -- inside the
    9-18 bracket by hour, and still shut, because TASE does not trade Friday.
    The old MON-FRI window polled all day. New York is mid-session."""
    friday = _utc(2026, 8, 7, 14, 0)
    assert not schedules.in_window(friday, "tase_hours")
    assert schedules.in_window(friday, "us_market_hours")


def test_a_thursday_evening_confirm_waits_for_sunday_in_tel_aviv():
    """Thursday 2026-08-06, 20:00 UTC is 23:00 in Jerusalem -- after the close.
    The next session is Sunday the 9th at 09:00 local, which is 06:00 UTC."""
    got = schedules.next_fire_after(_utc(2026, 8, 6, 20, 0), 30, "tase_hours")
    assert got == _utc(2026, 8, 9, 6, 0)


def test_in_window_is_true_for_a_continuous_schedule():
    """A shop's price has no notion of being shut, and a baseline read from
    one is always live."""
    assert schedules.in_window(_utc(2026, 8, 9, 3, 0))
    assert schedules.in_window(_utc(2026, 8, 9, 3, 0), "not_a_window")


def test_the_us_window_still_answers_exactly_as_before():
    """2026-08-04 is a Tuesday. 14:00 UTC is 10:00 New York, mid-session;
    20:33 UTC is 16:33, past the last slot but still inside the 9-16 bracket
    by hour, which is what `in_window` is asked about."""
    assert schedules.in_window(_utc(2026, 8, 4, 14, 0), "us_market_hours")
    assert not schedules.in_window(_utc(2026, 8, 4, 8, 0), "us_market_hours")


def test_every_registered_window_produces_a_usable_expression():
    """A window that cannot express a schedule would fail at confirm time, in
    front of a user, having already been chosen by the Planner."""
    for name in schedules.WINDOWS:
        expr = schedules.expression(5, name)
        assert expr["ScheduleExpression"].startswith("cron(")
        assert expr["ScheduleExpressionTimezone"]
        assert schedules.checks_per_month(5, name) > 0
        assert schedules.next_fire_after(_utc(2026, 8, 4, 12, 0), 5, name)


def test_a_session_is_a_whole_trading_day_of_checks():
    """The only non-arbitrary yardstick for "should have moved by now". A fixed
    threshold cannot work -- a liquid stock ticks every second, an illiquid one
    may not trade for an hour, and the interval is chosen per watch."""
    assert schedules.checks_per_session(5, "us_market_hours") == 96    # 8h
    assert schedules.checks_per_session(5, "tase_hours") == 120        # 10h
    assert schedules.checks_per_session(60, "us_market_hours") == 8


def test_a_continuous_schedule_has_no_session():
    """A shop's price sitting still for a month is normal, not a fault, so
    there is nothing to measure it against."""
    assert schedules.checks_per_session(5) is None
    assert schedules.checks_per_session(5, "not_a_window") is None


def test_a_session_matches_the_expression_it_describes():
    """Same guarantee as checks_per_month: derived from the cron, not from a
    parallel constant that could drift away from it."""
    for name in schedules.WINDOWS:
        for minutes in (1, 5, 30, 60):
            per_day = schedules.checks_per_session(minutes, name)
            per_month = schedules.checks_per_month(minutes, name)
            assert per_month == per_day * schedules.WINDOWS[name].days_per_month


# --- a schedule that fires once and removes itself ----------------------------
#
# The third shape, after rate and cron, and the one a reminder needs: nothing
# is being polled, so there is no interval to choose. The firing is the event.

def test_a_one_shot_is_an_at_expression():
    args = schedules.once_expression(datetime(2026, 8, 6, 9, 0))
    assert args["ScheduleExpression"] == "at(2026-08-06T09:00:00)"


def test_a_one_shot_deletes_itself_when_it_fires():
    """Without this a fired reminder leaves a schedule behind forever, and
    EventBridge Scheduler charges per schedule. It is the same leak this
    project has already fixed twice."""
    assert schedules.once_expression(
        datetime(2026, 8, 6, 9, 0))["ActionAfterCompletion"] == "DELETE"


def test_a_one_shot_carries_its_timezone_separately():
    """`at(...)` takes a naive local time; an offset or a trailing Z is
    rejected. The same split cron uses, for the same reason -- "9am" has to
    mean 9am after the clocks change too."""
    args = schedules.once_expression(datetime(2026, 8, 6, 9, 0),
                                     "Asia/Jerusalem")
    assert args["ScheduleExpressionTimezone"] == "Asia/Jerusalem"
    assert "+" not in args["ScheduleExpression"]
    assert "Z" not in args["ScheduleExpression"]


def test_an_aware_datetime_keeps_its_wall_clock_reading():
    """9am in Jerusalem is `at(...09:00:00)` plus the zone -- not 06:00 UTC.
    Converting here and naming the zone as well would apply the offset twice."""
    aware = datetime(2026, 8, 6, 9, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
    args = schedules.once_expression(aware, "Asia/Jerusalem")
    assert args["ScheduleExpression"] == "at(2026-08-06T09:00:00)"


def test_a_one_shot_accepts_the_string_form_the_api_receives():
    args = schedules.once_expression("2026-08-06T09:00:00")
    assert args["ScheduleExpression"] == "at(2026-08-06T09:00:00)"


def test_a_one_shot_without_a_zone_names_none():
    args = schedules.once_expression(datetime(2026, 8, 6, 9, 0))
    assert "ScheduleExpressionTimezone" not in args


def test_seconds_are_kept_because_the_api_requires_them():
    args = schedules.once_expression("2026-08-06T09:30:15")
    assert args["ScheduleExpression"] == "at(2026-08-06T09:30:15)"
