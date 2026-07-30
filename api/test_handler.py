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


def proposed(interval=10):
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
    assert payload["check_interval_min"] == 10
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
    call("PATCH /watches/{id}", {"check_interval_min": 30}, watch_id="w_1")

    assert env.scheduler.create_schedule.call_count == 1


def test_changing_the_interval_of_a_paused_watch_touches_no_schedule(aws):
    env = aws(watches={"w_1": {"watch_id": "w_1", "status": "paused"}},
              targets=one_target())
    call("PATCH /watches/{id}", {"check_interval_min": 30}, watch_id="w_1")

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

@pytest.mark.parametrize("minutes,expected", [
    (1, "rate(1 minute)"),
    (10, "rate(10 minutes)"),
    (60, "rate(60 minutes)"),
])
def test_rate_expression_gets_the_plural_right(minutes, expected):
    assert handler._rate_expression(minutes) == expected


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
