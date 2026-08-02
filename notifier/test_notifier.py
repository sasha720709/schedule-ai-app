"""Tests for the Notifier.

This Lambda is the last step of the entire product -- it is what turns a
satisfied condition into something a human actually sees -- and until now it
had no tests at all. It is also the hardest one to verify by hand, because
doing so sends real email and destroys real schedules.

The payloads below are copied from what `checker/handler.py` actually puts on
the bus, not invented. Two fields it can legitimately send as `None` (`note`
and `last_value`) are covered on purpose: an f-string interpolating None
produces "None" in an email to a person, and the handler's `or` fallbacks are
the only thing preventing that.
"""

import importlib.util
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))

# `handler.py` builds its boto3 clients at module scope, which is right for a
# Lambda -- they get reused across warm invocations -- and impossible in a test
# process with no credentials and no region. So boto3 is replaced before the
# module is executed.
#
# Replaced unconditionally rather than only when absent. A guard would make the
# result depend on which suite pytest happened to collect first, and on whether
# the machine has a real boto3 with a usable region -- which a CI runner does
# not. Every suite in this repo must pass alone and in any order.
_boto3 = types.ModuleType("boto3")
_boto3.client = MagicMock()
_boto3.resource = MagicMock()
sys.modules["boto3"] = _boto3

_conditions = types.ModuleType("boto3.dynamodb.conditions")


class Key:
    """Just enough of boto3's Key to record what a query asked for."""

    def __init__(self, name):
        self.name = name

    def eq(self, value):
        return ("eq", self.name, value)


_conditions.Key = Key
sys.modules["boto3.dynamodb"] = types.ModuleType("boto3.dynamodb")
sys.modules["boto3.dynamodb.conditions"] = _conditions

os.environ.setdefault("WATCH_TARGETS_TABLE", "targets")
os.environ.setdefault("NOTIFY_EMAIL", "owner@example.com")

# Loaded by path under a unique name, the same way checker/ and planner/ do it.
# Three Lambdas in this repo have a module called `handler`, and a plain
# `import handler` would hand whichever one pytest imported first to all of
# them -- silently, with the tests still passing against the wrong code.
_spec = importlib.util.spec_from_file_location(
    "notifier_handler", os.path.join(_HERE, "handler.py"))
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


class FakeTargets:
    """A targets table that can hand back more than one page.

    Pagination is the point. A real `query()` caps at 1MB and signals more
    with `LastEvaluatedKey`; the bug this replaces read only the first page,
    so the surplus schedules were never deleted and billed forever.
    """

    def __init__(self, pages):
        self.pages = pages
        self.queries = []
        self.updates = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        index = kwargs.get("ExclusiveStartKey", {}).get("page", 0)
        response = {"Items": self.pages[index]}
        if index + 1 < len(self.pages):
            response["LastEvaluatedKey"] = {"page": index + 1}
        return response

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        return {}


@pytest.fixture
def aws(monkeypatch):
    """Point the handler at fakes and record the order things happened in."""

    def build(pages=None, missing=(), email_fails=False):
        if pages is None:
            pages = [[{"target_id": "t_1", "watch_id": "w_1"}]]
        targets = FakeTargets(pages)

        dynamodb = MagicMock()
        dynamodb.Table.return_value = targets
        monkeypatch.setattr(handler, "dynamodb", dynamodb)

        # One shared log, so ordering between SES and Scheduler is assertable.
        # "Email first, delete second" is a stated guarantee of this module,
        # not an accident of line order.
        log = []

        class ResourceNotFoundException(Exception):
            pass

        scheduler = MagicMock()
        scheduler.exceptions.ResourceNotFoundException = ResourceNotFoundException

        def delete_schedule(Name):
            log.append(("delete_schedule", Name))
            if Name in missing:
                raise ResourceNotFoundException(Name)
            return {}

        scheduler.delete_schedule.side_effect = delete_schedule
        monkeypatch.setattr(handler, "scheduler", scheduler)

        ses = MagicMock()

        def send_email(**kwargs):
            log.append(("send_email", kwargs["Message"]["Subject"]["Data"]))
            if email_fails:
                raise RuntimeError("SES said no")
            return {"MessageId": "m_1"}

        ses.send_email.side_effect = send_email
        monkeypatch.setattr(handler, "ses", ses)

        return SimpleNamespace(targets=targets, scheduler=scheduler,
                               ses=ses, log=log, dynamodb=dynamodb)

    return build


