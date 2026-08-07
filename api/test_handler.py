"""Tests for the watch lifecycle API.

The paths worth covering here are the ones a manual Lambda invoke is worst
at reaching: a malformed body, a conflicting status, a confirm retried
after a partial failure, an interval just outside the allowed range, and an
unexpected exception that must not leak a stack trace to the caller.
"""

import json
from datetime import datetime, timedelta, timezone
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
    # The target's schedule, and the watch-level one a time-triggered watch
    # would own. Trying both unconditionally is cheaper than storing a flag to
    # decide with, and the second is a no-op here.
    assert [c.kwargs["Name"] for c in env.scheduler.delete_schedule.call_args_list] \
        == ["schedule-ai-app-t_1", "schedule-ai-app-w_1"]
    assert env.targets.deleted == ["t_1"]
    assert env.watches.deleted == ["w_1"]


def test_delete_removes_the_schedule_of_a_watch_that_has_no_targets(aws):
    """A time-triggered watch stores `targets: []` and owns its schedule
    directly. A teardown that only walked targets would delete nothing and
    leave it billing -- the unpaginated-query failure by a different road."""
    env = aws(watches={"w_1": {"watch_id": "w_1", "status": "active"}},
              targets={})
    response = call("DELETE /watches/{id}", watch_id="w_1")

    assert response["statusCode"] == 200
    assert body_of(response)["schedules_deleted"] == 1
    assert env.scheduler.delete_schedule.call_args.kwargs["Name"] \
        == "schedule-ai-app-w_1"


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


# --------------------------------------------------------------------------
# A repeating watch is the only thing here that does not stop by itself
# --------------------------------------------------------------------------

def test_confirming_a_repeating_watch_gives_it_a_term(aws):
    """Every other watch reaches a terminal state on its own and stops
    billing. Without a term a forgotten vacancy watch checks for years."""
    watches = proposed(interval=60)
    watches["w_1"]["repeating"] = True
    aws(watches, one_target())
    payload = body_of(call("POST /watches/{id}/confirm", watch_id="w_1"))

    assert payload["repeating"] is True
    assert payload["expires_at"] is not None


def test_a_one_shot_watch_gets_no_expiry(aws):
    """It already has one: firing."""
    aws(watches=proposed(), targets=one_target())
    payload = body_of(call("POST /watches/{id}/confirm", watch_id="w_1"))

    assert payload["repeating"] is False
    assert payload["expires_at"] is None


def test_the_term_starts_when_checking_starts_not_when_planning_did(aws):
    """Set at confirm rather than at plan time -- a watch described on Monday
    and confirmed on Friday should get its full term."""
    watches = proposed(interval=60)
    watches["w_1"]["repeating"] = True
    env = aws(watches, one_target())
    call("POST /watches/{id}/confirm", watch_id="w_1")

    stored = env.watches.updates[-1]["ExpressionAttributeValues"]
    assert stored[":x"] > stored[":t"]


def test_confirm_stores_the_answers_to_the_plan_cards_questions(aws):
    env = aws(watches=proposed(), targets=one_target())
    call("POST /watches/{id}/confirm",
         {"answers": {"seniority": ["junior"]}}, watch_id="w_1")

    stored = env.watches.updates[-1]["ExpressionAttributeValues"]
    assert stored[":a"] == {"seniority": ["junior"]}


def test_confirming_without_answering_still_works(aws):
    """Questions are a help, not a form to be got past."""
    env = aws(watches=proposed(), targets=one_target())
    response = call("POST /watches/{id}/confirm", watch_id="w_1")

    assert response["statusCode"] == 200
    assert env.watches.updates[-1]["ExpressionAttributeValues"][":a"] == {}


def test_malformed_answers_are_refused_rather_than_stored(aws):
    aws(watches=proposed(), targets=one_target())
    response = call("POST /watches/{id}/confirm",
                    {"answers": "junior"}, watch_id="w_1")

    assert response["statusCode"] == 400


