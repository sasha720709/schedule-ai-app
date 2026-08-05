"""When a watch is allowed to run, and how many times a month that is.

Until Phase 9 there was exactly one answer: `rate(N minutes)`, forever, at all
hours. That is right for a shop's price and wrong for anything with a calendar
-- a market that is shut, an office that is closed, a reminder that happens
once at 9am.

EventBridge Scheduler already supports everything needed, which is why this is
a small file rather than a service. Verified against the CLI:
`--schedule-expression-timezone` (so DST is the scheduler's problem, not ours),
`--start-date` / `--end-date`, and `--action-after-completion`.

## An honest note about why the market window exists

It was first justified as a correctness fix: outside trading hours CNBC's
`last` holds the previous close, so -- the argument ran -- a relative watch
could fire at market close against a frozen number.

**That argument is wrong, and was checked rather than assumed.** A frozen quote
is not a wrong quote; it is the last real price. Walk it through: baseline
$333.43 at 11:00, the stock closes at $335, and every out-of-hours check reads
$335 and correctly does not fire. If it closes at $330 the watch fires -- and
it *should*, because the price did go down. There are no false fires.

So what the window actually buys, in order of how much it matters:

1. **It stops hammering a free third-party endpoint we do not own.** A
   one-minute quote watch makes 43,200 requests a month, of which 35,010 cannot
   return anything new. CNBC's keyless quote API is the one that answered when
   Yahoo returned 429 and stooq returned 404 from Lambda; losing it to rate
   limiting would break every quote watch at once. This is the real reason.
2. It makes the plan card's monthly figure honest, rather than overstating a
   windowed watch by ~4x.
3. It is the mechanism reminders need anyway (`cron`, `at`), so the quote
   window is the cheap first user of a seam that has to exist regardless.

The money is not a reason: the saving is about fourteen cents a month.

## The previous-close baseline: decided 2026-08-04, and it is not a bug

This file used to call it the real correctness bug -- a watch created outside
trading hours takes its baseline from the previous close, so "goes down from
the current" asked on Sunday measures against Friday.

**The owner settled it: the close is a fine baseline.** Asked on a Sunday what
"current" means, Friday's close *is* current; there is no other number, and
refusing to plan a watch outside market hours would be a worse product than
using the last real price.

But accepting it sharpens what is left, so do not read this as closed:

**"Any change" is not a condition on a price, it is a guarantee.** A stock
does not reopen at the previous close, so a watch with
`relative_change_pct: 0` and a close for a baseline fires in the first seconds
of the next session, every time. Measured in-hours on 2026-08-04: baseline
306.40, first check 306.49, condition met -- a 0.03% move on the very first
tick. The email is correct and carries no information.

The fix is not to invent a percentage on the user's behalf; `plan.py` forbids
that deliberately, and fabricating 5% is a bug this project already shipped
once. The fix is to store `baseline_at` and `baseline_source`, and to say at
plan time that a zero-percent condition will fire at the opening bell. Same
shape as the silent-window fix below: the system knows something the user does
not. Full argument and ordering in `docs/shares-roadmap.md` §1.

## And the one a real night found (2026-08-04)

A window nobody reports is a window the user experiences as a bug. A quote
watch confirmed at 23:33 Israel time -- 16:33 New York, three minutes after
the last slot of `9-16` on a Monday -- would not run for another sixteen and a
half hours. Nothing said so, so it looked broken and was deleted the next
morning while it was behaving exactly as designed. `next_fire_after` exists to
be shown at confirm time. The schedule was right; the silence was the defect.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# 21 trading days a month is the usual convention (252 sessions / 12).
TRADING_DAYS_PER_MONTH = 21
MINUTES_PER_MONTH_CONTINUOUS = 60 * 24 * 30  # 43200, matches cost.py


class Window:
    """A recurring slice of the week during which a watch may run."""

    def __init__(self, name, *, hours, days, timezone, days_per_month):
        self.name = name
        self.hours = hours              # cron hours field, e.g. "9-16"
        self.days = days                # cron day-of-week field
        self.timezone = timezone
        self.days_per_month = days_per_month

    @property
    def hours_per_day(self) -> int:
        low, _, high = self.hours.partition("-")
        return int(high or low) - int(low) + 1

    def minutes_per_month(self) -> int:
        return self.days_per_month * self.hours_per_day * 60


# 9-16 rather than the exact 09:30-16:00 session, on purpose. Scheduler takes
# one expression per schedule and "09:30 to 16:00" needs two (`30-59 9` plus
# `* 10-15`), which would mean two schedules per target and two of everything
# that creates, pauses and deletes them.
#
# So the window brackets the session with a margin at each end: nothing is
# missed at the open or at the closing print, at the cost of ~90 minutes a day
# reading a value that has not moved. Established above, that is harmless --
# a frozen quote is the last real price, not a wrong one.
US_MARKET = Window(
    "us_market_hours",
    hours="9-16",
    days="MON-FRI",
    timezone="America/New_York",
    days_per_month=TRADING_DAYS_PER_MONTH,
)

# Tel Aviv trades **Sunday to Thursday**, which is the entire reason this stops
# being one hardcoded window. A `MON-FRI` schedule does not merely mistime a
# TASE watch: it misses Sunday's session completely and then polls all Friday
# while the exchange is shut. Continuous trading runs to about 17:15, earlier
# on Sundays, so 9-18 brackets it with the usual margin at each end.
#
# Written as an explicit list rather than `SUN-THU`. A weekday *range* that
# wraps the end of the week is the kind of thing that is either quietly wrong
# or quietly unsupported, and there is nothing to gain by finding out which.
TEL_AVIV = Window(
    "tase_hours",
    hours="9-18",
    days="SUN,MON,TUE,WED,THU",
    timezone="Asia/Jerusalem",
    days_per_month=TRADING_DAYS_PER_MONTH,
)

# Continuous trading to 17:30 Frankfurt time.
XETRA = Window(
    "xetra_hours",
    hours="9-18",
    days="MON-FRI",
    timezone="Europe/Berlin",
    days_per_month=TRADING_DAYS_PER_MONTH,
)

# 08:00-16:30 London.
LONDON = Window(
    "lse_hours",
    hours="8-17",
    days="MON-FRI",
    timezone="Europe/London",
    days_per_month=TRADING_DAYS_PER_MONTH,
)

WINDOWS = {w.name: w for w in (US_MARKET, TEL_AVIV, XETRA, LONDON)}


def get(name):
    """A window by name, or None for "run continuously"."""
    return WINDOWS.get(name or "")


# Divisors of 60, which are the only sub-hourly cadences a cron minute step
# can space evenly. `*/45` is legal cron and fires at :00 and :45 -- a
# 45-minute gap followed by a 15-minute one, which is not a 45-minute
# interval by any reading.
_EVEN_STEPS = [1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60]


def snap(interval_min: int, window_name=None) -> int:
    """The nearest cadence a windowed schedule can actually express.

    A cron grid cannot say "every 51 minutes", and 51 is exactly the sort of
    number that arrives here: `cost.py` derives the interval floor from a
    monthly budget, so it is arbitrary by construction. This used to raise,
    which turned a budget-clamped quote watch into a 500 at confirm time.

    Rounds **up**, never down, and the direction is the whole safety argument:
    a longer interval is fewer checks, so a snapped schedule can only ever
    cost less than the estimate that was approved. Rounding down would create
    a schedule that bills more than the budget gate agreed to, which is the
    one failure nobody notices.

    Continuous schedules are `rate(...)` and can express any interval, so
    they are returned untouched.
    """
    if interval_min <= 0:
        raise ValueError("interval_min must be positive")
    if get(window_name) is None:
        return interval_min
    if interval_min <= 60:
        return next(s for s in _EVEN_STEPS if s >= interval_min)
    return -(-interval_min // 60) * 60  # ceil to whole hours


def _cron_slots(interval_min: int, window: Window):
    """The cron minute and hour fields, and the exact times they fire at.

    Returned together on purpose. `expression` needs the fields and
    `checks_per_month` needs the count; they are one fact told twice, once to
    EventBridge and once to the bill, and deriving both here is what stops
    them drifting.
    """
    low, _, high = window.hours.partition("-")
    low, high = int(low), int(high or low)
    interval_min = snap(interval_min, window.name)

    if interval_min < 60:
        minute_field = "*" if interval_min == 1 else f"*/{interval_min}"
        minutes = range(0, 60, interval_min)
        hour_field, hours = window.hours, range(low, high + 1)
    else:
        # An hour or more steps the *hours* field and pins the minute to :00.
        # Stepping the minutes field instead is the bug this replaces:
        # `*/60` is silently `0`, so "every four hours" fired every hour.
        step = interval_min // 60
        minute_field, minutes = "0", [0]
        hours = range(low, high + 1, step)
        hour_field = window.hours if step == 1 else f"{window.hours}/{step}"

    return minute_field, hour_field, [(h, m) for h in hours for m in minutes]


def expression(interval_min: int, window_name=None) -> dict:
    """The Scheduler arguments for this interval, windowed or not.

    Returns the kwargs to splat into `create_schedule` / `update_schedule`, so
    callers never assemble a cron string themselves.
    """
    if interval_min <= 0:
        raise ValueError("interval_min must be positive")

    window = get(window_name)
    if window is None:
        # Scheduler wants the unit singular at 1: 'rate(1 minute)'.
        plural = "" if interval_min == 1 else "s"
        return {"ScheduleExpression": f"rate({interval_min} minute{plural})"}

    minute_field, hour_field, _ = _cron_slots(interval_min, window)
    return {
        "ScheduleExpression": (
            f"cron({minute_field} {hour_field} ? * {window.days} *)"
        ),
        # DST is the scheduler's problem, not ours. A UTC cron would drift by
        # an hour twice a year and read a closed market for a week each time.
        "ScheduleExpressionTimezone": window.timezone,
    }


def once_expression(when, timezone_name=None) -> dict:
    """A schedule that fires exactly once and then removes itself.

    `at(...)` is the third schedule shape, after `rate` and `cron`, and it is
    the one a reminder needs: nothing is being polled, so there is no interval
    to choose. The firing *is* the event.

    Two details that are the whole reason this is not one line.

    **`ActionAfterCompletion: DELETE`.** Without it, a fired one-shot leaves a
    schedule behind forever. EventBridge Scheduler charges per schedule, and a
    dead schedule that nobody deletes is exactly the leak this project has
    already fixed twice -- the Notifier's stale `schedule_arn`, and the
    unpaginated teardown that left half a watch's schedules alive.

    **`at(...)` takes a naive local time and a separate timezone.** Passing an
    offset or a trailing `Z` is rejected by the API, so the moment is formatted
    without one and the zone is named alongside -- the same split `cron` uses,
    and for the same reason: DST is the scheduler's problem, not ours. "9am"
    means 9am after the clocks change too.
    """
    if isinstance(when, str):
        moment = datetime.fromisoformat(when)
    else:
        moment = when

    # Whatever the caller had, `at()` wants it bare. A datetime carrying a
    # zone keeps its wall-clock reading here; the zone travels in its own
    # field, where the scheduler can apply it.
    local = moment.replace(tzinfo=None)

    args = {"ScheduleExpression": f"at({local.strftime('%Y-%m-%dT%H:%M:%S')})",
            "ActionAfterCompletion": "DELETE"}
    if timezone_name:
        args["ScheduleExpressionTimezone"] = timezone_name
    return args


def repeating_expression(when, repeat: str, timezone_name=None) -> dict:
    """A reminder that comes back: every day, or every week on the same day.

    The fourth schedule shape, and the one the owner asked for by name --
    "set a reminder for 9pm to learn English, every day". `once_expression`
    could not express it, and pretending it could by re-creating a one-shot
    after each firing would put the whole thing at the mercy of one Lambda
    invocation succeeding.

    Deliberately **not** `rate(1 day)`. A rate schedule counts from whenever it
    was created, so "every day at 9pm" confirmed at 21:07 drifts by however
    long each invocation took, and a rate schedule created at 14:00 fires at
    14:00 forever. A cron pins the wall-clock time, which is the entire request.

    No `ActionAfterCompletion` -- it must survive its own firing. What stops it
    is the watch's term, checked by the Checker, exactly as a repeating vacancy
    watch is stopped.
    """
    if isinstance(when, str):
        when = datetime.fromisoformat(when)

    if repeat == "daily":
        fields = f"{when.minute} {when.hour} * * ? *"
    elif repeat == "weekly":
        fields = f"{when.minute} {when.hour} ? * {_DAY_NAMES[when.weekday()]} *"
    else:
        raise ValueError(f"unknown repeat {repeat!r}")

    args = {"ScheduleExpression": f"cron({fields})"}
    if timezone_name:
        # The same reason `cron` windows carry one: a UTC cron drifts by an
        # hour twice a year, and a reminder that moves to 8pm every autumn is
        # a reminder nobody trusts.
        args["ScheduleExpressionTimezone"] = timezone_name
    return args


def checks_per_month(interval_min: int, window_name=None) -> float:
    """How many times this actually runs, which is what a cost estimate needs."""
    if interval_min <= 0:
        raise ValueError("interval_min must be positive")
    window = get(window_name)
    if window is None:
        return MINUTES_PER_MONTH_CONTINUOUS / interval_min
    _, _, slots = _cron_slots(interval_min, window)
    return float(len(slots) * window.days_per_month)


# --------------------------------------------------------------------------
# When does this actually run next?
# --------------------------------------------------------------------------

# datetime.weekday(): Monday is 0.
_DAY_NAMES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _allowed_weekdays(window: Window) -> set:
    field = window.days.upper()
    if field in ("*", "?"):
        return set(range(7))
    if "-" in field:
        first, last = (_DAY_NAMES.index(d) for d in field.split("-", 1))
        return set(range(first, last + 1))
    return {_DAY_NAMES.index(d) for d in field.split(",")}


def checks_per_session(interval_min: int, window_name=None):
    """How many checks one full trading session contains, or None.

    This is the only non-arbitrary yardstick for "this value should have moved
    by now". A fixed threshold cannot work: a liquid stock ticks every second
    while an illiquid small-cap may genuinely not trade for an hour, and the
    check interval is chosen per watch. One whole session is a claim anybody
    can evaluate -- *the price did not move all day* -- and it falls out of the
    window arithmetic already here.

    None for a continuous schedule, deliberately. A shop's price sitting
    unchanged for a month is the normal case for a `value` watch, not a fault,
    so there is no session to be measured against and no signal to give.
    """
    window = get(window_name)
    if window is None:
        return None
    _, _, slots = _cron_slots(interval_min, window)
    return len(slots)


def in_window(moment: datetime, window_name=None) -> bool:
    """Is this moment inside the window -- i.e. was the market open?

    Used to label a baseline. "Tell me when Apple drops 5% from current" reads
    a price and computes the threshold from it, and whether that price was live
    or the previous close is the difference between a threshold the user
    watched happen and one taken from a number they never saw. The owner
    decided the close is an acceptable baseline; this is what lets the plan
    card *say* which one it was rather than presenting both identically.

    True for a continuous window, which has no notion of being shut, and true
    when the timezone cannot be loaded -- an unknown answer must not be
    reported as "stale".
    """
    window = get(window_name)
    if window is None:
        return True
    try:
        tz = ZoneInfo(window.timezone)
    except Exception:
        return True

    local = moment.astimezone(tz)
    if local.weekday() not in _allowed_weekdays(window):
        return False
    low, _, high = window.hours.partition("-")
    return int(low) <= local.hour <= int(high or low)


def next_fire_after(after: datetime, interval_min: int, window_name=None):
    """When this schedule will run for the first time, as a UTC datetime.

    This answers the question a user asks the second they confirm a watch, and
    the one nothing in the product answered before: *when will you look?*

    It exists because of a real, wasted night. A quote watch was confirmed at
    23:33 Israel time, which is 16:33 in New York -- three minutes after the
    last slot of a `9-16` window on a Monday. Its next check was 09:00 Tuesday
    New York, sixteen and a half hours later. The confirm response said
    `"status": "active"` and nothing else, so the watch was indistinguishable
    from a broken one, and it was deleted the next morning for being silent
    while it was in fact behaving exactly as designed. A window that nothing
    reports is a window the user experiences as a bug.

    Returns None if the window's timezone is not installed, because a missing
    tzdata must degrade to "we cannot say" rather than turn a working confirm
    into a 500.
    """
    if interval_min <= 0:
        raise ValueError("interval_min must be positive")

    window = get(window_name)
    if window is None:
        # `rate(...)` counts from the moment the schedule is created.
        return after.astimezone(timezone.utc) + timedelta(minutes=interval_min)

    try:
        tz = ZoneInfo(window.timezone)
    except Exception:
        return None

    _, _, slots = _cron_slots(interval_min, window)
    days = _allowed_weekdays(window)
    local = after.astimezone(tz)

    # Eight days covers the worst case: a Friday-evening confirm on a
    # weekday-only window waits until Monday, and a leading skipped day makes
    # seven too tight.
    for offset in range(8):
        day = (local + timedelta(days=offset)).date()
        if day.weekday() not in days:
            continue
        for hour, minute in slots:
            fire = datetime(day.year, day.month, day.day, hour, minute,
                            tzinfo=tz)
            if fire > local:
                return fire.astimezone(timezone.utc)
    return None