def triggered(**overrides):
    """Exactly the shape checker/handler.py publishes on a match."""
    detail = {
        "watch_id": "w_1",
        "target_id": "t_1",
        "url": "https://store.example.com/deck",
        "prompt": "tell me when the Steam Deck drops below $450",
        "last_value": "$449.00",
        "note": "price fell below the threshold",
        "triggered_at": "2026-08-02T12:00:00Z",
    }
    detail.update(overrides)
    return {"detail-type": "WatchTriggered", "detail": detail}


def degraded(**overrides):
    """Exactly the shape checker/handler.py publishes when it gives up."""
    detail = {
        "watch_id": "w_1",
        "target_id": "t_1",
        "url": "https://store.example.com/deck",
        "prompt": "tell me when the Steam Deck drops below $450",
        "reason": "css selector matched nothing",
        "repair_spend_usd": 0.0081,
        "degraded_at": "2026-08-02T12:00:00Z",
    }
    detail.update(overrides)
    return {"detail-type": "WatchDegraded", "detail": detail}


def sent(env):
    """The one email that was sent, as (subject, body)."""
    call = env.ses.send_email.call_args.kwargs
    return (call["Message"]["Subject"]["Data"],
            call["Message"]["Body"]["Text"]["Data"])


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------

def test_a_triggered_watch_emails_the_owner(aws):
    env = aws()
    handler.lambda_handler(triggered(), None)

    call = env.ses.send_email.call_args.kwargs
    assert call["Source"] == "owner@example.com"
    assert call["Destination"]["ToAddresses"] == ["owner@example.com"]


def test_the_email_carries_the_value_the_page_and_the_reason(aws):
    env = aws()
    handler.lambda_handler(triggered(), None)
    subject, body = sent(env)

    assert "Steam Deck drops below $450" in subject
    assert "$449.00" in body
    assert "https://store.example.com/deck" in body
    assert "price fell below the threshold" in body


def test_the_summary_names_what_was_torn_down(aws):
    env = aws()
    result = handler.lambda_handler(triggered(), None)

    assert result == {"notified": True, "watch_id": "w_1",
                      "schedules_deleted": ["schedule-ai-app-t_1"]}


def test_only_this_watch_s_targets_are_touched(aws):
    env = aws()
    handler.lambda_handler(triggered(), None)

    query = env.targets.queries[0]
    assert query["IndexName"] == "watch_id-index"
    assert query["KeyConditionExpression"] == ("eq", "watch_id", "w_1")


# --------------------------------------------------------------------------
# Ordering: the email is the point, the teardown is the cleanup
# --------------------------------------------------------------------------

def test_the_email_goes_before_any_schedule_is_deleted(aws):
    env = aws()
    handler.lambda_handler(triggered(), None)

    assert [step for step, _ in env.log] == ["send_email", "delete_schedule"]


def test_a_failed_email_leaves_every_schedule_alive(aws):
    """The stated trade: a duplicate check beats a match nobody hears about.

    If this inverts, a watch can fire, fail to reach the owner, and stop
    checking -- the one outcome with no recovery, since nothing will look
    at that page again.
    """
    env = aws(email_fails=True)

    with pytest.raises(RuntimeError):
        handler.lambda_handler(triggered(), None)

    env.scheduler.delete_schedule.assert_not_called()
    assert env.targets.updates == []


# --------------------------------------------------------------------------
# Pagination -- the bug that would have billed forever
# --------------------------------------------------------------------------

def test_every_page_of_targets_is_followed(aws):
    env = aws(pages=[
        [{"target_id": "t_1", "watch_id": "w_1"}],
        [{"target_id": "t_2", "watch_id": "w_1"}],
        [{"target_id": "t_3", "watch_id": "w_1"}],
    ])
    result = handler.lambda_handler(triggered(), None)

    assert result["schedules_deleted"] == [
        "schedule-ai-app-t_1", "schedule-ai-app-t_2", "schedule-ai-app-t_3",
    ]


def test_the_next_page_is_requested_from_where_the_last_one_stopped(aws):
    env = aws(pages=[
        [{"target_id": "t_1", "watch_id": "w_1"}],
        [{"target_id": "t_2", "watch_id": "w_1"}],
    ])
    handler.lambda_handler(triggered(), None)

    assert "ExclusiveStartKey" not in env.targets.queries[0]
    assert env.targets.queries[1]["ExclusiveStartKey"] == {"page": 1}


