"""Tests for the watch lifecycle API.

The paths worth covering here are the ones a manual Lambda invoke is worst
at reaching: a malformed body, a conflicting status, a confirm retried
after a partial failure, an interval just outside the allowed range, and an
unexpected exception that must not leak a stack trace to the caller.
"""

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import handler


class FakeTable:
    """A DynamoDB table that records what was done to it."""

    def __init__(self, items=None):
        self.items = dict(items or {})
        self.puts = []
        self.updates = []
        self.deleted = []

    def get_item(self, Key):
        item = self.items.get(next(iter(Key.values())))
        return {"Item": item} if item else {}

    def put_item(self, Item):
        self.puts.append(Item)
        return {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        return {"Attributes": {"watch_id": "w_1", "status": "updated"}}

    def delete_item(self, Key):
        self.deleted.append(next(iter(Key.values())))
        return {}

    def query(self, **kwargs):
        return {"Items": list(self.items.values())}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


@pytest.fixture
def aws(monkeypatch):
    """Point the handler at fake tables and clients.

    Returns a namespace so a test can both drive behaviour (make
    create_schedule conflict) and assert on it (was delete_schedule called).
    """

    def build(watches=None, targets=None):
        watches_table = FakeTable(watches)
        targets_table = FakeTable(targets)
        monkeypatch.setattr(handler, "_watches", lambda: watches_table)
        monkeypatch.setattr(handler, "_targets", lambda: targets_table)

        scheduler = MagicMock()
        scheduler.create_schedule.return_value = {"ScheduleArn": "arn:sched:1"}
        scheduler.update_schedule.return_value = {"ScheduleArn": "arn:sched:1"}

        class ConflictException(Exception):
            pass

        class ResourceNotFoundException(Exception):
            pass

        scheduler.exceptions.ConflictException = ConflictException
        scheduler.exceptions.ResourceNotFoundException = ResourceNotFoundException
        monkeypatch.setattr(handler, "scheduler", scheduler)

        lambda_client = MagicMock()
        monkeypatch.setattr(handler, "lambda_client", lambda_client)

        return SimpleNamespace(
            watches=watches_table,
            targets=targets_table,
            scheduler=scheduler,
            lambda_client=lambda_client,
        )

    return build


def call(route, body=None, watch_id=None):
    event = {"routeKey": route}
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    if watch_id is not None:
        event["pathParameters"] = {"id": watch_id}
    return handler.lambda_handler(event, None)


def body_of(response):
    return json.loads(response["body"])


def proposed(interval=60):
    """60 minutes because a $5/month budget affords it at today's per-check
    cost. Anything much tighter is now correctly refused."""
    return {"w_1": {"watch_id": "w_1", "status": "proposed",
                    "check_interval_min": Decimal(interval)}}


def one_target():
    return {"t_1": {"target_id": "t_1", "watch_id": "w_1"}}


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

def test_missing_route_key_is_rejected(aws):
    aws()
    assert handler.lambda_handler({}, None)["statusCode"] == 400


def test_unknown_route_is_404(aws):
    aws()
    assert call("GET /nope")["statusCode"] == 404


# --------------------------------------------------------------------------
# POST /watches
# --------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"request": "   "},          # whitespace only
    {},                          # absent
    {"request": ""},             # empty
    "{not json",                 # malformed
    "[1, 2]",                    # not an object
])
def test_bad_create_bodies_are_rejected(aws, body):
    aws()
    assert call("POST /watches", body)["statusCode"] == 400


def test_overlong_request_is_rejected(aws):
    aws()
    assert call("POST /watches", {"request": "x" * 2001})["statusCode"] == 400


def test_create_returns_202_and_a_watch_id(aws):
    env = aws()
    response = call("POST /watches", {"request": "watch the steam deck"})

    assert response["statusCode"] == 202
    payload = body_of(response)
    assert payload["status"] == "planning"
    assert payload["watch_id"].startswith("w_")


def test_create_writes_a_planning_row(aws):
    env = aws()
    call("POST /watches", {"request": "watch the steam deck"})

    assert len(env.watches.puts) == 1
    row = env.watches.puts[0]
    assert row["status"] == "planning"
    assert row["prompt"] == "watch the steam deck"
    assert row["user_id"] == "default"


def test_create_invokes_the_planner_asynchronously(aws):
    """The Planner takes ~20s against a 29s gateway ceiling, so this call
    must never be waited on."""
    env = aws()
    call("POST /watches", {"request": "watch the steam deck"})

    kwargs = env.lambda_client.invoke.call_args.kwargs
    assert kwargs["InvocationType"] == "Event"
    assert "watch_id" in json.loads(kwargs["Payload"])


