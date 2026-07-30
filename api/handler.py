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
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")
scheduler = boto3.client("scheduler")
lambda_client = boto3.client("lambda")

# Every row is written with this until real auth exists. Kept as a field
# rather than dropped, so multi-user is a query change and not a migration.
USER_ID = "default"

# Statuses a watch can be in. planning -> proposed -> active -> triggered,
# with failed as a dead end off planning and paused toggling with active.
CONFIRMABLE = ("proposed",)
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


def _rate_expression(minutes: int) -> str:
    """EventBridge Scheduler wants the unit singular at 1: 'rate(1 minute)'."""
    return f"rate({minutes} minute{'s' if minutes != 1 else ''})"


def _schedule_name(target_id: str) -> str:
    return f"schedule-ai-app-{target_id}"


def _upsert_schedule(target_id: str, interval_min: int) -> str:
    """Create the schedule, or retune an existing one to a new interval.

    The name is derived from target_id rather than generated, which makes
    this safe to retry: a confirm that failed halfway can be called again
    and will update what it already created instead of duplicating it.
    """
    args = {
        "Name": _schedule_name(target_id),
        "ScheduleExpression": _rate_expression(interval_min),
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


def get_watch(event) -> dict:
    watch_id = event["pathParameters"]["id"]
    watch = _get_watch(watch_id)
    return _response(200, {"watch": watch, "targets": _targets_for(watch_id)})


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

    targets = _targets_for(watch_id)
    if not targets:
        raise HttpError(409, f"watch {watch_id} has no targets to schedule")

    for target in targets:
        arn = _upsert_schedule(target["target_id"], interval)
        _targets().update_item(
            Key={"target_id": target["target_id"]},
            UpdateExpression="SET schedule_arn = :a",
            ExpressionAttributeValues={":a": arn},
        )

    _watches().update_item(
        Key={"watch_id": watch_id},
        UpdateExpression=(
            "SET #s = :s, check_interval_min = :i, confirmed_at = :t"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "active",
            ":i": _to_decimal(interval),
            ":t": _now(),
        },
    )

    print(f"confirmed {watch_id}: {len(targets)} schedule(s) at {interval}min")
    return _response(200, {
        "watch_id": watch_id,
        "status": "active",
        "check_interval_min": interval,
        "targets_scheduled": len(targets),
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

        # Retune the live schedules too, or the stored interval would lie.
        if status == "active":
            for target in _targets_for(watch_id):
                _upsert_schedule(target["target_id"], interval)

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
    return _response(200, {"watch": updated})


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