# --------------------------------------------------------------------------
# The baseline, re-derived once the answers say which offer this is about
#
# The order of events creates this and the order is right: the Planner cannot
# know which offer is meant until it has fetched the shops and asked about what
# it found, and the answers only arrive at confirm. So the baseline it stored
# is the cheapest thing any shop listed for the search -- measured on real
# pages, a ILS 139 headset for "xbox series x" -- and the threshold hanging off
# it is 10% below an object nobody is watching.
# --------------------------------------------------------------------------

def a_product_watch(*, pct=-10, baseline=139.0, threshold=125.1, op="<",
                    currency="ILS"):
    return {"w_1": {
        "watch_id": "w_1", "status": "proposed",
        "check_interval_min": Decimal(360),
        "condition": {"metric": "price", "op": op,
                      "value": Decimal(str(threshold)),
                      "currency": currency,
                      "baseline": Decimal(str(baseline)),
                      "baseline_source": "live",
                      "relative_change_pct": Decimal(pct),
                      "across": "best"},
        "questions": [{
            "id": "what",
            "question": "Which one did you mean?",
            "options": [
                {"value": "console", "label": "The console", "items": ["c1"]},
                {"value": "bits", "label": "Games and accessories",
                 "items": ["h1", "g1"]},
            ],
        }],
    }}


def shop_targets():
    return {
        "t_1": {"target_id": "t_1", "watch_id": "w_1", "currency": "ILS",
                "shop": "ivory", "verified_items": [
                    {"id": "h1", "text": "a headset", "price": Decimal("139")},
                    {"id": "c1", "text": "the console", "price": Decimal("1899")},
                ]},
        "t_2": {"target_id": "t_2", "watch_id": "w_1", "currency": "ILS",
                "shop": "bug", "verified_items": [
                    {"id": "g1", "text": "a game", "price": Decimal("29")},
                ]},
    }


def stored_condition(env):
    return env.watches.updates[-1]["ExpressionAttributeValues"][":cond"]


def test_pinning_the_product_moves_the_baseline_onto_it(aws):
    """10% off ILS 139 is a threshold about a headset. The watch is about a
    console."""
    env = aws(watches=a_product_watch(), targets=shop_targets())
    call("POST /watches/{id}/confirm",
         {"answers": {"what": ["console"]}}, watch_id="w_1")

    condition = stored_condition(env)
    assert float(condition["baseline"]) == 1899.0
    assert float(condition["value"]) == 1709.1


def test_the_baseline_is_the_best_pinned_offer_across_every_shop(aws):
    """Two shops both carry the console; "10% cheaper" means cheaper than the
    price you would actually pay today."""
    targets = shop_targets()
    targets["t_2"]["verified_items"].append(
        {"id": "c1", "text": "the console", "price": Decimal("1750")})
    env = aws(watches=a_product_watch(), targets=targets)
    call("POST /watches/{id}/confirm",
         {"answers": {"what": ["console"]}}, watch_id="w_1")

    assert float(stored_condition(env)["baseline"]) == 1750.0


def test_a_shop_in_another_currency_cannot_set_the_baseline(aws):
    """$34.99 is the smallest number on the table and is not a shekel."""
    targets = shop_targets()
    targets["t_2"]["currency"] = "USD"
    targets["t_2"]["verified_items"] = [
        {"id": "c1", "text": "the console", "price": Decimal("649")}]
    env = aws(watches=a_product_watch(), targets=targets)
    call("POST /watches/{id}/confirm",
         {"answers": {"what": ["console"]}}, watch_id="w_1")

    assert float(stored_condition(env)["baseline"]) == 1899.0


def test_a_rising_watch_repins_to_the_dearest_pinned_offer(aws):
    targets = shop_targets()
    targets["t_2"]["verified_items"].append(
        {"id": "c1", "text": "the console", "price": Decimal("1750")})
    env = aws(watches=a_product_watch(op=">", pct=10, baseline=139.0,
                                      threshold=152.9),
              targets=targets)
    call("POST /watches/{id}/confirm",
         {"answers": {"what": ["console"]}}, watch_id="w_1")

    condition = stored_condition(env)
    assert float(condition["baseline"]) == 1899.0
    assert float(condition["value"]) == 2088.9


