"""What a check costs, in one place.

Copied into each Lambda's zip by its build.sh rather than shared as a Lambda
Layer -- layers are still a deferred gap, and one small file duplicated by a
build script is cheaper than the infrastructure to avoid duplicating it.

Every figure here is either a published rate or a Phase 6 measurement, and
the measurements are noted as such. Rates change; when they do, this file is
the only thing to edit.

The important design choice is that limits are expressed as a **monthly budget
per watch**, and the minimum allowed interval is derived from it. Phase 8 is
about to cut the cost of a check by roughly a thousandfold, and a budget
automatically permits tighter intervals when that lands. A hardcoded "minimum
15 minutes" would have to be found and changed by hand, and would silently
keep the old architecture's caution forever.
"""

import math
import os

import schedules

# --- published rates, us-east-1, on-demand ------------------------------------

# Claude Haiku 4.5, per million tokens.
HAIKU_INPUT_PER_MTOK = 1.00
HAIKU_OUTPUT_PER_MTOK = 5.00

# Lambda: per GB-second, plus a flat per-request charge.
LAMBDA_PER_GB_SECOND = 0.0000166667
LAMBDA_PER_REQUEST = 0.0000002

# DynamoDB on-demand writes, per request unit.
DYNAMODB_PER_WRITE = 0.00000125

# EventBridge Scheduler, per invocation.
SCHEDULER_PER_INVOCATION = 0.000001

# --- shape of one check -------------------------------------------------------

# checker/check.py truncates page text at MAX_PAGE_CHARS = 20000. At roughly
# four characters per token that is ~5000 tokens, plus the system prompt.
JUDGE_INPUT_TOKENS = 5200
JUDGE_OUTPUT_TOKENS = 100

# Measured in Phase 6: warm renders ~4.7s at 2048MB, 915MB actually used.
FETCHER_MEMORY_GB = 2.0
FETCHER_SECONDS = 4.7

# The Checker itself: 256MB, and it mostly waits on something else.
CHECKER_MEMORY_GB = 0.25
CHECKER_SECONDS_WITH_MODEL = 2.5

# Deterministic (Tier 0) checks, split by fetch method because the Checker
# *blocks* while the browser Fetcher renders -- it is billed for that wait as
# well as the Fetcher being billed for the work.
#
# The browser figure is measured: warm Tier 0 ticks on the Steam Deck page
# reported 6545ms (156MB of 256MB used, so parsing 1.49MB of HTML with
# BeautifulSoup is not memory-bound). The first version of this file assumed
# 0.4s for both, which understated a browser check by about a sixth.
CHECKER_SECONDS_DETERMINISTIC_BROWSER = 6.5
# NOT yet measured -- a plain GET plus a BeautifulSoup parse, with no second
# Lambda to wait on. Kept at the original estimate and flagged here so the next
# HTTP-target watch is used to replace it rather than to confirm a guess.
CHECKER_SECONDS_DETERMINISTIC_HTTP = 0.4

CHECKS_PER_MONTH_AT_ONE_MINUTE = 60 * 24 * 30  # 43200

MIN_INTERVAL_MIN = 1
MAX_INTERVAL_MIN = 1440

# Overridable per environment so the ceiling can be raised without a deploy of
# this file. Five dollars a month per watch is deliberately tight: at today's
# cost it forces ~49-minute intervals, which is the honest consequence of
# paying a language model to re-read a page 14,400 times a month.
DEFAULT_MONTHLY_BUDGET_USD = 5.00


def monthly_budget_usd() -> float:
    raw = os.environ.get("MONTHLY_BUDGET_USD")
    if not raw:
        return DEFAULT_MONTHLY_BUDGET_USD
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MONTHLY_BUDGET_USD
    return value if value > 0 else DEFAULT_MONTHLY_BUDGET_USD


def _lambda_cost(memory_gb: float, seconds: float) -> float:
    return memory_gb * seconds * LAMBDA_PER_GB_SECOND + LAMBDA_PER_REQUEST