# --------------------------------------------------------------------------
# Missing watches
# --------------------------------------------------------------------------

@pytest.mark.parametrize("route", [
    "GET /watches/{id}",
    "DELETE /watches/{id}",
    "POST /watches/{id}/confirm",
])
def test_operations_on_a_missing_watch_are_404(aws, route):
    aws()
    assert call(route, watch_id="w_nope")["statusCode"] == 404


# --------------------------------------------------------------------------
# POST /watches/{id}/confirm
# --------------------------------------------------------------------------

def test_confirm_creates_schedules_and_activates(aws):
    env = aws(watches=proposed(), targets=one_target())
    response = call("POST /watches/{id}/confirm", watch_id="w_1")

    assert response["statusCode"] == 200
    payload = body_of(response)
    assert payload["status"] == "active"
    assert payload["check_interval_min"] == 60
    assert env.scheduler.create_schedule.call_count == 1


def test_confirm_writes_the_schedule_arn_back(aws):
    env = aws(watches=proposed(), targets=one_target())
    call("POST /watches/{id}/confirm", watch_id="w_1")

    assert len(env.targets.updates) == 1
    assert env.targets.updates[0]["ExpressionAttributeValues"][":a"] == "arn:sched:1"


def test_confirm_honours_an_interval_override(aws):
    """The whole point of plan-then-confirm: the owner can veto the
    Planner's interval before it starts billing."""
    aws(watches=proposed(interval=5), targets=one_target())
    response = call("POST /watches/{id}/confirm",
                    {"check_interval_min": 60}, watch_id="w_1")

    assert body_of(response)["check_interval_min"] == 60


def test_confirming_an_active_watch_conflicts(aws):
    aws(watches={"w_1": {"watch_id": "w_1", "status": "active"}},
        targets=one_target())
    assert call("POST /watches/{id}/confirm",
                watch_id="w_1")["statusCode"] == 409


def test_confirming_with_no_targets_conflicts(aws):
    aws(watches=proposed(), targets={})
    assert call("POST /watches/{id}/confirm",
                watch_id="w_1")["statusCode"] == 409


@pytest.mark.parametrize("interval", [0, -5, 1441, "abc", None])
def test_out_of_range_intervals_are_rejected(aws, interval):
    aws(watches=proposed(), targets=one_target())
    response = call("POST /watches/{id}/confirm",
                    {"check_interval_min": interval}, watch_id="w_1")
    assert response["statusCode"] == 400


def test_confirm_is_safe_to_retry(aws):
    """Schedule names are derived from target_id, so a confirm that failed
    halfway can be called again and updates instead of duplicating."""
    env = aws(watches=proposed(), targets=one_target())
    env.scheduler.create_schedule.side_effect = \
        env.scheduler.exceptions.ConflictException()

    response = call("POST /watches/{id}/confirm", watch_id="w_1")

    assert response["statusCode"] == 200
    assert env.scheduler.update_schedule.call_count == 1


# --------------------------------------------------------------------------
# PATCH /watches/{id}
# --------------------------------------------------------------------------

def test_pausing_an_active_watch_succeeds(aws):
    aws(watches={"w_1": {"watch_id": "w_1", "status": "active"}})
    assert call("PATCH /watches/{id}", {"status": "paused"},
                watch_id="w_1")["statusCode"] == 200


def test_pausing_an_already_paused_watch_conflicts(aws):
    aws(watches={"w_1": {"watch_id": "w_1", "status": "paused"}})
    assert call("PATCH /watches/{id}", {"status": "paused"},
                watch_id="w_1")["statusCode"] == 409


def test_resuming_a_paused_watch_succeeds(aws):
    aws(watches={"w_1": {"watch_id": "w_1", "status": "paused"}})
    assert call("PATCH /watches/{id}", {"status": "active"},
                watch_id="w_1")["statusCode"] == 200


def test_an_unknown_status_is_rejected(aws):
    aws(watches={"w_1": {"watch_id": "w_1", "status": "active"}})
    assert call("PATCH /watches/{id}", {"status": "banana"},
                watch_id="w_1")["statusCode"] == 400


def test_an_empty_patch_is_rejected(aws):
    aws(watches={"w_1": {"watch_id": "w_1", "status": "active"}})
    assert call("PATCH /watches/{id}", {}, watch_id="w_1")["statusCode"] == 400