def test_answering_nothing_leaves_the_threshold_exactly_as_planned(aws):
    """No pin, no new information. The behaviour that shipped before this."""
    env = aws(watches=a_product_watch(), targets=shop_targets())
    call("POST /watches/{id}/confirm", watch_id="w_1")

    condition = stored_condition(env)
    assert float(condition["baseline"]) == 139.0
    assert float(condition["value"]) == 125.1


def test_an_absolute_threshold_is_never_repinned(aws):
    """"Under 2000 shekels" is 2000 shekels whichever offer is meant."""
    watches = a_product_watch()
    del watches["w_1"]["condition"]["relative_change_pct"]
    del watches["w_1"]["condition"]["baseline"]
    watches["w_1"]["condition"]["value"] = Decimal("2000")
    env = aws(watches=watches, targets=shop_targets())
    call("POST /watches/{id}/confirm",
         {"answers": {"what": ["console"]}}, watch_id="w_1")

    assert float(stored_condition(env)["value"]) == 2000.0


def test_pinning_to_offers_with_no_price_changes_nothing(aws):
    """Never widens the watch and never raises: anything unexpected leaves the
    condition exactly as the Planner wrote it."""
    targets = shop_targets()
    for target in targets.values():
        for item in target["verified_items"]:
            item.pop("price", None)
    env = aws(watches=a_product_watch(), targets=targets)
    response = call("POST /watches/{id}/confirm",
                    {"answers": {"what": ["console"]}}, watch_id="w_1")

    assert response["statusCode"] == 200
    assert float(stored_condition(env)["baseline"]) == 139.0


def test_a_watch_with_no_condition_at_all_still_confirms(aws):
    env = aws(watches=proposed(), targets=one_target())
    response = call("POST /watches/{id}/confirm", watch_id="w_1")

    assert response["statusCode"] == 200
    assert stored_condition(env) == {}


# --------------------------------------------------------------------------
# Confirming a watch whose trigger is the clock
#
# Almost nothing the ordinary path does applies: no interval to snap, no
# window to fit, no targets to pin, and no budget gate -- a watch that fires
# exactly once cannot exceed a monthly allowance however the arithmetic is
# arranged, so running the gate would be theatre.
# --------------------------------------------------------------------------

def a_reminder(**extra):
    row = {"watch_id": "w_1", "status": "proposed",
           "fire_at": "2026-08-06T21:00:00+03:00",
           "fire_timezone": "Asia/Jerusalem",
           "reminder_title": "Call the dentist"}
    row.update(extra)
    return {"w_1": row}


def test_a_reminder_confirms_with_no_targets_at_all(aws):
    """The 409 that used to guard this was right for everything that polls
    and wrong for the one kind that does not."""
    env = aws(watches=a_reminder(), targets={})
    response = call("POST /watches/{id}/confirm", watch_id="w_1")

    assert response["statusCode"] == 200
    assert body_of(response)["status"] == "active"
    assert body_of(response)["targets_scheduled"] == 0


def test_the_schedule_is_addressed_to_the_watch_not_a_target(aws):
    env = aws(watches=a_reminder(), targets={})
    call("POST /watches/{id}/confirm", watch_id="w_1")

    args = env.scheduler.create_schedule.call_args.kwargs
    assert args["Name"] == "schedule-ai-app-w_1"
    assert json.loads(args["Target"]["Input"]) == {"watch_id": "w_1"}


def test_the_schedule_fires_once_and_deletes_itself(aws):
    env = aws(watches=a_reminder(), targets={})
    call("POST /watches/{id}/confirm", watch_id="w_1")

    args = env.scheduler.create_schedule.call_args.kwargs
    assert args["ScheduleExpression"] == "at(2026-08-06T21:00:00)"
    assert args["ActionAfterCompletion"] == "DELETE"
    assert args["ScheduleExpressionTimezone"] == "Asia/Jerusalem"