def test_a_final_page_that_is_empty_terminates(aws):
    """`LastEvaluatedKey` can be present on a page with no items -- DynamoDB
    paginates on scanned bytes, not returned rows. Stopping on an empty
    `Items` instead of on a missing key would lose the remaining targets."""
    env = aws(pages=[
        [{"target_id": "t_1", "watch_id": "w_1"}],
        [],
        [{"target_id": "t_2", "watch_id": "w_1"}],
    ])
    result = handler.lambda_handler(triggered(), None)

    assert result["schedules_deleted"] == [
        "schedule-ai-app-t_1", "schedule-ai-app-t_2",
    ]


# --------------------------------------------------------------------------
# The table must stop lying about schedules that no longer exist
# --------------------------------------------------------------------------

def test_the_schedule_arn_is_cleared_on_every_target(aws):
    env = aws(pages=[[
        {"target_id": "t_1", "watch_id": "w_1", "schedule_arn": "arn:1"},
        {"target_id": "t_2", "watch_id": "w_1", "schedule_arn": "arn:2"},
    ]])
    handler.lambda_handler(triggered(), None)

    assert [u["Key"] for u in env.targets.updates] == [
        {"target_id": "t_1"}, {"target_id": "t_2"},
    ]
    assert all(u["UpdateExpression"] == "REMOVE schedule_arn"
               for u in env.targets.updates)


def test_an_already_deleted_schedule_is_tolerated_and_still_cleared(aws):
    """Re-delivery is normal -- EventBridge retries, and a retried teardown
    finds the schedule gone. That is the state we wanted, not an error. The
    row must still be cleaned up, or a redelivery leaves the lie in place."""
    env = aws(missing=("schedule-ai-app-t_1",))
    result = handler.lambda_handler(triggered(), None)

    assert result["schedules_deleted"] == []
    assert env.targets.updates[0]["Key"] == {"target_id": "t_1"}


# --------------------------------------------------------------------------
# Degraded is a different message, not a variant of the same one
# --------------------------------------------------------------------------

def test_a_degraded_watch_does_not_claim_the_condition_came_true(aws):
    env = aws()
    handler.lambda_handler(degraded(), None)
    subject, body = sent(env)

    assert "stopped working" in subject
    assert "came true" not in body
    assert "css selector matched nothing" in body


def test_the_degraded_email_says_what_the_failed_repair_cost(aws):
    env = aws()
    handler.lambda_handler(degraded(), None)
    _, body = sent(env)

    assert "$0.008" in body


def test_a_degraded_watch_also_stops_checking(aws):
    """Degrading deletes schedules for the same reason triggering does:
    continuing to check something known to be broken bills every tick to
    re-learn a settled fact."""
    env = aws()
    result = handler.lambda_handler(degraded(), None)

    assert result["schedules_deleted"] == ["schedule-ai-app-t_1"]


def test_an_absent_detail_type_is_treated_as_a_trigger(aws):
    """Only WatchDegraded takes the degraded path. Anything else -- including
    a hand-crafted test invoke with no detail-type -- gets the normal one."""
    env = aws()
    event = triggered()
    del event["detail-type"]
    handler.lambda_handler(event, None)

    assert "came true" in sent(env)[1]


# --------------------------------------------------------------------------
# Fields the Checker can legitimately send as None or omit
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["last_value", "note"])
def test_a_null_field_never_reaches_the_reader_as_the_word_none(aws, field):
    """`result["note"]` and `result["raw"]`/`["value"]` can both be None, and
    an f-string turns None into the literal text "None" in a human's inbox."""
    env = aws()
    handler.lambda_handler(triggered(**{field: None}), None)
    _, body = sent(env)

    assert "None" not in body
    assert "(no" in body


def test_a_missing_url_does_not_crash_the_notification(aws):
    env = aws()
    event = triggered()
    del event["detail"]["url"]
    handler.lambda_handler(event, None)

    assert "(unknown)" in sent(env)[1]


def test_an_overlong_prompt_is_cut_out_of_the_subject_but_not_the_body(aws):
    env = aws()
    prompt = "watch this extremely specific thing " * 10
    handler.lambda_handler(triggered(prompt=prompt), None)
    subject, body = sent(env)

    assert len(subject) <= len("Watch triggered: ") + 60
    assert prompt.strip() in body