def test_changing_the_interval_retunes_a_live_schedule(aws):
    """Otherwise the stored interval would disagree with what is running."""
    env = aws(watches={"w_1": {"watch_id": "w_1", "status": "active"}},
              targets=one_target())
    call("PATCH /watches/{id}", {"check_interval_min": 90}, watch_id="w_1")

    assert env.scheduler.create_schedule.call_count == 1


def test_changing_the_interval_of_a_paused_watch_touches_no_schedule(aws):
    env = aws(watches={"w_1": {"watch_id": "w_1", "status": "paused"}},
              targets=one_target())
    call("PATCH /watches/{id}", {"check_interval_min": 90}, watch_id="w_1")

    assert env.scheduler.create_schedule.call_count == 0


# --------------------------------------------------------------------------
# DELETE /watches/{id}
# --------------------------------------------------------------------------

def test_delete_removes_schedules_and_both_rows(aws):
    env = aws(watches={"w_1": {"watch_id": "w_1", "status": "active"}},
              targets=one_target())
    response = call("DELETE /watches/{id}", watch_id="w_1")

    assert response["statusCode"] == 200
    assert env.scheduler.delete_schedule.call_count == 1
    assert env.targets.deleted == ["t_1"]
    assert env.watches.deleted == ["w_1"]


def test_delete_tolerates_an_already_missing_schedule(aws):
    """Already gone is the state we wanted anyway."""
    env = aws(watches={"w_1": {"watch_id": "w_1", "status": "active"}},
              targets=one_target())
    env.scheduler.delete_schedule.side_effect = \
        env.scheduler.exceptions.ResourceNotFoundException()

    response = call("DELETE /watches/{id}", watch_id="w_1")

    assert response["statusCode"] == 200
    assert env.watches.deleted == ["w_1"]


# --------------------------------------------------------------------------
# GET /watches
# --------------------------------------------------------------------------

def test_list_returns_newest_first(aws):
    aws(watches={
        "w_old": {"watch_id": "w_old", "created_at": "2026-01-01T00:00:00"},
        "w_new": {"watch_id": "w_new", "created_at": "2026-07-01T00:00:00"},
    })
    watches = body_of(call("GET /watches"))["watches"]

    assert [w["watch_id"] for w in watches] == ["w_new", "w_old"]


def test_get_returns_the_watch_with_its_targets(aws):
    aws(watches=proposed(), targets=one_target())
    payload = body_of(call("GET /watches/{id}", watch_id="w_1"))

    assert payload["watch"]["watch_id"] == "w_1"
    assert len(payload["targets"]) == 1


# --------------------------------------------------------------------------
# Helpers and failure masking
# --------------------------------------------------------------------------

def test_an_ordinary_target_still_gets_a_plain_rate_schedule(aws):
    """Phase 9 moved expression-building to shared/schedules.py. The default
    must be exactly what it was, or every existing watch changes cadence."""
    env = aws(proposed(), one_target())
    handler.lambda_handler(
        {"routeKey": "POST /watches/{id}/confirm",
         "pathParameters": {"id": "w_1"}}, None)

    args = env.scheduler.create_schedule.call_args.kwargs
    assert args["ScheduleExpression"] == "rate(60 minutes)"
    assert "ScheduleExpressionTimezone" not in args


def test_a_windowed_target_is_scheduled_with_cron_and_a_timezone(aws):
    """A market watch simply does not fire when the market is shut, which is
    what keeps the Checker from ever needing to know what a stock market is.

    The timezone is not decoration: a UTC cron would drift an hour twice a
    year and spend a week each time reading a closed market.
    """
    # A compiled extractor, so the budget gate prices this without a model
    # and a 5-minute interval is affordable -- otherwise confirm refuses and
    # no schedule is created at all.
    targets = {"t_1": {"target_id": "t_1", "watch_id": "w_1",
                       "extractor": {"kind": "jsonpath"},
                       "schedule_window": "us_market_hours"}}
    env = aws(proposed(interval=5), targets)
    handler.lambda_handler(
        {"routeKey": "POST /watches/{id}/confirm",
         "pathParameters": {"id": "w_1"}}, None)

    args = env.scheduler.create_schedule.call_args.kwargs
    assert args["ScheduleExpression"] == "cron(*/5 9-16 ? * MON-FRI *)"
    assert args["ScheduleExpressionTimezone"] == "America/New_York"