def test_confirming_a_reminder_says_exactly_when_it_will_fire(aws):
    """The same field the ordinary path returns, meaning the same thing --
    just exact rather than estimated, which is the one place in this product
    where it is."""
    aws(watches=a_reminder(), targets={})
    response = call("POST /watches/{id}/confirm", watch_id="w_1")

    assert body_of(response)["next_check_at"] == "2026-08-06T21:00:00+03:00"


def test_a_reminder_costs_nothing_and_says_so(aws):
    """Omitting the field would leave the client guessing whether it was
    missing or the answer was nothing."""
    aws(watches=a_reminder(), targets={})
    response = call("POST /watches/{id}/confirm", watch_id="w_1")

    assert body_of(response)["cost"]["estimated_monthly_usd"] == 0.0


def test_a_reminder_gets_no_expiry(aws):
    """It already has one: the moment it fires."""
    aws(watches=a_reminder(), targets={})
    assert body_of(call("POST /watches/{id}/confirm",
                        watch_id="w_1"))["expires_at"] is None


def test_an_ordinary_watch_still_needs_targets(aws):
    aws(watches=proposed(), targets={})
    assert call("POST /watches/{id}/confirm", watch_id="w_1")["statusCode"] == 409


def test_confirming_a_reminder_twice_conflicts_like_anything_else(aws):
    aws(watches=a_reminder(status="active"), targets={})
    assert call("POST /watches/{id}/confirm", watch_id="w_1")["statusCode"] == 409


def test_answering_daily_creates_a_repeating_schedule(aws):
    """"Set a reminder for 9pm to learn English, every day" -- the shape
    `at(...)` cannot express."""
    env = aws(watches=a_reminder(), targets={})
    response = call("POST /watches/{id}/confirm",
                    {"answers": {"repeat": ["daily"]}}, watch_id="w_1")

    args = env.scheduler.create_schedule.call_args.kwargs
    assert args["ScheduleExpression"] == "cron(0 21 * * ? *)"
    assert args["ActionAfterCompletion"] == "NONE"
    assert body_of(response)["repeating"] is True


def test_a_repeating_reminder_gets_a_term(aws):
    """It is the second thing here that does not stop by itself."""
    aws(watches=a_reminder(), targets={})
    response = call("POST /watches/{id}/confirm",
                    {"answers": {"repeat": ["daily"]}}, watch_id="w_1")

    assert body_of(response)["expires_at"] is not None


def test_confirming_without_answering_keeps_it_a_one_off(aws):
    """The safe direction: a reminder that came once is easier to fix than one
    arriving every evening that was never wanted."""
    env = aws(watches=a_reminder(), targets={})
    response = call("POST /watches/{id}/confirm", watch_id="w_1")

    assert body_of(response)["repeat"] == "once"
    assert env.scheduler.create_schedule.call_args.kwargs[
        "ActionAfterCompletion"] == "DELETE"


def test_an_explicitly_daily_plan_needs_no_answer(aws):
    """The question is only asked when the request left it open."""
    env = aws(watches=a_reminder(repeat="daily"), targets={})
    response = call("POST /watches/{id}/confirm", watch_id="w_1")

    assert body_of(response)["repeat"] == "daily"
    assert env.scheduler.create_schedule.call_args.kwargs[
        "ScheduleExpression"] == "cron(0 21 * * ? *)"


def test_the_answer_beats_the_plan(aws):
    """It is newer, and it is the user's."""
    aws(watches=a_reminder(repeat="daily"), targets={})
    response = call("POST /watches/{id}/confirm",
                    {"answers": {"repeat": ["once"]}}, watch_id="w_1")

    assert body_of(response)["repeat"] == "once"