def judge_cost() -> float:
    """One Haiku call over a truncated page. The dominant cost today."""
    return (
        JUDGE_INPUT_TOKENS / 1_000_000 * HAIKU_INPUT_PER_MTOK
        + JUDGE_OUTPUT_TOKENS / 1_000_000 * HAIKU_OUTPUT_PER_MTOK
    )


def fetch_cost(fetch_method: str) -> float:
    """A plain GET is almost free; Chromium is not.

    Once the model leaves the hot path this becomes the dominant term -- about
    eighty times a plain fetch -- which is why the Planner should prefer a JSON
    endpoint over a rendered page.
    """
    if fetch_method == "browser":
        return _lambda_cost(FETCHER_MEMORY_GB, FETCHER_SECONDS)
    return 0.0  # the Checker's own invocation already covers an HTTP GET


# One Tier 1 ranking: a Haiku call over the items that just appeared, judging
# them against the original request. Batched -- all the new items in one call,
# because ten job cards are about 500 tokens and judging them separately would
# cost ten times as much for scores that could not be compared with each other.
#
# Paid per *notification*, not per check, which is the whole reason this does
# not undo Phase 8b. A jobs watch at 15-minute intervals firing twice a day
# spends about $0.19/month on ranking; the same watch judging on every tick
# would spend $16.42.
RANK_BASE_INPUT_TOKENS = 700     # the system prompt and the request
RANK_TOKENS_PER_ITEM = 90        # one card: title, company, place, date
RANK_OUTPUT_TOKENS_PER_ITEM = 30


def rank_cost(items: int = 10) -> float:
    """One batched ranking call over `items` things that just appeared."""
    inputs = RANK_BASE_INPUT_TOKENS + RANK_TOKENS_PER_ITEM * max(items, 0)
    outputs = RANK_OUTPUT_TOKENS_PER_ITEM * max(items, 0)
    return (inputs / 1_000_000 * HAIKU_INPUT_PER_MTOK
            + outputs / 1_000_000 * HAIKU_OUTPUT_PER_MTOK)


def can_afford_rank(interval_min: int, targets: int = 1,
                    fetch_method: str = "http", spend_usd: float = 0.0,
                    items: int = 10) -> bool:
    """Is there room in this watch's monthly budget to rank what appeared?

    Charged against the same `MONTHLY_BUDGET_USD` as checks and repairs, for
    the reason given on `can_afford_repair`: one guarantee is easier to reason
    about than three, and it self-limits with no constant to tune. A watch that
    fires constantly stops being ranked and keeps notifying -- ranking is the
    part that degrades, never the notification.
    """
    checks = monthly_cost(interval_min, targets, fetch_method, uses_model=False)
    return checks + spend_usd + rank_cost(items) <= monthly_budget_usd()


# One question-building call, at plan time, over the items the search already
# returned. Paid once per watch, never on a tick.
QUESTIONS_BASE_INPUT_TOKENS = 800
QUESTIONS_OUTPUT_TOKENS = 400


def questions_cost(items: int = 25) -> float:
    inputs = QUESTIONS_BASE_INPUT_TOKENS + RANK_TOKENS_PER_ITEM * max(items, 0)
    return (inputs / 1_000_000 * HAIKU_INPUT_PER_MTOK
            + QUESTIONS_OUTPUT_TOKENS / 1_000_000 * HAIKU_OUTPUT_PER_MTOK)


# One Tier 1 repair: a Haiku call that re-reads the page and re-derives the
# spec. Bigger than a judge call in both directions -- it is shown the old
# extractor and the error alongside the text, and it writes a spec rather than
# a verdict.
REPAIR_INPUT_TOKENS = 6000
REPAIR_OUTPUT_TOKENS = 400


def repair_cost() -> float:
    return (
        REPAIR_INPUT_TOKENS / 1_000_000 * HAIKU_INPUT_PER_MTOK
        + REPAIR_OUTPUT_TOKENS / 1_000_000 * HAIKU_OUTPUT_PER_MTOK
    )