def test_a_windowed_watch_is_not_priced_as_if_it_ran_all_night(aws):
    """Left unfixed this overstates a quote watch by about 4x, which is the
    difference between an interval the budget affords and one it refuses."""
    windowed = {"t_1": {"target_id": "t_1", "watch_id": "w_1",
                        "extractor": {"kind": "jsonpath"},
                        "schedule_window": "us_market_hours"}}
    always = {"t_1": {"target_id": "t_1", "watch_id": "w_1",
                      "extractor": {"kind": "jsonpath"}}}

    aws(proposed(interval=5), windowed)
    a = body_of(handler.lambda_handler(
        {"routeKey": "GET /watches/{id}", "pathParameters": {"id": "w_1"}}, None))
    aws(proposed(interval=5), always)
    b = body_of(handler.lambda_handler(
        {"routeKey": "GET /watches/{id}", "pathParameters": {"id": "w_1"}}, None))

    assert a["cost"]["checks_per_month"] == 2016
    assert b["cost"]["checks_per_month"] == 8640
    assert a["cost"]["estimated_monthly_usd"] < b["cost"]["estimated_monthly_usd"]


@pytest.mark.parametrize("value,expected", [
    (Decimal(10), "10"),
    (Decimal("1.5"), "1.5"),
])
def test_decimals_encode_without_losing_their_type(value, expected):
    assert json.dumps(value, default=handler._json_default) == expected


def test_an_unexpected_error_becomes_a_500_with_no_detail(aws, monkeypatch):
    aws(watches=proposed())
    monkeypatch.setattr(handler, "_targets_for",
                        lambda _: (_ for _ in ()).throw(RuntimeError("boom")))

    response = call("GET /watches/{id}", watch_id="w_1")

    assert response["statusCode"] == 500
    assert body_of(response) == {"error": "internal error"}


# --------------------------------------------------------------------------
# Budget guardrail (Phase 8a)
#
# A schedule is the only thing in this system that bills indefinitely, and the
# AWS budget alarms cannot see the Anthropic spend that dominates it. Confirm
# and patch are the two doors to creating one, so both are gated.
# --------------------------------------------------------------------------

def test_confirming_an_unaffordable_interval_is_refused(aws):
    aws(watches=proposed(interval=3), targets=one_target())
    response = call("POST /watches/{id}/confirm", watch_id="w_1")

    assert response["statusCode"] == 409
    assert "budget" in body_of(response)["error"]


def test_the_refusal_names_an_interval_that_would_be_accepted(aws):
    """An error that sends you to a value also refused would be useless."""
    aws(watches=proposed(interval=3), targets=one_target())
    message = body_of(call("POST /watches/{id}/confirm", watch_id="w_1"))["error"]

    suggested = int(message.split("Use ")[1].split(" min")[0])

    aws(watches=proposed(interval=3), targets=one_target())
    retry = call("POST /watches/{id}/confirm",
                 {"check_interval_min": suggested}, watch_id="w_1")
    assert retry["statusCode"] == 200


def test_an_unaffordable_confirm_creates_no_schedule(aws):
    """The gate has to come before the side effect, not after."""
    env = aws(watches=proposed(interval=3), targets=one_target())
    call("POST /watches/{id}/confirm", watch_id="w_1")

    assert env.scheduler.create_schedule.call_count == 0
    assert env.watches.updates == []


def test_patch_cannot_walk_a_watch_below_what_confirm_allowed(aws):
    """Otherwise PATCH would be a way straight around the budget."""
    aws(watches={"w_1": {"watch_id": "w_1", "status": "active"}},
        targets=one_target())
    response = call("PATCH /watches/{id}", {"check_interval_min": 3},
                    watch_id="w_1")

    assert response["statusCode"] == 409
    assert "budget" in body_of(response)["error"]


def test_confirm_reports_what_it_will_cost(aws):
    aws(watches=proposed(), targets=one_target())
    payload = body_of(call("POST /watches/{id}/confirm", watch_id="w_1"))

    assert payload["cost"]["within_budget"] is True
    assert payload["cost"]["estimated_monthly_usd"] > 0


def test_get_reports_cost_so_the_plan_card_can_show_it(aws):
    aws(watches=proposed(), targets=one_target())
    payload = body_of(call("GET /watches/{id}", watch_id="w_1"))

    assert payload["cost"]["interval_min"] == 60
    assert payload["cost"]["estimated_monthly_usd"] > 0