def test_a_weekly_answer_keeps_the_day_of_the_week(aws):
    env = aws(watches=a_reminder(), targets={})
    call("POST /watches/{id}/confirm",
         {"answers": {"repeat": ["weekly"]}}, watch_id="w_1")

    # 2026-08-06 is a Thursday.
    assert env.scheduler.create_schedule.call_args.kwargs[
        "ScheduleExpression"] == "cron(0 21 ? * THU *)"


def test_a_nonsense_answer_falls_back_rather_than_crashing(aws):
    aws(watches=a_reminder(), targets={})
    response = call("POST /watches/{id}/confirm",
                    {"answers": {"repeat": ["forever"]}}, watch_id="w_1")

    assert response["statusCode"] == 200
    assert body_of(response)["repeat"] == "once"


# --------------------------------------------------------------------------
# Editing a reminder
#
# The first thing in this product that can be changed after it exists. Every
# other watch is defined by what it reads, so "change it" means re-planning;
# a reminder is defined by a moment and a sentence, and both are things a
# person mistypes. Decided 2026-08-07.
#
# The moment is computed, never written down -- see the twelve reminder tests
# that went red on 2026-08-07 because a literal date walked into the past.
# --------------------------------------------------------------------------

def in_days(days=2, hour=9):
    """A wall-clock reading safely in the future, in every zone these use."""
    when = datetime.now(timezone.utc) + timedelta(days=days)
    return when.strftime(f"%Y-%m-%dT{hour:02d}:00:00")


def live_reminder(**extra):
    row = {"watch_id": "w_1", "status": "active",
           "fire_at": in_days(1), "fire_timezone": "Asia/Jerusalem",
           "reminder_title": "Call the dentist", "repeat": "once"}
    row.update(extra)
    return {"w_1": row}


def sole_update(env):
    """The one UpdateExpression the request produced, as a string."""
    assert len(env.watches.updates) == 1
    return env.watches.updates[0]


def test_moving_a_reminder_replaces_its_schedule(aws):
    env = aws(watches=live_reminder(), targets={})
    when = in_days(3, hour=20)

    response = call("PATCH /watches/{id}", {"fire_at": when}, watch_id="w_1")

    assert response["statusCode"] == 200
    args = env.scheduler.create_schedule.call_args.kwargs
    # Same name as the original, so this replaces rather than adds a second
    # schedule that would fire at the old time forever.
    assert args["Name"] == "schedule-ai-app-w_1"
    assert args["ScheduleExpression"] == f"at({when})"
    assert args["ScheduleExpressionTimezone"] == "Asia/Jerusalem"


def test_a_time_in_the_past_is_refused_with_a_sentence(aws):
    """The same guard the Planner applies to the model's answer, applied to
    the user's. EventBridge would reject it as a 500, and a guardrail that
    returns 500 is an outage."""
    aws(watches=live_reminder(), targets={})
    response = call("PATCH /watches/{id}",
                    {"fire_at": "2020-01-01T09:00:00"}, watch_id="w_1")

    assert response["statusCode"] == 400
    assert "already passed" in body_of(response)["error"]


def test_an_unreadable_time_is_refused_before_any_schedule_is_touched(aws):
    env = aws(watches=live_reminder(), targets={})
    response = call("PATCH /watches/{id}", {"fire_at": "soonish"},
                    watch_id="w_1")

    assert response["statusCode"] == 400
    assert env.scheduler.create_schedule.call_count == 0
    assert env.watches.updates == []


def test_switching_to_daily_swaps_at_for_cron_and_starts_a_term(aws):
    """`at(...)` carries ActionAfterCompletion DELETE. Left in place, a
    reminder switched to daily would delete itself after firing once."""
    env = aws(watches=live_reminder(), targets={})
    response = call("PATCH /watches/{id}", {"repeat": "daily"},
                    watch_id="w_1")

    assert response["statusCode"] == 200
    args = env.scheduler.create_schedule.call_args.kwargs
    assert args["ScheduleExpression"].startswith("cron(")
    assert args["ActionAfterCompletion"] == "NONE"
    assert sole_update(env)["ExpressionAttributeValues"][":x"] is not None


