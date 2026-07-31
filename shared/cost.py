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
) -> float:
    if interval_min <= 0:
        raise ValueError("interval_min must be positive")
    checks = CHECKS_PER_MONTH_AT_ONE_MINUTE / interval_min
    return checks * targets * cost_per_check(fetch_method, uses_model)


def min_interval_for_budget(
    budget_usd: float | None = None,
    targets: int = 1,
    fetch_method: str = "http",
    uses_model: bool = True,
) -> int:
    """Tightest interval whose monthly cost still fits the budget.

    Inverts monthly_cost. Rounded up, because rounding down would return an
    interval the budget does not actually allow.
    """
    budget = monthly_budget_usd() if budget_usd is None else budget_usd
    per_month_at_one_minute = (
        CHECKS_PER_MONTH_AT_ONE_MINUTE * targets * cost_per_check(fetch_method, uses_model)
    )
    needed = math.ceil(per_month_at_one_minute / budget)
    return max(MIN_INTERVAL_MIN, min(MAX_INTERVAL_MIN, needed))


def estimate(
    interval_min: int,
    targets: int = 1,
    fetch_method: str = "http",
    uses_model: bool = True,
) -> dict:
    """A JSON-safe summary, for API responses and the plan card."""
    budget = monthly_budget_usd()
    floor = min_interval_for_budget(budget, targets, fetch_method, uses_model)
    cost = monthly_cost(interval_min, targets, fetch_method, uses_model)
    return {
        "interval_min": interval_min,
        "targets": targets,
        "fetch_method": fetch_method,
        "cost_per_check_usd": round(cost_per_check(fetch_method, uses_model), 8),
        "estimated_monthly_usd": round(cost, 4),
        "monthly_budget_usd": budget,
        "min_interval_min": floor,
        "within_budget": cost <= budget,
    }