def test_a_watch_with_no_interval_yet_reports_no_cost(aws):
    """A watch still in "planning" has no interval to price."""
    aws(watches={"w_1": {"watch_id": "w_1", "status": "planning"}}, targets={})
    assert body_of(call("GET /watches/{id}", watch_id="w_1"))["cost"] is None


def test_a_browser_target_costs_more_than_an_http_one(aws):
    aws(watches=proposed(), targets={"t_1": {"target_id": "t_1", "watch_id": "w_1"}})
    http = body_of(call("GET /watches/{id}", watch_id="w_1"))["cost"]

    aws(watches=proposed(),
        targets={"t_1": {"target_id": "t_1", "watch_id": "w_1",
                         "fetch_method": "browser"}})
    browser = body_of(call("GET /watches/{id}", watch_id="w_1"))["cost"]

    assert browser["estimated_monthly_usd"] > http["estimated_monthly_usd"]


# --- cost reflects whether a tick actually pays for a model -------------------
#
# Phase 8b makes a compiled extractor ~1000x cheaper than a Haiku call. If the
# API keeps pricing every watch as though it still calls a model, the plan card
# overstates the bill by ~35x and `confirm` refuses intervals the budget
# comfortably affords -- the guardrail turns into an obstacle.

def test_a_watch_with_compiled_extractors_is_priced_without_the_model():
    assert handler._uses_model([{"extractor": {"kind": "css"}}]) is False


def test_a_watch_predating_phase_8b_is_still_priced_with_the_model():
    """Rows written before 8b carry no extractor and fall back to judge()."""
    assert handler._uses_model([{"url": "https://example.com"}]) is True


def test_one_target_without_an_extractor_prices_the_whole_watch_with_the_model():
    """Deliberately pessimistic. Underestimating is the failure that bills."""
    assert handler._uses_model([
        {"extractor": {"kind": "css"}},
        {"url": "https://example.com"},
    ]) is True


def test_a_compiled_watch_is_dramatically_cheaper_than_a_model_one():
    watch = {"check_interval_min": 60}
    compiled = handler._estimate_for(watch, [{"extractor": {"kind": "css"}}])
    modelled = handler._estimate_for(watch, [{"url": "https://example.com"}])

    assert compiled["estimated_monthly_usd"] < modelled["estimated_monthly_usd"] / 100
    # And the derived floor collapses, which is the whole point of expressing
    # the guardrail as a budget rather than a hardcoded minimum interval.
    assert compiled["min_interval_min"] < modelled["min_interval_min"]


# --------------------------------------------------------------------------
# The night of 2026-08-03, which produced both of these
# --------------------------------------------------------------------------

def _windowed_target():
    return {"t_1": {"target_id": "t_1", "watch_id": "w_1",
                    "extractor": {"kind": "jsonpath"},
                    "schedule_window": "us_market_hours"}}


def test_confirming_a_windowed_watch_hourly_is_not_a_500(aws):
    """The real failure. An hourly quote watch was confirmed nine times in one
    evening and returned `500 ValueError: a windowed schedule cannot use a
    60-minute interval` every time, because a cron minute step cannot express
    an hour. It steps the hours field instead now, and 60 means 60.
    """
    env = aws(proposed(interval=60), _windowed_target())
    response = call("POST /watches/{id}/confirm", watch_id="w_1")

    assert response["statusCode"] == 200
    args = env.scheduler.create_schedule.call_args.kwargs
    assert args["ScheduleExpression"] == "cron(0 9-16 ? * MON-FRI *)"


def test_an_interval_the_cron_grid_cannot_express_is_snapped_not_rejected(aws):
    """51 minutes is what the 8a budget floor produces, so this arrives on the
    ordinary path rather than as a curiosity. Snapping goes up, never down, so
    the schedule created can only be cheaper than the estimate approved."""
    env = aws(proposed(interval=51), _windowed_target())
    response = call("POST /watches/{id}/confirm", watch_id="w_1")

    assert response["statusCode"] == 200
    # The stored and reported interval is the one that actually runs, not the
    # one that was asked for -- otherwise the row describes a schedule that
    # does not exist.
    assert body_of(response)["check_interval_min"] == 60
    args = env.scheduler.create_schedule.call_args.kwargs
    assert args["ScheduleExpression"] == "cron(0 9-16 ? * MON-FRI *)"