def test_switching_back_to_once_restores_the_self_deleting_schedule(aws):
    env = aws(watches=live_reminder(repeat="daily"), targets={})
    call("PATCH /watches/{id}", {"repeat": "once"}, watch_id="w_1")

    args = env.scheduler.create_schedule.call_args.kwargs
    assert args["ScheduleExpression"].startswith("at(")
    assert args["ActionAfterCompletion"] == "DELETE"
    # And the 90-day term goes with it: a one-off already has an end.
    assert sole_update(env)["ExpressionAttributeValues"][":x"] is None


def test_editing_only_the_note_touches_no_schedule(aws):
    """Nothing about when it fires changed, so nothing about the schedule
    should be rewritten."""
    env = aws(watches=live_reminder(), targets={})
    response = call("PATCH /watches/{id}",
                    {"reminder_note": "  bring   the X-ray  "}, watch_id="w_1")

    assert response["statusCode"] == 200
    assert env.scheduler.create_schedule.call_count == 0
    assert sole_update(env)["ExpressionAttributeValues"][":rn"] == "bring the X-ray"


def test_a_condition_watch_cannot_be_edited_this_way(aws):
    """It is defined by what it reads, and none of these fields mean anything
    to it. Saying so beats writing fire_at onto a price watch."""
    aws(watches={"w_1": {"watch_id": "w_1", "status": "active"}},
        targets=one_target())
    response = call("PATCH /watches/{id}", {"fire_at": in_days()},
                    watch_id="w_1")

    assert response["statusCode"] == 400
    assert "only a reminder" in body_of(response)["error"]


def test_an_unknown_repeat_is_refused(aws):
    aws(watches=live_reminder(), targets={})
    assert call("PATCH /watches/{id}", {"repeat": "fortnightly"},
                watch_id="w_1")["statusCode"] == 400


def test_a_fired_reminder_re_arms_when_given_a_new_time(aws):
    """`at(...)` deleted its own schedule on the way out, so this creates a
    fresh one and brings the watch back to active. Decided 2026-08-07:
    re-describing it from scratch would lose the note and the history."""
    env = aws(watches=live_reminder(status="triggered", trigger_count=1),
              targets={})
    response = call("PATCH /watches/{id}", {"fire_at": in_days(4)},
                    watch_id="w_1")

    assert response["statusCode"] == 200
    assert env.scheduler.create_schedule.call_count == 1
    assert sole_update(env)["ExpressionAttributeValues"][":s"] == "active"


def test_a_fired_reminder_cannot_be_edited_without_a_new_time(aws):
    """Changing the note of something that already happened does nothing a
    user would want, and silently leaving it `triggered` would read as a
    reminder that was quietly set going again."""
    env = aws(watches=live_reminder(status="triggered"), targets={})
    response = call("PATCH /watches/{id}", {"reminder_note": "later"},
                    watch_id="w_1")

    assert response["statusCode"] == 409
    assert "already fired" in body_of(response)["error"]
    assert env.watches.updates == []


def test_re_arming_and_pausing_cannot_collide(aws):
    """Both write `status`, and two `#s = :s` clauses in one UpdateExpression
    is a ValidationException -- the same shape as the `repeat` reserved-word
    bug of 2026-08-05. They cannot meet: re-arming needs a terminal watch and
    pausing needs an active one. This test is what keeps that true, because
    the guard in `patch_watch` is otherwise unreachable and would be deleted
    by the next person tidying up.
    """
    env = aws(watches=live_reminder(status="triggered"), targets={})
    response = call("PATCH /watches/{id}",
                    {"fire_at": in_days(5), "status": "paused"},
                    watch_id="w_1")

    assert response["statusCode"] == 409
    assert env.watches.updates == []


