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

import across
import condition as condition_mod
import cost
import questions
import schedules

dynamodb = boto3.resource("dynamodb")
scheduler = boto3.client("scheduler")
lambda_client = boto3.client("lambda")

# What a row is written with when nothing identifies the caller. Kept as a
# constant rather than deleted, because rows created before sign-in existed
# carry it and must keep working -- the same rule as a target row with no
# extractor.
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


def _from_decimal(value):
    """DynamoDB returns Decimal for every number; undo it at the read edge."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_decimal(v) for v in value]
    return value


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


def _claims(event) -> dict:
    """What the token said, or nothing.

    API Gateway has already verified the signature, the issuer, the audience
    and the expiry against Cognito's published keys before this Lambda ran. So
    these claims are not input to be validated -- they are the *result* of a
    validation that happened outside our code, which is the whole reason the
    76-line authorizer could be deleted rather than improved. Nothing here
    re-checks them, and nothing here should.
    """
    context = (event.get("requestContext") or {}).get("authorizer") or {}
    return (context.get("jwt") or {}).get("claims") or {}


def _user_id(event) -> str:
    """Who is asking, by Cognito's `sub`.

    `sub` rather than `email`, deliberately. An address can be changed and can
    be reassigned; `sub` is stable for the life of the account, so a person who
    changes their email keeps their watches instead of losing them to a
    stranger who later takes the address.

    Falls back to the old constant when there is no token at all, which is the
    case for a locally-invoked Lambda and for anything created before sign-in
    existed. It is not a bypass: without a token API Gateway never routes the
    request here.
    """
    return _claims(event).get("sub") or USER_ID


def _user_email(event):
    """The address to notify, from the token rather than an environment
    variable. Google has verified it; nobody typed it into a config."""
    return _claims(event).get("email") or None


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


def _schedule_name(owner_id: str) -> str:
    """One name per thing that owns a schedule -- a target, or a whole watch.

    Both ids are already globally unique and differently prefixed (`t_`, `w_`),
    so one namespace serves both without a second naming rule to keep in step
    across four Lambdas.
    """
    return f"schedule-ai-app-{owner_id}"


def _upsert_schedule(target_id: str, interval_min: int,
                     window: str | None = None) -> str:
    """Create the schedule, or retune an existing one to a new interval.

    The name is derived from target_id rather than generated, which makes
    this safe to retry: a confirm that failed halfway can be called again
    and will update what it already created instead of duplicating it.
    """
    return _put_schedule(
        _schedule_name(target_id),
        # rate(...) normally; cron(...) plus a timezone for a windowed target,
        # so that a market watch simply does not fire when the market is shut.
        # Building the expression here is what kept the Checker from ever
        # needing to know what a stock market is.
        schedules.expression(interval_min, window),
        {"target_id": target_id},
    )


def _upsert_once(watch_id: str, when, timezone_name=None) -> str:
    """The schedule a time-triggered watch owns, firing once and self-deleting.

    **Addressed to the watch, not to a target** — decided 2026-08-02 and
    recorded in `docs/phase-9-watch-kinds.md` §8. A reminder has no target, and
    the cheap alternative was one synthetic target row, which would have made
    the table describe something that does not exist. That is the exact class
    of lie taken out of the Notifier in Phase 5, and this phase exists because
    small exceptions like it accumulated.

    So the payload carries `watch_id` where a condition watch carries
    `target_id`, and every consumer branches on which key is present.
    """
    return _put_schedule(
        _schedule_name(watch_id),
        schedules.once_expression(when, timezone_name),
        {"watch_id": watch_id},
    )


def _upsert_repeating(watch_id: str, when, repeat: str,
                      timezone_name=None) -> str:
    """The schedule a reminder that comes back owns.

    A cron, not a rate: a rate schedule counts from whenever it was created, so
    "every day at 9pm" would drift and a rate created at 14:00 fires at 14:00
    forever. The wall-clock time is the entire request.
    """
    return _put_schedule(
        _schedule_name(watch_id),
        schedules.repeating_expression(when, repeat, timezone_name),
        {"watch_id": watch_id},
    )


def _put_schedule(name: str, expression: dict, payload: dict) -> str:
    args = {
        "Name": name,
        **expression,
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {
            "Arn": os.environ["CHECKER_FUNCTION_ARN"],
            "RoleArn": os.environ["SCHEDULER_ROLE_ARN"],
            "Input": json.dumps(payload),
        },
    }
    try:
        return scheduler.create_schedule(**args)["ScheduleArn"]
    except scheduler.exceptions.ConflictException:
        return scheduler.update_schedule(**args)["ScheduleArn"]


def _delete_schedules(targets: list, watch_id: str | None = None) -> list:
    """Every schedule this watch owns, whichever shape it uses.

    A time-triggered watch has no targets, so a teardown that only walked
    `targets` would silently delete nothing and leave a schedule billing --
    the same failure the unpaginated GSI query had, arriving by a different
    road. `watch_id` is passed by every caller and the extra delete is a no-op
    for a condition watch, which has no schedule under that name.
    """
    names = [_schedule_name(t["target_id"]) for t in targets]
    if watch_id:
        names.append(_schedule_name(watch_id))

    deleted = []
    for name in names:
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
    email = _user_email(event)
    _watches().put_item(Item={
        "watch_id": watch_id,
        "user_id": _user_id(event),
        # Stored on the row, not read from an environment variable at send
        # time. A watch should be delivered to whoever asked for it, and a
        # single NOTIFY_EMAIL is the last place multi-user is still assumed.
        **({"notify_email": email} if email else {}),
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


def _confirm_reminder(watch_id: str, watch: dict, answers: dict) -> dict:
    """Commit a watch whose trigger is the clock.

    Almost nothing the ordinary path does applies. There is no interval to
    snap, no window to fit, no targets to write `watched_ids` onto, and no
    budget gate -- a reminder costs one Lambda invocation per firing, which is
    four millionths of a dollar, so running the gate would be theatre.

    What is left: decide whether it comes back, create the right shape of
    schedule, and say when it will go off. `next_check_at` is the same field
    the ordinary path returns and means the same thing -- just exact rather
    than estimated, which is the one place in this product where it is.
    """
    fire_at = watch["fire_at"]
    zone = watch.get("fire_timezone") or None
    repeat = _repeat_of(watch, answers)

    if repeat == "once":
        arn = _upsert_once(watch_id, fire_at, zone)
        expires_at = None
    else:
        arn = _upsert_repeating(watch_id, fire_at, repeat, zone)
        # A repeating reminder is the second thing in this system that does
        # not stop by itself. Same term as a repeating vacancy watch, and the
        # same reason: a forgotten one would arrive every evening for years.
        expires_at = (datetime.now(timezone.utc)
                      + timedelta(days=REPEATING_TERM_DAYS)).isoformat()

    _watches().update_item(
        Key={"watch_id": watch_id},
        UpdateExpression=("SET #s = :s, confirmed_at = :t, schedule_arn = :a, "
                          "#r = :r, expires_at = :x, answers = :ans"),
        ExpressionAttributeNames={"#s": "status", "#r": "repeat"},
        ExpressionAttributeValues={
            ":s": "active", ":t": _now(), ":a": arn, ":r": repeat,
            ":x": expires_at, ":ans": _to_decimal(answers or {}),
        },
    )

    print(f"confirmed {watch_id}: {repeat} reminder at {fire_at}"
          f"{f' ({zone})' if zone else ''}")
    return _response(200, {
        "watch_id": watch_id,
        "status": "active",
        "next_check_at": fire_at,
        "fire_at": fire_at,
        "fire_timezone": zone,
        "repeat": repeat,
        "targets_scheduled": 0,
        # A daily reminder survives its own firing, exactly as a vacancy watch
        # does, and the Notifier reads this to decide whether to tear the
        # schedule down.
        "repeating": repeat != "once",
        "expires_at": expires_at,
        # Named, and zero. Omitting it would leave the client to guess whether
        # the field was missing or the answer was nothing.
        "cost": {"estimated_monthly_usd": 0.0, "within_budget": True},
    })


def _repeat_of(watch: dict, answers: dict) -> str:
    """Once, daily or weekly -- the answer if one was given, else the plan's.

    The answer wins because it is newer and it is the user's. The plan's value
    is `once` unless the request said otherwise, and the question is only asked
    when the request left it open, so a user who confirms without answering
    gets the safe direction: a reminder that comes once is easier to fix than
    one that arrives every evening and was never wanted.
    """
    chosen = (answers or {}).get("repeat")
    if isinstance(chosen, list):
        chosen = chosen[0] if chosen else None
    if isinstance(chosen, str) and chosen in ("once", "daily", "weekly"):
        return chosen
    stored = watch.get("repeat")
    return stored if stored in ("once", "daily", "weekly") else "once"


def _repin_baseline(watch: dict, targets: list, kept) -> dict:
    """Re-derive a relative threshold from the offer the user actually picked.

    The order of events is what creates this problem, and the order is right:
    the Planner cannot know which offer is meant until it has fetched the shops
    and asked about what it found, and the answers only arrive here. So the
    baseline it stored is the cheapest thing any shop listed for the search --
    measured on real pages, a ILS 139 headset for "xbox series x" -- and the
    threshold hanging off it is 10% below an object nobody is watching.

    Recomputed from the pinned offers, best-first in the same direction the
    condition is judged in, and only ever across offers priced in the
    condition's currency: `across.py` on why nothing here converts money.

    Never raises and never widens the watch. Anything unexpected -- no
    relative condition, no answers, no priced offer left -- returns the
    condition exactly as it was, which is the behaviour that shipped before
    this existed.
    """
    stored = _from_decimal(watch.get("condition") or {})
    pct = stored.get("relative_change_pct")
    if kept is None or pct is None:
        return stored

    prices = []
    for target in targets:
        if not across.comparable(target, stored):
            continue
        for item in _from_decimal(target.get("verified_items") or []):
            price = item.get("price")
            if (item.get("id") in kept
                    and isinstance(price, (int, float))
                    and not isinstance(price, bool)):
                prices.append(float(price))

    if not prices:
        return stored

    pick = max if across.direction(stored) == "max" else min
    baseline = pick(prices)
    if baseline == stored.get("baseline"):
        return stored

    repinned = condition_mod.resolve_relative_condition(
        stored, pct, baseline,
        baseline_at=_now(),
        # Unchanged: whether the market was open is a fact about when the page
        # was read, and this is the same reading, narrowed to fewer offers.
        baseline_source=stored.get("baseline_source"),
    )
    print(f"repinned baseline {stored.get('baseline')} -> {baseline}, "
          f"threshold {stored.get('value')} -> {repinned.get('value')}")
    return repinned


def confirm_watch(event) -> dict:
    """Commit a proposed plan: create its schedules and go active."""
    watch_id = event["pathParameters"]["id"]
    watch = _get_watch(watch_id)

    status = watch.get("status")
    if status not in CONFIRMABLE:
        raise HttpError(409, f"watch {watch_id} is {status}, not proposed")

    # A time-triggered watch has nothing to poll, so it takes the short path
    # and returns before any of this. Everything below is about an interval, a
    # window, targets and a budget, and none of those has a meaning for a watch
    # that fires once -- `check_interval_min` in particular is absent on the
    # row, and the ordinary path rejects that as a 400.
    if watch.get("fire_at"):
        return _confirm_reminder(watch_id, watch, _body(event).get("answers"))

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

    # Which offers the answers left standing, per target. For a price watch
    # this is the difference between watching a console and watching a headset:
    # the cheapest thing a shop lists for "xbox series x" is an accessory, so a
    # watch that does not pin the product is confidently wrong about money.
    #
    # Computed here from the questions and answers rather than taken from the
    # client, using the same set intersection the plan card does while the user
    # clicks -- so what they saw narrow is what gets watched.
    kept = questions.chosen_ids(
        _from_decimal(watch.get("questions") or []), answers or {})

    # "10% cheaper than now" was computed at plan time from the cheapest offer
    # any shop listed -- which for "xbox series x" is a ILS 139 headset. The
    # product is only pinned down here, by the answers. Left alone, the watch
    # would carry a threshold derived from an object it is not watching.
    condition = _repin_baseline(watch, targets, kept)

    for target in targets:
        arn = _upsert_schedule(target["target_id"], interval,
                                target.get("schedule_window"))
        update = "SET schedule_arn = :a"
        values = {":a": arn}
        if kept is not None:
            # Only the ids this target actually has. A target left with none is
            # one the answers ruled out entirely, and it will read `unavailable`
            # rather than pretending the cheapest thing on the page is the one.
            mine = [i["id"] for i in (target.get("verified_items") or [])
                    if i.get("id") in kept]
            update += ", watched_ids = :w"
            values[":w"] = mine
        _targets().update_item(
            Key={"target_id": target["target_id"]},
            UpdateExpression=update,
            ExpressionAttributeValues=values,
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
            "expires_at = :x, answers = :a, #c = :cond"
        ),
        ExpressionAttributeNames={"#s": "status", "#c": "condition"},
        ExpressionAttributeValues={
            ":s": "active",
            ":i": _to_decimal(interval),
            ":t": _now(),
            ":x": expires_at,
            ":a": _to_decimal(answers or {}),
            ":cond": _to_decimal(condition),
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
    deleted = _delete_schedules(targets, watch_id)

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