def test_confirm_says_when_it_will_first_look(aws):
    """The other half of that night: the watch was correct and silent. It was
    deleted the next morning because "active" was the only thing the product
    ever said, and a market watch confirmed after the close does not run for
    sixteen hours."""
    aws(proposed(interval=5), _windowed_target())
    payload = body_of(call("POST /watches/{id}/confirm", watch_id="w_1"))

    assert payload["next_check_at"] is not None
    assert payload["next_check_at"].endswith("Z")


def test_a_continuous_confirm_says_it_too(aws):
    aws(watches=proposed(), targets=one_target())
    payload = body_of(call("POST /watches/{id}/confirm", watch_id="w_1"))
    assert payload["next_check_at"] is not None


def test_only_an_active_watch_claims_a_next_check(aws):
    """A proposed watch has no schedule, so a time here would describe
    something that does not exist."""
    aws(proposed(interval=5), _windowed_target())
    payload = body_of(call("GET /watches/{id}", watch_id="w_1"))
    assert payload["next_check_at"] is None


def test_an_active_watch_reports_its_next_check_on_get(aws):
    aws(watches={"w_1": {"watch_id": "w_1", "status": "active",
                         "check_interval_min": 5}},
        targets=one_target())
    payload = body_of(call("GET /watches/{id}", watch_id="w_1"))
    assert payload["next_check_at"] is not None


# --------------------------------------------------------------------------
# Has it stopped moving, and does that mean anything?
# --------------------------------------------------------------------------

def _stale_target(**over):
    target = {"target_id": "t_1", "watch_id": "w_1",
              "extractor": {"kind": "jsonpath"},
              "schedule_window": "us_market_hours"}
    target.update(over)
    return {"t_1": target}


def _active(interval=5):
    return {"w_1": {"watch_id": "w_1", "status": "active",
                    "check_interval_min": interval}}


def test_a_quote_frozen_for_a_whole_session_is_flagged(aws):
    """A US window at 5 minutes is 96 checks a session. Ninety-six identical
    readings in a row means the price did not move all day, which for a
    traded instrument is a frozen feed, not a stable price."""
    aws(_active(5), _stale_target(unchanged_checks=96,
                                  last_changed_at="2026-08-03T13:00:00Z"))
    stale = body_of(call("GET /watches/{id}", watch_id="w_1"))["staleness"][0]

    assert stale["checks_per_session"] == 96
    assert stale["stale"] is True
    assert stale["last_changed_at"] == "2026-08-03T13:00:00Z"


def test_a_quote_that_moved_recently_is_not_flagged(aws):
    aws(_active(5), _stale_target(unchanged_checks=3))
    assert body_of(call("GET /watches/{id}",
                        watch_id="w_1"))["staleness"][0]["stale"] is False


def test_a_continuous_watch_is_never_called_stale(aws):
    """A shop price sitting unchanged for a month is the normal case for a
    `value` watch, not a fault. Without a window there is no session, so there
    is no claim to make and no flag to raise."""
    aws(_active(5), _stale_target(unchanged_checks=99999,
                                  schedule_window=None))
    stale = body_of(call("GET /watches/{id}", watch_id="w_1"))["staleness"][0]

    assert stale["checks_per_session"] is None
    assert stale["stale"] is False


def test_the_threshold_follows_the_interval(aws):
    """The judgement is computed at read time precisely because a PATCH can
    change the interval after the counter was written. At 30 minutes a session
    is 16 checks, so the same 20 unchanged readings mean something different."""
    aws(_active(5), _stale_target(unchanged_checks=20))
    assert body_of(call("GET /watches/{id}",
                        watch_id="w_1"))["staleness"][0]["stale"] is False

    aws(_active(30), _stale_target(unchanged_checks=20))
    assert body_of(call("GET /watches/{id}",
                        watch_id="w_1"))["staleness"][0]["stale"] is True


def test_a_target_that_has_never_been_checked_is_not_stale(aws):
    aws(_active(5), _stale_target())
    stale = body_of(call("GET /watches/{id}", watch_id="w_1"))["staleness"][0]

    assert stale["unchanged_checks"] == 0
    assert stale["last_changed_at"] is None
    assert stale["stale"] is False


def test_staleness_never_changes_the_watch(aws):
    """It reports and a human decides. Acting on stillness would re-create the
    false positive the unavailable/failed split exists to prevent: escalating a
    watch that is patiently doing exactly its job."""
    env = aws(_active(5), _stale_target(unchanged_checks=100000))
    call("GET /watches/{id}", watch_id="w_1")

    assert env.watches.updates == []
    assert env.targets.updates == []
    assert env.scheduler.delete_schedule.call_count == 0