def test_pausing_a_reminder_leaves_its_schedule_alone(aws):
    """A paused reminder keeps its EventBridge schedule and the Checker
    declines to fire it -- `_fire_on_time` returns early on any status that is
    not active. Deleting the schedule here would make resuming it silent."""
    env = aws(watches=live_reminder(), targets={})
    response = call("PATCH /watches/{id}", {"status": "paused"},
                    watch_id="w_1")

    assert response["statusCode"] == 200
    assert env.scheduler.delete_schedule.call_count == 0


def test_a_reminders_next_check_is_its_own_firing_time(aws):
    """Derived from `check_interval_min` it would read the 1440 placeholder
    that exists only because the cost model wants a number, and answer "in 24
    hours" for a reminder due in ten minutes."""
    when = in_days(1, hour=21)
    aws(watches=live_reminder(fire_at=when), targets={})
    response = call("GET /watches/{id}", watch_id="w_1")

    assert body_of(response)["next_check_at"] == when


# --------------------------------------------------------------------------
# Who is asking
#
# API Gateway has already verified the signature, issuer, audience and expiry
# against Cognito's published keys before this Lambda runs. These claims are
# not input to be validated -- they are the result of a validation that
# happened outside our code, which is why the 76-line passcode authorizer was
# deleted rather than improved.
# --------------------------------------------------------------------------

def signed_in(sub="sub-abc", email="owner@example.com"):
    return {"requestContext": {"authorizer": {"jwt": {"claims": {
        "sub": sub, "email": email}}}}}


def call_as(route, claims, body=None, watch_id=None):
    event = {"routeKey": route, **claims}
    if body is not None:
        event["body"] = json.dumps(body)
    if watch_id is not None:
        event["pathParameters"] = {"id": watch_id}
    return handler.lambda_handler(event, None)


def test_a_watch_belongs_to_the_person_who_asked_for_it(aws):
    env = aws()
    call_as("POST /watches", signed_in(), {"request": "watch the steam deck"})

    assert env.watches.puts[0]["user_id"] == "sub-abc"


def test_identity_is_the_subject_not_the_address(aws):
    """An address can be changed and can be reassigned; `sub` is stable for the
    life of the account. Keying on email loses someone's watches when they
    change it, to whoever takes the address next."""
    env = aws()
    call_as("POST /watches", signed_in(sub="sub-abc", email="new@example.com"),
            {"request": "watch it"})

    assert env.watches.puts[0]["user_id"] == "sub-abc"


def test_the_address_to_notify_is_stored_on_the_watch(aws):
    """Not read from an environment variable at send time. A watch should
    reach whoever asked for it, and one NOTIFY_EMAIL is the last place
    multi-user is still assumed."""
    env = aws()
    call_as("POST /watches", signed_in(email="owner@example.com"),
            {"request": "watch it"})

    assert env.watches.puts[0]["notify_email"] == "owner@example.com"


def test_a_request_with_no_token_still_works_locally(aws):
    """Not a bypass: without a token API Gateway never routes the request
    here. This is what keeps a locally-invoked Lambda, and every row created
    before sign-in existed, working."""
    env = aws()
    call("POST /watches", {"request": "watch it"})

    assert env.watches.puts[0]["user_id"] == "default"
    assert "notify_email" not in env.watches.puts[0]


def test_a_token_without_an_email_claim_stores_no_address(aws):
    env = aws()
    call_as("POST /watches",
            {"requestContext": {"authorizer": {"jwt": {"claims":
                                                       {"sub": "sub-abc"}}}}},
            {"request": "watch it"})

    assert env.watches.puts[0]["user_id"] == "sub-abc"
    assert "notify_email" not in env.watches.puts[0]


@pytest.mark.parametrize("context", [
    {},
    {"requestContext": {}},
    {"requestContext": {"authorizer": {}}},
    {"requestContext": {"authorizer": {"jwt": {}}}},
    {"requestContext": {"authorizer": None}},
])
def test_every_shape_of_missing_claim_falls_back_rather_than_crashing(aws, context):
    env = aws()
    call_as("POST /watches", context, {"request": "watch it"})

    assert env.watches.puts[0]["user_id"] == "default"
