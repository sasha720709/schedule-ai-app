"""API Lambda: every watch lifecycle operation, behind one function.

One Lambda with internal routing rather than one per route -- fewer cold
starts, one IAM role, and far less Terraform for six endpoints that all
touch the same two tables.

This is also where schedules are created and destroyed. The Planner used
to do it as part of planning; since Phase 4 it proposes a plan and stops,
and committing that plan is an explicit API call. That keeps the cost of a
watch visible before it starts, and keeps the slow Sonnet path free of
external side effects it could half-complete.

    POST   /watches              202, kicks off planning
    GET    /watches              list
    GET    /watches/{id}         detail, including targets
    POST   /watches/{id}/confirm create schedules, -> active
    PATCH  /watches/{id}         pause / resume / change interval
    DELETE /watches/{id}         delete rows AND schedules

CORS is configured on the API Gateway itself, so no headers are set here.
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

import cost
import schedules

dynamodb = boto3.resource("dynamodb")
scheduler = boto3.client("scheduler")
lambda_client = boto3.client("lambda")

# Every row is written with this until real auth exists. Kept as a field
# rather than dropped, so multi-user is a query change and not a migration.
USER_ID = "default"

# Statuses a watch can be in. planning -> proposed -> active -> triggered,
# with failed as a dead end off planning and paused toggling with active.
CONFIRMABLE = ("proposed",)

# How long a repeating watch runs before stopping itself.
#
# Every other watch reaches a terminal state on its own -- triggered, degraded
# -- and stops billing. A repeating one would run until somebody remembered it,
# which is the only unbounded cost this system has ever had. Ninety days is
# about the length of a job search; the point is that the number exists, not
# that it is exactly right. One-shot watches get no expiry at all, because they
# already have one.
REPEATING_TERM_DAYS = 90
PAUSABLE = ("active",)
RESUMABLE = ("paused",)


class HttpError(Exception):
    """Carries an HTTP status so the router can turn it into a response."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _json_default(value):
    """DynamoDB hands every number back as Decimal, which json can't encode."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _to_decimal(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_decimal(v) for v in value]
    return value


def _response(status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def _watches():
    return dynamodb.Table(os.environ["WATCHES_TABLE"])


def _targets():
    return dynamodb.Table(os.environ["WATCH_TARGETS_TABLE"])


def _body(event) -> dict:
    raw = event.get("body")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HttpError(400, f"body is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HttpError(400, "body must be a JSON object")
    return parsed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_watch(watch_id: str) -> dict:
    watch = _watches().get_item(Key={"watch_id": watch_id}).get("Item")
    if watch is None:
        raise HttpError(404, f"no such watch: {watch_id}")
    return watch


def _targets_for(watch_id: str) -> list:
    """Every target of a watch, paginated.

    query() returns at most 1MB per call. The Notifier still has this bug
    -- a watch with enough targets would silently keep some schedules
    alive forever -- so new code does not repeat it.
    """
    items, start_key = [], None
    while True:
        kwargs = {
            "IndexName": "watch_id-index",
            "KeyConditionExpression": Key("watch_id").eq(watch_id),
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        page = _targets().query(**kwargs)
        items.extend(page.get("Items", []))
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            return items


def _schedule_name(target_id: str) -> str:
    return f"schedule-ai-app-{target_id}"


def _upsert_schedule(target_id: str, interval_min: int,
                     window: str | None = None) -> str:
    """Create the schedule, or retune an existing one to a new interval.

    The name is derived from target_id rather than generated, which makes
    this safe to retry: a confirm that failed halfway can be called again
    and will update what it already created instead of duplicating it.
    """
    args = {
        "Name": _schedule_name(target_id),
        # rate(...) normally; cron(...) plus a timezone for a windowed target,
        # so that a market watch simply does not fire when the market is shut.
        # Building the expression here is what kept the Checker from ever
        # needing to know what a stock market is.
        **schedules.expression(interval_min, window),
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {
            "Arn": os.environ["CHECKER_FUNCTION_ARN"],
            "RoleArn": os.environ["SCHEDULER_ROLE_ARN"],
            "Input": json.dumps({"target_id": target_id}),
        },
    }
    try:
        return scheduler.create_schedule(**args)["ScheduleArn"]
    except scheduler.exceptions.ConflictException:
        return scheduler.update_schedule(**args)["ScheduleArn"]


def _delete_schedules(targets: list) -> list:
    deleted = []
    for target in targets:
        name = _schedule_name(target["target_id"])
        try:
            scheduler.delete_schedule(Name=name)
            deleted.append(name)
        except scheduler.exceptions.ResourceNotFoundException:
            pass  # already gone -- that is the state we wanted
    return deleted


# --------------------------------------------------------------------------
# Route handlers
# --------------------------------------------------------------------------

def create_watch(event) -> dict:
    request = (_body(event).get("request") or "").strip()
    if not request:
        raise HttpError(400, "request is required")
    if len(request) > 2000:
        raise HttpError(400, "request must be 2000 characters or fewer")

    watch_id = f"w_{uuid.uuid4().hex[:8]}"
    _watches().put_item(Item={
        "watch_id": watch_id,
        "user_id": USER_ID,
        "prompt": request,
        "status": "planning",
        "created_at": _now(),
    })

    # Fire and forget. The Planner takes ~20s against API Gateway's 29s
    # ceiling, so this call must not be waited on; the client polls
    # GET /watches/{id} until the status leaves "planning".
    lambda_client.invoke(
        FunctionName=os.environ["PLANNER_FUNCTION_ARN"],
        InvocationType="Event",
        Payload=json.dumps({"watch_id": watch_id, "request": request}),
    )

    return _response(202, {"watch_id": watch_id, "status": "planning"})


def list_watches(event) -> dict:
    """All watches, newest first.

    A scan, because the table is keyed on watch_id and there is no GSI on
    user_id. Correct and cheap while this is single-user with a handful of
    rows; it needs a user_id index before a second person exists.
    """
    items, start_key = [], None
    while True:
        kwargs = {"ExclusiveStartKey": start_key} if start_key else {}
        page = _watches().scan(**kwargs)
        items.extend(page.get("Items", []))
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            break

    items.sort(key=lambda w: w.get("created_at", ""), reverse=True)
    return _response(200, {"watches": items})


def _uses_model(targets: list) -> bool:
    """Will any tick of this watch pay for a language model?

    Since Phase 8b a target that carries a compiled `extractor` is read in pure
    Python, which is roughly a thousandth of the cost of asking Haiku. Pricing
    every watch as though it still called a model would overstate the bill by
    ~35x and make `confirm` refuse intervals the budget comfortably affords.

    Deliberately pessimistic: one target without an extractor still falls back
    to the model path, so the whole watch is priced as if it does. A cost
    estimate that is too low is the one that lets a surprise bill through.
    """
    return any("extractor" not in target for target in targets)


def _window_of(targets: list):
    """The window every target shares, or None if they disagree.

    Disagreement resolves to continuous on purpose. Overstating cost refuses
    an interval that was affordable, which the user can argue with;
    understating it creates a schedule that quietly bills more than the budget
    allowed, which nobody notices.
    """
    windows = {t.get("schedule_window") for t in targets}
    return windows.pop() if len(windows) == 1 else None


def _estimate_for(watch: dict, targets: list, interval=None) -> dict | None:
    """Cost of running this watch, for the plan card to show before confirming."""
    interval = interval if interval is not None else watch.get("check_interval_min")
    if interval is None or not targets:
        return None
    browser = any(t.get("fetch_method", "http") == "browser" for t in targets)
    return cost.estimate(
        interval_min=int(interval),
        targets=len(targets),
        fetch_method="browser" if browser else "http",
        uses_model=_uses_model(targets),
        window=_window_of(targets),
    )


def _next_check_at(interval_min, window) -> str | None:
    """When this cadence next runs, as an ISO-8601 UTC string.

    None whenever the answer would be a guess -- no interval yet, or a
    timezone database the runtime does not have. A missing answer displays as
    "unknown", which is honest; a wrong one is worse than silence.
    """
    if interval_min is None:
        return None
    fire = schedules.next_fire_after(datetime.now(timezone.utc),
                                     int(interval_min), window)
    return fire.isoformat().replace("+00:00", "Z") if fire else None


def _staleness_of(watch: dict, targets: list) -> list:
    """Has each target stopped moving, and does that mean anything?

    The Checker records two facts -- when the value last changed, and how many
    checks it has been the same. The judgement lives here because it depends on
    the current interval, which a PATCH can change long after those rows were
    written.

    **This never stops, degrades or pauses anything, and that is deliberate.**
    A value sitting still is the normal case for most watches: a shop price
    waiting weeks for a drop, a vacancy count that is zero until the day it is
    not. Acting on stillness would re-create the exact false positive the
    `unavailable` / `failed` split exists to prevent -- escalating a watch that
    is patiently doing its job. So this reports, and a human decides.

    It is only ever *notable* for a windowed target, because only a window
    defines what "should have moved by now" means. `checks_per_session` is one
    full trading day: a claim anyone can evaluate, rather than a constant.
    """
    interval = watch.get("check_interval_min")
    out = []
    for target in targets:
        unchanged = int(target.get("unchanged_checks") or 0)
        per_session = (
            schedules.checks_per_session(int(interval),
                                         target.get("schedule_window"))
            if interval else None
        )
        out.append({
            "target_id": target.get("target_id"),
            "last_changed_at": target.get("last_changed_at"),
            "unchanged_checks": unchanged,
            "checks_per_session": per_session,
            # A whole session without a single tick. For a quote that is a
            # frozen feed; there is no equivalent claim to make without a
            # window, so there is no flag either.
            "stale": bool(per_session and unchanged >= per_session),
        })
    return out


def _next_check_for(watch: dict, targets: list) -> str | None:
    """Only an `active` watch has schedules, so only an active watch has a next
    check. A paused or proposed one answering with a time would be describing a
    schedule that does not exist."""
    if watch.get("status") != "active":
        return None
    return _next_check_at(watch.get("check_interval_min"), _window_of(targets))


def get_watch(event) -> dict:
    watch_id = event["pathParameters"]["id"]
    watch = _get_watch(watch_id)
    targets = _targets_for(watch_id)
    return _response(200, {
        "watch": watch,
        "targets": targets,
        "cost": _estimate_for(watch, targets),
        "next_check_at": _next_check_for(watch, targets),
        "staleness": _staleness_of(watch, targets),
    })


def confirm_watch(event) -> dict:
    """Commit a proposed plan: create its schedules and go active."""
    watch_id = event["pathParameters"]["id"]
    watch = _get_watch(watch_id)

    status = watch.get("status")
    if status not in CONFIRMABLE:
        raise HttpError(409, f"watch {watch_id} is {status}, not proposed")

    # "field absent" and "field present but null" are deliberately different.
    # Silently substituting the Planner's interval for an explicit null would
    # mean a client that sent an empty input gets billed at a rate it never
    # chose -- which is the exact surprise this confirm step exists to stop.
    body = _body(event)
    interval = (body["check_interval_min"] if "check_interval_min" in body
                else watch.get("check_interval_min"))
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        raise HttpError(400, "check_interval_min must be an integer") from None
    if not 1 <= interval <= 1440:
        raise HttpError(400, "check_interval_min must be between 1 and 1440")

    # Answers to the plan card's questions, if any were asked and any given.
    # Deliberately optional: confirming without answering behaves exactly as it
    # did before this existed, which is what keeps the questions a help rather
    # than a form to be got past.
    answers = body.get("answers")
    if answers is not None and not isinstance(answers, dict):
        raise HttpError(400, "answers must be an object of question id -> choices")

    targets = _targets_for(watch_id)
    if not targets:
        raise HttpError(409, f"watch {watch_id} has no targets to schedule")

    # A windowed watch runs on a cron grid, which cannot express every number
    # of minutes. Snap before the estimate rather than after, so the cost that
    # is checked, the interval that is stored and the schedule that is created
    # all describe the same cadence. Snapping only ever lengthens the interval,
    # so this cannot sneak past the budget gate below.
    window = _window_of(targets)
    interval = schedules.snap(interval, window)

    # Refuse to start something whose running cost exceeds the budget. This is
    # the last gate before a schedule exists, and a schedule is the only thing
    # here that bills indefinitely -- the AWS budget alarms cannot see the
    # Anthropic spend that dominates it.
    browser = any(t.get("fetch_method", "http") == "browser" for t in targets)
    estimate = cost.estimate(
        interval_min=interval,
        targets=len(targets),
        fetch_method="browser" if browser else "http",
        uses_model=_uses_model(targets),
        window=window,
    )
    if not estimate["within_budget"]:
        raise HttpError(
            409,
            f"every {interval} min would cost about "
            f"${estimate['estimated_monthly_usd']:.2f}/month, over the "
            f"${estimate['monthly_budget_usd']:.2f} budget. "
            f"Use {estimate['min_interval_min']} min or longer.",
        )

    for target in targets:
        arn = _upsert_schedule(target["target_id"], interval,
                                target.get("schedule_window"))
        _targets().update_item(
            Key={"target_id": target["target_id"]},
            UpdateExpression="SET schedule_arn = :a",
            ExpressionAttributeValues={":a": arn},
        )

    # The single most useful thing to say at this moment, and the thing that
    # used to be missing. A market watch confirmed after the close does not run
    # for hours; without this the user sees "active", waits, gets nothing, and
    # reasonably concludes the product is broken. That happened.
    # Deliberately computed, never stored: it is right for about one interval
    # and then it is a lie, and a table that describes something which is no
    # longer true is the exact defect Phase 5 took out of the Notifier.
    next_check = _next_check_at(interval, window)

    # A repeating watch gets a term here rather than at plan time, because the
    # clock should start when it starts checking, not when it was described.
    repeating = bool(watch.get("repeating"))
    expires_at = (
        (datetime.now(timezone.utc)
         + timedelta(days=REPEATING_TERM_DAYS)).isoformat()
        if repeating else None
    )

    _watches().update_item(
        Key={"watch_id": watch_id},
        UpdateExpression=(
            "SET #s = :s, check_interval_min = :i, confirmed_at = :t, "
            "expires_at = :x, answers = :a"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "active",
            ":i": _to_decimal(interval),
            ":t": _now(),
            ":x": expires_at,
            ":a": _to_decimal(answers or {}),
        },
    )

    print(f"confirmed {watch_id}: {len(targets)} schedule(s) at {interval}min, "
          f"~${estimate['estimated_monthly_usd']:.2f}/month, "
          f"first check {next_check or 'unknown'}"
          + (f", expires {expires_at}" if expires_at else ""))
    return _response(200, {
        "watch_id": watch_id,
        "status": "active",
        "check_interval_min": interval,
        "targets_scheduled": len(targets),
        "next_check_at": next_check,
        "repeating": repeating,
        "expires_at": expires_at,
        "cost": estimate,
    })


def patch_watch(event) -> dict:
    """Pause, resume, or retune the interval of an existing watch."""
    watch_id = event["pathParameters"]["id"]
    watch = _get_watch(watch_id)
    body = _body(event)

    updates, names, values = [], {}, {}
    status = watch.get("status")

    if "status" in body:
        wanted = body["status"]
        if wanted == "paused":
            if status not in PAUSABLE:
                raise HttpError(409, f"watch {watch_id} is {status}, cannot pause")
        elif wanted == "active":
            if status not in RESUMABLE:
                raise HttpError(409, f"watch {watch_id} is {status}, cannot resume")
        else:
            raise HttpError(400, "status must be 'paused' or 'active'")
        updates.append("#s = :s")
        names["#s"] = "status"
        values[":s"] = wanted
        status = wanted

    if "check_interval_min" in body:
        try:
            interval = int(body["check_interval_min"])
        except (TypeError, ValueError):
            raise HttpError(400, "check_interval_min must be an integer") from None
        if not 1 <= interval <= 1440:
            raise HttpError(400, "check_interval_min must be between 1 and 1440")

        # Snap to a cadence the schedule can express, exactly as confirm does,
        # and for the same reason: the stored interval must be the one that
        # actually runs. See shared/schedules.py.
        watch_targets = _targets_for(watch_id)
        interval = schedules.snap(interval, _window_of(watch_targets))

        # The same budget gate as confirm. Without it, PATCH would be a way to
        # walk an already-confirmed watch down to an interval confirm refused.
        estimate = _estimate_for(watch, watch_targets, interval)
        if estimate and not estimate["within_budget"]:
            raise HttpError(
                409,
                f"every {interval} min would cost about "
                f"${estimate['estimated_monthly_usd']:.2f}/month, over the "
                f"${estimate['monthly_budget_usd']:.2f} budget. "
                f"Use {estimate['min_interval_min']} min or longer.",
            )

        # Retune the live schedules too, or the stored interval would lie.
        if status == "active":
            for target in watch_targets:
                _upsert_schedule(target["target_id"], interval,
                                target.get("schedule_window"))

        updates.append("check_interval_min = :i")
        values[":i"] = _to_decimal(interval)

    if not updates:
        raise HttpError(400, "nothing to update: send status or check_interval_min")

    updates.append("updated_at = :u")
    values[":u"] = _now()

    kwargs = {
        "Key": {"watch_id": watch_id},
        "UpdateExpression": "SET " + ", ".join(updates),
        "ExpressionAttributeValues": values,
        "ReturnValues": "ALL_NEW",
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names

    updated = _watches().update_item(**kwargs)["Attributes"]
    # Resuming a paused watch is the other moment "when will you look?" gets
    # asked, and on a windowed watch the answer can be tomorrow.
    return _response(200, {
        "watch": updated,
        "next_check_at": _next_check_for(updated, _targets_for(watch_id)),
    })


def delete_watch(event) -> dict:
    """Remove a watch entirely: schedules first, then rows.

    Schedules are deleted before any row, on purpose. If this fails
    halfway the leftovers are rows in DynamoDB, which cost nothing --
    whereas a surviving schedule would keep invoking the Checker and
    keep billing forever with nothing pointing at it.
    """
    watch_id = event["pathParameters"]["id"]
    _get_watch(watch_id)

    targets = _targets_for(watch_id)
    deleted = _delete_schedules(targets)

    for target in targets:
        _targets().delete_item(Key={"target_id": target["target_id"]})
    _watches().delete_item(Key={"watch_id": watch_id})

    print(f"deleted {watch_id}: {len(targets)} target(s), "
          f"{len(deleted)} schedule(s)")
    return _response(200, {
        "watch_id": watch_id,
        "deleted": True,
        "targets_deleted": len(targets),
        "schedules_deleted": len(deleted),
    })


ROUTES = {
    "POST /watches": create_watch,
    "GET /watches": list_watches,
    "GET /watches/{id}": get_watch,
    "POST /watches/{id}/confirm": confirm_watch,
    "PATCH /watches/{id}": patch_watch,
    "DELETE /watches/{id}": delete_watch,
}


def lambda_handler(event, context):
    # routeKey is set by API Gateway (payload format 2.0). A direct invoke
    # can supply it too, which is how these routes are tested before the
    # gateway exists.
    route = event.get("routeKey")
    if not route:
        return _response(400, {"error": "no routeKey on event"})

    handler = ROUTES.get(route)
    if handler is None:
        return _response(404, {"error": f"no route for {route}"})

    try:
        return handler(event)
    except HttpError as exc:
        print(f"{route} -> {exc.status}: {exc.message}")
        return _response(exc.status, {"error": exc.message})
    except Exception as exc:  # noqa: BLE001 -- never leak a stack trace
        print(f"{route} -> 500: {type(exc).__name__}: {exc}")
        return _response(500, {"error": "internal error"})