def can_afford_repair(interval_min: int, targets: int = 1,
                      fetch_method: str = "http",
                      repair_spend_usd: float = 0.0) -> bool:
    """Is there room in this watch's monthly budget for one more repair?

    Repairs are charged against the same `MONTHLY_BUDGET_USD` as checks rather
    than getting an allowance of their own. One guarantee is easier to reason
    about than two, and it removes the failure mode where a watch is inside its
    check budget while quietly spending far more on repairing itself.

    It also self-limits without a retry counter to tune. A watch that fails
    every tick burns through the remaining budget quickly and is escalated to
    `degraded`; a watch that breaks once a month repairs itself for years. The
    number that decides this is the same $5 the owner already understands, not
    a MAX_REPAIRS constant that ages badly -- the same reasoning that made the
    interval floor derived rather than hardcoded.
    """
    checks = monthly_cost(interval_min, targets, fetch_method, uses_model=False)
    return checks + repair_spend_usd + repair_cost() <= monthly_budget_usd()


def cost_per_check(fetch_method: str = "http", uses_model: bool = True) -> float:
    if uses_model:
        checker_seconds = CHECKER_SECONDS_WITH_MODEL
    elif fetch_method == "browser":
        checker_seconds = CHECKER_SECONDS_DETERMINISTIC_BROWSER
    else:
        checker_seconds = CHECKER_SECONDS_DETERMINISTIC_HTTP
    total = (
        _lambda_cost(CHECKER_MEMORY_GB, checker_seconds)
        + fetch_cost(fetch_method)
        + DYNAMODB_PER_WRITE
        + SCHEDULER_PER_INVOCATION
    )
    if uses_model:
        total += judge_cost()
    return total


def monthly_cost(
    interval_min: int,
    targets: int = 1,
    fetch_method: str = "http",
    uses_model: bool = True,
    window: str | None = None,
) -> float:
    """Cost of running this watch for a month.

    `window` names a recurring slice of the week the watch is confined to (see
    shared/schedules.py). Without it the old assumption holds: every minute of
    every day. With one, a quote watch runs 10,080 times a month rather than
    43,200, and pretending otherwise overstates the plan card by ~4x.
    """
    checks = schedules.checks_per_month(interval_min, window)
    return checks * targets * cost_per_check(fetch_method, uses_model)


def min_interval_for_budget(
    budget_usd: float | None = None,
    targets: int = 1,
    fetch_method: str = "http",
    uses_model: bool = True,
    window: str | None = None,
) -> int:
    """Tightest interval whose monthly cost still fits the budget.

    Inverts monthly_cost. Rounded up, because rounding down would return an
    interval the budget does not actually allow.
    """
    budget = monthly_budget_usd() if budget_usd is None else budget_usd
    per_month_at_one_minute = (
        schedules.checks_per_month(1, window) * targets
        * cost_per_check(fetch_method, uses_model)
    )
    needed = math.ceil(per_month_at_one_minute / budget)
    return max(MIN_INTERVAL_MIN, min(MAX_INTERVAL_MIN, needed))


def estimate(
    interval_min: int,
    targets: int = 1,
    fetch_method: str = "http",
    uses_model: bool = True,
    window: str | None = None,
) -> dict:
    """A JSON-safe summary, for API responses and the plan card."""
    budget = monthly_budget_usd()
    floor = min_interval_for_budget(budget, targets, fetch_method, uses_model, window)
    cost = monthly_cost(interval_min, targets, fetch_method, uses_model, window)
    return {
        "interval_min": interval_min,
        "targets": targets,
        "fetch_method": fetch_method,
        "window": window,
        "checks_per_month": round(schedules.checks_per_month(interval_min, window)),
        "cost_per_check_usd": round(cost_per_check(fetch_method, uses_model), 8),
        "estimated_monthly_usd": round(cost, 4),
        "monthly_budget_usd": budget,
        "min_interval_min": floor,
        "within_budget": cost <= budget,
    }
