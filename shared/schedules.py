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

## Still open, and *this* is the real correctness bug

A watch created outside trading hours takes its baseline from the previous
close. "Tell me when Apple goes down from the current" asked on Sunday is
measured against Friday, so if Monday opens higher the watch is comparing
against a number the user never saw. Windowing does not fix this -- the fix is
to take the baseline during a session, or to say plainly which close it came
from. Tracked in `docs/phase-9-watch-kinds.md`.
"""

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

WINDOWS = {US_MARKET.name: US_MARKET}


def get(name):
    """A window by name, or None for "run continuously"."""
    return WINDOWS.get(name or "")


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

    if interval_min >= 60:
        raise ValueError(
            f"a windowed schedule cannot use a {interval_min}-minute interval: "
            f"cron steps run within the hour, so anything at or above 60 would "
            f"silently fire hourly instead"
        )

    minutes = "*" if interval_min == 1 else f"*/{interval_min}"
    return {
        "ScheduleExpression": (
            f"cron({minutes} {window.hours} ? * {window.days} *)"
        ),
        # DST is the scheduler's problem, not ours. A UTC cron would drift by
        # an hour twice a year and read a closed market for a week each time.
        "ScheduleExpressionTimezone": window.timezone,
    }


def checks_per_month(interval_min: int, window_name=None) -> float:
    """How many times this actually runs, which is what a cost estimate needs."""
    if interval_min <= 0:
        raise ValueError("interval_min must be positive")
    window = get(window_name)
    minutes = (MINUTES_PER_MONTH_CONTINUOUS if window is None
               else window.minutes_per_month())
    return minutes / interval_min
