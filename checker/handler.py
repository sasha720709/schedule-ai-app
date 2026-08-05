"""Checker Lambda handler. One invocation = one scheduled tick for one
target: read the target, check it, write the result back.

## Tier 0

Since Phase 8b this runs the extractor the Planner compiled, in Python, for
roughly four millionths of a dollar -- instead of sending 20,000 characters to
Haiku and paying it to re-solve, on every tick, a problem that was solved once
at planning time.

The model has not been deleted; it has been moved out of the hot path. A
target row with no `extractor` still goes through `judge()`, because rows
written before this change exist and must keep working rather than start
failing. Phase 8d brings the model back deliberately, as *repair* rather than
as *reading*.

## What a tick can conclude

    ok            a value was read -- evaluate the condition against it
    unavailable   there is legitimately no value today -- not met, not broken
    failed        the extractor did not work -- record it for 8d to escalate
    blocked       the source refused us -- not broken, and not repairable

`unavailable` and `failed` are written to different fields on purpose. A watch
that has quietly stopped being able to read its target must be distinguishable
from one that is patiently waiting, or it will wait forever.

`blocked` is the same argument a third time, and it earns its place by what it
prevents: without it, the day a large shop starts turning us away, every watch
on that shop pays Haiku to repair an extractor that was never broken, fails,
and degrades separately -- N emails saying N things broke, and nothing saying
the one true thing. See `shared/blocked.py`.
"""

import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

import across
import blocked
import cost
from condition import ConditionError, evaluate
from extract import FAILED, OK, UNAVAILABLE, SpecError, extract, plausible
from check import fetch_raw, fetch_text, judge
import questions
from rank import rank
import shipping
from repair import repair

dynamodb = boto3.resource("dynamodb")
events = boto3.client("events")
lambda_client = boto3.client("lambda")
cloudwatch = boto3.client("cloudwatch")


# How many consecutive failed ticks before a watch is declared broken.
#
# The monthly budget already bounds *spending* on repairs, but on a long
# interval it bounds it slowly -- at 60 minutes a watch could fail and retry
# for most of a month before the money ran out, saying nothing. This is a
# different signal: a repair that was attempted and could not produce a working
# spec will usually fail the same way against the same page next tick. Three
# says "this is not a transient network blip" without waiting for the budget.
DEGRADE_AFTER = 3

# The same idea for a source that is refusing us, and deliberately far more
# patient. A broken extractor fails the same way against the same page every
# time, so three ticks is plenty of evidence. A block is *probabilistic* --
# Amazon served twenty-one good renders across two days and blocks on a
# datacenter IP are routinely transient -- so treating a couple of refusals as
# proof would kill a working watch over one bad afternoon.
#
# The cost of being patient is nothing: a blocked check pays for a fetch and
# no model at all. The cost of being hasty is a watch the owner has to
# recreate. So this errs long, and the CloudWatch metric below is what makes
# the situation visible long before the counter runs out.
BLOCKED_DEGRADE_AFTER = 10


def _record_cost(fetch_method: str, used_model: bool) -> None:
    """Publish what this check cost, so the spend is visible somewhere.

    AWS budget alarms cannot see Anthropic charges -- the alarms at
    $50/$100/$200 are blind to what has been the dominant cost of this system.
    This metric is what a CloudWatch alarm will eventually watch; creating that
    alarm needs cloudwatch:PutMetricAlarm on the deploy user, so it is grouped
    with Phase 5.

    Deliberately never raises. A metric that fails to publish must not fail a
    check that otherwise succeeded.
    """
    try:
        spent = cost.cost_per_check(fetch_method, used_model)
        cloudwatch.put_metric_data(
            Namespace="ScheduleAI",
            MetricData=[
                {
                    "MetricName": "EstimatedCostUSD",
                    "Value": spent,
                    "Unit": "None",
                    "Dimensions": [
                        {"Name": "FetchMethod", "Value": fetch_method},
                        {"Name": "UsedModel", "Value": str(used_model).lower()},
                    ],
                },
                # The same number again with no dimensions, because a
                # dimensioned metric cannot be read back -- or alarmed on --
                # without naming its exact dimension set. Alarming on
                # {browser,false} would miss every HTTP check and every repair,
                # which is precisely the spend worth catching. CloudWatch treats
                # the dimensionless series as its own metric, so this one is the
                # total across every watch and the only thing an alarm can
                # usefully watch.
                {
                    "MetricName": "EstimatedCostUSD",
                    "Value": spent,
                    "Unit": "None",
                },
            ],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"could not publish cost metric: {type(exc).__name__}: {exc}")


def _record_block(url: str) -> None:
    """One metric, so "the day Amazon starts blocking" is one alarm.

    This is the entire point of giving a refusal its own state. Without it the
    event is N watches degrading separately over three ticks each, which looks
    like N unrelated broken extractors and reads like the product rotting.

    Published twice for the reason on `_record_cost`: a dimensioned metric
    cannot be alarmed on without naming its exact dimension set, so the bare
    series is the one an alarm can watch and the `Host` dimension is what says
    *which* source went dark. Never raises -- a metric that fails to publish
    must not fail a check that already knows its answer.
    """
    try:
        host = blocked.host_of(url)
        cloudwatch.put_metric_data(
            Namespace="ScheduleAI",
            MetricData=[
                {"MetricName": "BlockedFetches", "Value": 1.0, "Unit": "Count",
                 "Dimensions": [{"Name": "Host", "Value": host}]},
                {"MetricName": "BlockedFetches", "Value": 1.0, "Unit": "Count"},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"could not publish blocked metric: {type(exc).__name__}: {exc}")


def _fetch(url: str, fetch_method: str, *, markup: bool) -> str:
    """Plain GET by default. The Planner marks a target "browser" when the page
    needs JavaScript to render, which costs a second Lambda and a few seconds --
    so it's opt-in per target, not the default.

    `markup=True` returns HTML for a compiled extractor; `markup=False` returns
    reduced text for the model, where every character is billed.
    """
    if fetch_method != "browser":
        body = fetch_raw(url) if markup else fetch_text(url)
        # Checked here as well as inside `fetch_raw`, and not by accident. A
        # refusal must be recognised by *the Checker*, whichever reading
        # function happens to be wired in -- making the behaviour depend on
        # that is how a guarantee turns into a coincidence. The second scan is
        # a few thousand characters and costs nothing.
        blocked.check(body=body, url=url)
        return body

    response = lambda_client.invoke(
        FunctionName=os.environ["FETCHER_FUNCTION_ARN"],
        Payload=json.dumps({"url": url}),
    )
    payload = json.loads(response["Payload"].read())

    if "FunctionError" in response:
        raise RuntimeError(f"browser fetch failed: {payload}")

    # A rendered refusal reaches here as a perfectly good 200 with a captcha in
    # it, and would then read as "the selector stopped matching" -- which pays
    # Haiku to repair an extractor that was never broken. Checked before the
    # payload is used for anything.
    blocked.check(status=payload.get("status"),
                  body=payload.get("html") or payload.get("text") or "",
                  url=url)

    if not markup:
        return payload["text"]

    # Refuse to silently substitute text for markup. Feeding stripped text to a
    # CSS extractor is precisely the bug that blocked this phase, and it fails
    # as "selector matched nothing" -- which reads as a broken plan.
    if "html" not in payload:
        raise RuntimeError(
            "browser fetch returned no `html`; the Fetcher image is older than "
            "the Checker. Redeploy it with aws lambda update-function-code."
        )
    return payload["html"]


def _from_decimal(value):
    """DynamoDB hands back every number as Decimal, which json.dumps can't
    serialize. Undo it at the read boundary -- the inverse of _to_decimal."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_decimal(v) for v in value]
    return value


def _only_watched(target: dict, result):
    """Narrow an offer list to the ones the user actually picked.

    A shop search for "xbox series x" returns headsets, controllers and games,
    and the console is one line among twenty. Without this the cheapest offer
    on the page *is* the reading, so a watch for "under 2000 shekels" fires
    within minutes on a ILS 139 headset and reports it as the price of a
    console. Measured on real shop pages; it is not a hypothetical.

    `watched_ids` is written at confirm time from the answers to the plan
    card's questions. **Absent and empty are deliberately different**, and
    conflating them was a real bug caught by running this rather than reading
    it: absent means no preference was stated, so the reading is left exactly
    as it was; empty means the answers were given and *this shop* had nothing
    matching, so it must not quietly fall back to the cheapest thing on the
    page -- which is how a console watch ends up following a ILS 29 game.

    Either way, none present today is **`unavailable`, not `failed`**: the
    product is out of stock, delisted, or not carried here, which is a
    legitimate state of the world and the one thing 8d must never pay a model
    to "repair". The watch keeps looking, which is what you want from "tell me
    when this shop gets it".
    """
    raw = target.get("watched_ids")
    if raw is None or not result.items:
        return result
    watched = set(_from_decimal(raw))

    kept = [i for i in result.items if i.get("id") in watched]
    if not kept:
        return type(result)(
            UNAVAILABLE,
            notes=["none of the offers being watched are listed right now"])

    # Landed, not sticker: the reading is what the pinned offer costs to
    # receive. Where delivery is unknown the two are the same number and this
    # behaves exactly as it did -- see shared/shipping.py on why that is not
    # the same claim.
    prices = [shipping.landed(i) for i in kept
              if isinstance(i.get("price"), (int, float))]
    return type(result)(
        result.status,
        value=min(prices) if prices else result.value,
        raw=str(min(prices)) if prices else result.raw,
        items=kept,
    )


def _reading_of(source: dict, *, stored: bool = False) -> str:
    """One reading, as a string, for comparing this check against the last.

    Deliberately the **raw** text rather than the parsed number. `last_value`
    comes back from DynamoDB as a `Decimal`, and `Decimal("306.49") == 306.49`
    is *False* -- the float is a binary approximation of a value the Decimal
    holds exactly. Comparing those would report a change on every single check
    of an utterly static price, which fails safe but makes the field useless.

    The raw string is what the extractor actually saw, and comparing it is
    exact. `value` is only a fallback for the model path, which does not always
    produce a raw.
    """
    raw = source.get("last_raw") if stored else source.get("raw")
    if raw is not None:
        return str(raw)
    return str(source.get("last_value") if stored else source.get("value"))


def _to_decimal(value):
    """DynamoDB's resource API rejects native floats. Tier 0 produces real
    numbers where the model path only ever produced strings, so this is newly
    needed on the write side of the Checker."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_decimal(v) for v in value]
    return value


def _tier0(target: dict, watch_condition: dict, fetch_method: str) -> dict:
    """Run the compiled extractor. No model, no network beyond one fetch."""
    spec = _from_decimal(target["extractor"])
    payload = _fetch(target["url"], fetch_method, markup=True)
    outcome = _judge_extraction(spec, payload, target, watch_condition)
    outcome["payload"] = payload
    return outcome


def _judge_extraction(spec, payload, target, watch_condition) -> dict:
    result = extract(spec, payload)

    if result.status == FAILED:
        return {
            "status": FAILED,
            "value": None,
            "raw": result.raw,
            "note": "",
            "error": result.error,
            "condition_met": False,
        }

    if result.status == UNAVAILABLE:
        # Not met, and not broken. 8d must leave this alone.
        return {
            "status": UNAVAILABLE,
            "value": None,
            "raw": None,
            "note": "; ".join(result.notes) or "no value available right now",
            "error": None,
            "condition_met": False,
        }

    # A selector can survive a redesign and start pointing at a different
    # number -- a shipping cost, a review count. Comparing against the value
    # verified at plan time is the cheap defence. Skipped for `count`, where a
    # legitimate reading of 0 against a baseline of 3 is the normal case and
    # would look wildly implausible.
    baseline = _from_decimal(target.get("verified_value"))
    if spec.get("kind") != "count" and not plausible(result.value, baseline):
        return {
            "status": FAILED,
            "value": None,
            "raw": result.raw,
            "note": "",
            "error": (
                f"implausible reading {result.value!r} against the plan-time "
                f"value {baseline!r} -- the extractor is probably reading the "
                f"wrong element"
            ),
            "condition_met": False,
        }

    result = _only_watched(target, result)
    if result.status == UNAVAILABLE:
        return {
            "status": UNAVAILABLE,
            "value": None,
            "raw": None,
            "note": "; ".join(result.notes),
            "error": None,
            "condition_met": False,
        }

    try:
        met = evaluate(watch_condition, result.value)
    except ConditionError as exc:
        # An op nobody can evaluate is a broken plan, not an unmet condition.
        return {
            "status": FAILED,
            "value": result.value,
            "raw": result.raw,
            "note": "",
            "error": f"condition is not evaluable: {exc}",
            "condition_met": False,
        }

    return {
        "status": OK,
        "value": result.value,
        "raw": result.raw,
        "note": "",
        "error": None,
        "condition_met": met,
        # Only a `count` or `offers` extraction produces these. They are what a
        # repeating watch deduplicates on and what the email is written from.
        "items": result.items,
    }


def _tier_model(target: dict, watch_condition: dict, fetch_method: str) -> dict:
    """The pre-Phase-8 path, kept for target rows that carry no extractor."""
    page_text = _fetch(target["url"], fetch_method, markup=False)
    verdict = judge(
        target["url"], target["extract_hint"], watch_condition, page_text
    )
    value = verdict.get("last_value")
    return {
        "status": OK if value is not None else UNAVAILABLE,
        "value": value,
        "raw": value,
        "note": verdict.get("note", ""),
        "error": None,
        "condition_met": bool(verdict.get("condition_met")),
    }


# How many item identities to keep per target. A job board churns; without a
# cap this list would grow for as long as the watch lived, and DynamoDB stops
# at 400KB per item. Twelve-hex ids, so 500 of them is about 7KB.
#
# The consequence of the cap, stated rather than hidden: a posting that falls
# out of the window and later reappears is reported again. That is the right
# way round -- a duplicate email is a small annoyance, a missed job is the
# thing the watch exists to prevent.
MAX_SEEN_ITEMS = 500


def _json_safe(value):
    """DynamoDB hands numbers back as Decimal, which json.dumps refuses."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _rank(watch: dict, target: dict, fresh: list):
    """Score what just appeared against what was asked for. Never raises.

    Charged against the same monthly budget as checks and repairs, for the
    reason on `cost.can_afford_repair`: one guarantee is easier to reason about
    than three. **Ranking is what degrades when the budget runs out, never the
    notification** -- a watch that fires constantly stops being ranked and
    keeps emailing, which is the right way round.
    """
    prompt = watch.get("prompt") or ""
    interval = int(watch.get("check_interval_min") or 60)
    spent = float(watch.get("rank_spend_usd") or 0)

    # What the user said when they answered the plan card's questions. These
    # become ranking *criteria*, never a filter: the answers were given about
    # the postings visible that day, and a future job that misses one should
    # score lower rather than disappear. See shared/questions.py.
    criteria = questions.as_criteria(
        _from_decimal(watch.get("questions") or []),
        _from_decimal(watch.get("answers") or {}),
    )

    if not cost.can_afford_rank(interval,
                                fetch_method=target.get("fetch_method", "http"),
                                spend_usd=spent, items=len(fresh)):
        print(f"[rank] skipped, ${spent:.4f} already spent this month")
        return fresh, 0.0

    ranked, spend = rank(prompt, fresh, criteria=criteria)
    if spend:
        kept, dropped = len(ranked), len(fresh) - len(ranked)
        print(f"[rank] {kept} kept, {dropped} set aside, ${spend:.4f}")
    return ranked, spend


def _siblings(targets_table, watch_id: str) -> list:
    """Every target of this watch, following pagination.

    The same query the Notifier makes, for the same reason: a `query()` returns
    at most 1MB, and with three targets that limit is unreachable right up
    until the day it is not.
    """
    items, start_key = [], None
    while True:
        kwargs = {
            "IndexName": "watch_id-index",
            "KeyConditionExpression": Key("watch_id").eq(watch_id),
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = targets_table.query(**kwargs)
        items.extend(response.get("Items", []))
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            return items


def _readings(targets_table, watch: dict, target: dict, result: dict,
              condition: dict, now: str) -> list:
    """What every shop is charging right now, best first.

    A watch on three shops used to report one of them -- whichever ticked into
    the condition -- and the email said "ILS 1,890" with a link, leaving the
    obvious next question ("is that the cheapest?") unanswered by the one
    system that knew. This is that answer.

    **Only computed when the watch is about to speak.** A DynamoDB query is
    cheap but not free, and paying it on every tick of every watch to build a
    picture nobody reads is exactly the per-tick cost Phase 8b removed. Same
    rule as ranking: paid per notification, not per check.

    Never raises. A cross-shop summary is the nicest sentence in the email and
    it is not worth a single missed notification -- the established rule that
    nothing about presentation may block a send.
    """
    try:
        siblings = _siblings(targets_table, watch["watch_id"])
        if len(siblings) < 2:
            return []
        items = result.get("items") or []
        mine = across.Reading(
            target_id=target["target_id"],
            value=result["value"],
            currency=target.get("currency"),
            url=target.get("url"),
            shop=target.get("shop"),
            at=now,
            shipping=items[0].get("shipping") if items else None,
        )
        found = across.readings(
            [_from_decimal(s) for s in siblings], condition,
            now=datetime.now(timezone.utc),
            interval_min=int(_from_decimal(watch.get("check_interval_min")) or 60),
            override={target["target_id"]: mine},
        )
        print(f"[across] {across.describe(found)}")
        return [r.as_dict() for r in found]
    except Exception as exc:  # noqa: BLE001
        print(f"could not read sibling targets: {type(exc).__name__}: {exc}")
        return []


def _already_fired(exc) -> bool:
    """Did another target of this watch get there first?

    Every target of a watch is scheduled at the same interval and the schedules
    are created in the same second, so EventBridge fires them together. Two
    shops crossing the same threshold on the same tick would both flip the
    watch to `triggered` and both publish -- two emails about one event. The
    conditional write makes the transition happen once; this recognises the
    loser.

    Matched on the name rather than on a class because the resource API raises
    a dynamically-built exception type. `ClientError` is checked too, which is
    what the low-level client raises for the same condition.
    """
    if type(exc).__name__ == "ConditionalCheckFailedException":
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        return code == "ConditionalCheckFailedException"
    return False


def _remember(targets_table, target_id: str, target: dict, fresh: list) -> None:
    """Record which items have now been reported, newest last, bounded."""
    seen = list(target.get("seen_item_ids") or [])
    seen.extend(i["id"] for i in fresh if i.get("id"))
    targets_table.update_item(
        Key={"target_id": target_id},
        UpdateExpression="SET seen_item_ids = :s",
        ExpressionAttributeValues={":s": seen[-MAX_SEEN_ITEMS:]},
    )


def _expire(watches_table, watch: dict, target: dict, now: str) -> dict:
    """A repeating watch that has run out its term. Not a fault.

    Reuses the `WatchDegraded` event because it does exactly the right two
    things -- email, then tear down the schedules -- and adding a third event
    type would mean a third EventBridge rule for a difference the plumbing
    does not care about. The user-facing wording is what has to be honest, so
    `reason_kind` tells the Notifier which email to write. Nothing here is
    broken and the email must not say it is.
    """
    watch_id = watch["watch_id"]
    print(f"EXPIRED {watch_id}: term ended at {watch.get('expires_at')}")

    watches_table.update_item(
        Key={"watch_id": watch_id},
        UpdateExpression="SET #s = :s, expired_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "expired", ":t": now},
    )
    events.put_events(Entries=[{
        "Source": "schedule-ai-app.checker",
        "DetailType": "WatchDegraded",
        "EventBusName": os.environ["EVENT_BUS_NAME"],
        "Detail": json.dumps({
            "watch_id": watch_id,
            "target_id": target["target_id"],
            "url": target["url"],
            "prompt": watch.get("prompt", ""),
            "reason_kind": "expired",
            "reason": f"the watch reached the end of its term "
                      f"({watch.get('expires_at')})",
            "trigger_count": int(watch.get("trigger_count") or 0),
            "degraded_at": now,
        }),
    }])
    return {"checked": False, "status": "expired"}


def _fire_on_time(watch_id: str) -> dict:
    """A watch whose trigger is the clock. Nothing is read and nothing is judged.

    Everything else in this Lambda exists to answer "is it true yet". Here the
    answer arrived with the invocation, so the work is: confirm the watch still
    wants to hear from us, say so, and stop.

    **The schedule is not deleted here.** `at(...)` carries
    `ActionAfterCompletion: DELETE`, so a one-shot removes itself when it
    fires; the Notifier's teardown is the belt to that braces and is a no-op by
    the time it runs. Deleting it here as well would be a third place that has
    to agree about a name.

    Costs nothing to run and nothing to check -- no fetch, no model -- so the
    cost metric is not published: there is no spend to make visible, and a
    zero would dilute the series the daily-spend alarm reads.
    """
    watches_table = dynamodb.Table(os.environ["WATCHES_TABLE"])
    watch = watches_table.get_item(Key={"watch_id": watch_id}).get("Item")
    if watch is None:
        raise RuntimeError(f"No such watch: {watch_id}")

    if watch["status"] != "active":
        # Paused, already fired, or deleted between the schedule firing and
        # this reading the row. Not an error -- the same guard every tick has.
        print(f"watch {watch_id} status={watch['status']}, not firing")
        return {"skipped": True, "status": watch["status"]}

    now = datetime.now(timezone.utc).isoformat()

    # A repeating reminder is the second thing here that does not stop by
    # itself. Checked before anything else, exactly as a repeating vacancy
    # watch is, so an expired one costs a read rather than an email.
    repeating = (watch.get("repeat") or "once") != "once"
    expires_at = watch.get("expires_at")
    if repeating and expires_at and now >= expires_at:
        return _expire(watches_table, watch, {"target_id": None, "url": None},
                       now)

    if repeating:
        # It stays `active`: this reminder is not finished, it just spoke.
        # Marking it `triggered` would stop it -- and would make the Notifier
        # tear down a schedule that has to survive.
        watches_table.update_item(
            Key={"watch_id": watch_id},
            UpdateExpression=("SET last_triggered_at = :t, trigger_count = :n"),
            ExpressionAttributeValues={
                ":t": now,
                ":n": int(watch.get("trigger_count") or 0) + 1,
            },
        )
    else:
        # The same conditional transition a condition watch uses. A one-shot
        # cannot race with a sibling target because it has none, but it *can*
        # be retried by EventBridge after a downstream failure, and firing
        # twice means telling a person twice.
        try:
            watches_table.update_item(
                Key={"watch_id": watch_id},
                UpdateExpression="SET #s = :s, triggered_at = :t",
                ConditionExpression="#s = :active",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": "triggered", ":t": now, ":active": "active",
                },
            )
        except Exception as exc:  # noqa: BLE001
            if not _already_fired(exc):
                raise
            print(f"watch {watch_id} has already fired -- not repeating it")
            return {"skipped": True, "status": "triggered"}

    events.put_events(Entries=[{
        "Source": "schedule-ai-app.checker",
        "DetailType": "WatchTriggered",
        "EventBusName": os.environ["EVENT_BUS_NAME"],
        "Detail": json.dumps({
            "watch_id": watch_id,
            # No target and no page. Saying so plainly is better than an empty
            # string that reads as a missing value downstream.
            "target_id": None,
            "url": None,
            "prompt": watch.get("prompt", ""),
            "last_value": None,
            "note": watch.get("reminder_note") or "",
            "items": [],
            "readings": [],
            # Tells the Notifier whether to tear the schedule down. A daily
            # reminder must survive its own notification, exactly as a
            # repeating vacancy watch does.
            "repeating": repeating,
            "repeat": watch.get("repeat") or "once",
            "fire_at": watch.get("fire_at"),
            "fire_timezone": watch.get("fire_timezone"),
            "reminder_title": watch.get("reminder_title") or "",
            "expires_at": expires_at,
            # What the Notifier branches on to write a reminder rather than a
            # "your watch came true" email. The two are not the same message:
            # nothing was found, and nothing was being looked for.
            "trigger_kind": "time",
            "triggered_at": now,
        }),
    }])
    print(f"TIME REACHED for watch {watch_id} -- event emitted"
          + (" (repeating)" if repeating else ""))
    return {"checked": True, "status": "fired", "notified": True,
            "trigger_kind": "time", "repeating": repeating}


def _record_blocked(targets_table, watches_table, watch: dict, target: dict,
                    exc, now: str, fetch_method: str) -> dict:
    """A refusal: recorded, counted separately, never repaired.

    Three deliberate differences from a failure, and each one is the whole
    reason this state exists.

    **No repair.** The extractor is fine. Paying Haiku to rewrite a selector
    against a captcha page cannot work and would be paid on every tick of every
    Amazon watch for as long as the block lasted.

    **Its own counter.** `consecutive_failures` is what 8d escalates on, and
    mixing refusals into it would degrade a healthy watch three ticks after a
    site had a bad afternoon. `consecutive_blocks` is reset by any successful
    read, exactly like the other.

    **A metric, so it is one event.** The alarm on `BlockedFetches` is what
    turns "Amazon started blocking" from N separate broken watches into a
    single thing the owner is told once.
    """
    target_id = target["target_id"]
    blocks = int(_from_decimal(target.get("consecutive_blocks", 0)) or 0) + 1
    print(f"target={target_id} BLOCKED ({blocks}): {exc.reason}")

    _record_cost(fetch_method, used_model=False)
    _record_block(target.get("url", ""))

    targets_table.update_item(
        Key={"target_id": target_id},
        UpdateExpression=(
            "SET last_checked_at = :t, last_error = :e, last_status = :s, "
            "consecutive_blocks = :b, blocked_at = :t"
        ),
        ExpressionAttributeValues={
            ":t": now, ":e": str(exc.reason)[:500], ":s": blocked.BLOCKED,
            ":b": blocks,
        },
    )

    if blocks < BLOCKED_DEGRADE_AFTER:
        return {"checked": True, "status": blocked.BLOCKED,
                "blocked": blocks, "error": exc.reason}

    # Long past a bad afternoon. Stop paying to be refused, and say plainly
    # that the source shut us out rather than that the watch broke -- the
    # owner can act on the first and can do nothing about the second.
    return _degrade(watches_table, targets_table, watch, target,
                    {"error": exc.reason}, now, 0.0, reason_kind="blocked")


def _degrade(watches_table, targets_table, watch: dict, target: dict,
             result: dict, now: str, repair_spend: float,
             reason_kind: str = "broken") -> dict:
    """Tier 2: stop, say why, and stop billing.

    A watch that cannot read its target must say so. Silence is the failure
    this whole phase is organised against -- a deterministic extractor that
    quietly returns nothing looks exactly like a condition that is not met, and
    the owner learns nothing for weeks.

    Degrading **deletes the schedules**, like a triggered watch does. Continuing
    to check something known to be broken bills EventBridge, Lambda and
    DynamoDB every tick to re-learn a fact already established. The row keeps
    `last_error` and `repair_spend_usd`, so re-planning is a decision the owner
    makes with the reason in front of them.
    """
    watch_id = watch["watch_id"]
    print(f"DEGRADING {watch_id}: {result['error']}")

    targets_table.update_item(
        Key={"target_id": target["target_id"]},
        UpdateExpression=(
            "SET last_checked_at = :t, last_error = :e, last_status = :s, "
            "consecutive_failures = :f, repair_spend_usd = :r, degraded_at = :d"
        ),
        ExpressionAttributeValues={
            ":t": now,
            ":e": str(result["error"])[:500],
            # A refused target has not failed, and the row must not say it
            # has: `failed` is what 8d escalates and what the UI paints as a
            # broken extractor.
            ":s": blocked.BLOCKED if reason_kind == "blocked" else FAILED,
            ":f": DEGRADE_AFTER, ":r": _to_decimal(repair_spend), ":d": now,
        },
    )
    watches_table.update_item(
        Key={"watch_id": watch_id},
        UpdateExpression="SET #s = :s, degraded_at = :t, degraded_reason = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "degraded", ":t": now, ":r": str(result["error"])[:500],
        },
    )
    events.put_events(Entries=[{
        "Source": "schedule-ai-app.checker",
        "DetailType": "WatchDegraded",
        "EventBusName": os.environ["EVENT_BUS_NAME"],
        "Detail": json.dumps({
            "watch_id": watch_id,
            "target_id": target["target_id"],
            "url": target["url"],
            "prompt": watch.get("prompt", ""),
            "reason": str(result["error"])[:500],
            # Which email to write. Nothing about the plumbing cares, and the
            # user-facing wording is the whole difference: "your watch broke"
            # and "the shop shut us out" ask for different things from the
            # person reading them.
            "reason_kind": reason_kind,
            "repair_spend_usd": round(repair_spend, 4),
            "degraded_at": now,
        }),
    }])
    return {"checked": True, "status": "degraded", "error": result["error"],
            "reason_kind": reason_kind}


def lambda_handler(event, context):
    """One tick. Two shapes of invocation, and the difference is the whole
    point of Phase 9 step 3b.

        {"target_id": ...}   a condition watch -- read this target, judge it
        {"watch_id":  ...}   a time-triggered watch -- the firing IS the event

    The second has no target to read, no page to fetch and nothing to compare;
    the schedule going off is the entire trigger. Dispatching on which key is
    present is what lets a reminder exist without inventing a target row that
    describes nothing -- see `docs/phase-9-watch-kinds.md` §8, and Phase 5 on
    why a table that lies is worse than a branch.
    """
    if "target_id" not in event:
        return _fire_on_time(event["watch_id"])

    target_id = event["target_id"]

    targets_table = dynamodb.Table(os.environ["WATCH_TARGETS_TABLE"])
    watches_table = dynamodb.Table(os.environ["WATCHES_TABLE"])

    target = targets_table.get_item(Key={"target_id": target_id}).get("Item")
    if target is None:
        raise RuntimeError(f"No such target: {target_id}")

    watch_id = target["watch_id"]
    watch = watches_table.get_item(Key={"watch_id": watch_id}).get("Item")
    if watch is None:
        raise RuntimeError(f"Target {target_id} points at missing watch {watch_id}")

    # "active" is the only status that should ever be checked. Anything else is
    # triggered, paused, or still awaiting confirmation -- don't pay for a fetch
    # or a Claude call. Note this used to also allow "planning", which since
    # Phase 4 means the Planner is still running and no schedule should exist
    # yet; a tick in that state would check a half-written watch.
    if watch["status"] != "active":
        print(f"watch {watch_id} status={watch['status']}, skipping check")
        return {"skipped": True, "status": watch["status"]}

    now = datetime.now(timezone.utc).isoformat()

    # A repeating watch is the first thing here that does not stop by itself.
    # Everything else -- triggered, degraded -- reaches a terminal state and
    # stops billing; a vacancy watch would otherwise run until someone
    # remembered it. Checked before the fetch, so an expired watch costs one
    # DynamoDB read rather than a page.
    expires_at = watch.get("expires_at")
    if expires_at and now >= expires_at:
        return _expire(watches_table, watch, target, now)
    # Read outside the try: the failure path reports cost by fetch method, and
    # must not itself fail on an unbound name.
    fetch_method = target.get("fetch_method", "http")
    used_model = "extractor" not in target
    watch_condition = _from_decimal(watch["condition"])

    try:
        runner = _tier_model if used_model else _tier0
        result = runner(target, watch_condition, fetch_method)
    except blocked.Blocked as exc:
        # Not broken and not empty: we were not allowed to look. Handled before
        # anything else because every other path below would treat it as a
        # broken extractor and pay Haiku to repair a captcha page.
        return _record_blocked(targets_table, watches_table, watch, target,
                               exc, now, fetch_method)
    except SpecError as exc:
        # A malformed spec is a planning bug and will fail identically forever.
        result = {"status": FAILED, "value": None, "raw": None, "note": "",
                  "error": f"invalid extractor spec: {exc}", "condition_met": False}
    except Exception as exc:  # noqa: BLE001 -- record it, don't crash the tick
        print(f"check failed for {target_id}: {type(exc).__name__}: {exc}")
        targets_table.update_item(
            Key={"target_id": target_id},
            UpdateExpression=(
                "SET last_checked_at = :t, last_error = :e, last_status = :s"
            ),
            ExpressionAttributeValues={
                ":t": now,
                ":e": f"{type(exc).__name__}: {exc}"[:500],
                ":s": FAILED,
            },
        )
        _record_cost(fetch_method, used_model=False)
        return {"checked": False, "error": type(exc).__name__}

    _record_cost(fetch_method, used_model=used_model)

    # --- Tier 1: the model comes back, but only to repair -------------------
    repair_spend = float(_from_decimal(target.get("repair_spend_usd", 0)) or 0)
    failures = int(_from_decimal(target.get("consecutive_failures", 0)) or 0)

    if result["status"] == FAILED and not used_model and result.get("payload"):
        failures += 1
        interval = int(_from_decimal(watch.get("check_interval_min", 60)) or 60)
        affordable = cost.can_afford_repair(
            interval_min=interval, targets=1, fetch_method=fetch_method,
            repair_spend_usd=repair_spend,
        )
        if affordable:
            try:
                fixed = repair(
                    _from_decimal(target["extractor"]),
                    str(result["error"]),
                    target.get("extract_hint", ""),
                    result["payload"],
                    baseline=_from_decimal(target.get("verified_value")),
                )
            except Exception as exc:  # noqa: BLE001 -- a failed repair is data
                repair_spend += cost.repair_cost()
                print(f"repair failed for {target_id}: {type(exc).__name__}: {exc}")
            else:
                repair_spend += cost.repair_cost()
                print(f"repaired {target_id}: {fixed['extractor']}")
                targets_table.update_item(
                    Key={"target_id": target_id},
                    UpdateExpression=(
                        "SET extractor = :x, repaired_at = :t, "
                        "repair_spend_usd = :s, previous_extractor = :p"
                    ),
                    ExpressionAttributeValues={
                        ":x": _to_decimal(fixed["extractor"]),
                        ":t": now,
                        ":s": _to_decimal(repair_spend),
                        ":p": target["extractor"],
                    },
                )
                # Re-judge with the repaired spec against the page already in
                # hand. Not re-fetched: the repair was derived from this exact
                # markup, so anything else would be judging a different page.
                result = _judge_extraction(
                    fixed["extractor"], result["payload"], target, watch_condition)
                failures = 0
        else:
            print(f"{target_id} cannot afford a repair "
                  f"(spent ${repair_spend:.4f}); escalating")
            failures = DEGRADE_AFTER

    if result["status"] == FAILED and failures >= DEGRADE_AFTER:
        return _degrade(watches_table, targets_table, watch, target,
                        result, now, repair_spend)

    if result["status"] == FAILED:
        print(f"target={target_id} EXTRACTION FAILED: {result['error']}")
        targets_table.update_item(
            Key={"target_id": target_id},
            UpdateExpression=(
                "SET last_checked_at = :t, last_error = :e, last_status = :s, "
                "consecutive_failures = :f, repair_spend_usd = :r"
            ),
            ExpressionAttributeValues={
                ":t": now, ":e": str(result["error"])[:500], ":s": FAILED,
                ":f": failures, ":r": _to_decimal(repair_spend),
            },
        )
        return {"checked": True, "status": FAILED, "error": result["error"]}

    # Did the number actually move? Nothing in this system used to ask, so a
    # frozen feed -- a halt, a delisting, a plausible-but-dormant ticker -- read
    # `ok` forever, a `!=` watch never fired, no error was ever recorded, and
    # the owner would have concluded the price simply never moved. That is the
    # same silent rot `condition.py` refuses to permit for an unknown operator,
    # unguarded one layer up.
    #
    # Only the two facts are stored. Whether they *mean* anything is computed
    # at read time, because it depends on the interval, and the interval can be
    # changed by a PATCH after this row was written -- the same reason
    # `next_check_at` is computed rather than stored.
    moved = _reading_of(result) != _reading_of(target, stored=True)
    unchanged = 0 if moved else int(target.get("unchanged_checks") or 0) + 1

    targets_table.update_item(
        Key={"target_id": target_id},
        UpdateExpression=(
            "SET last_value = :v, last_raw = :r, last_checked_at = :t, "
            "last_note = :n, last_status = :s, consecutive_failures = :f, "
            "last_changed_at = :c, unchanged_checks = :u, last_items = :i, "
            # A read that worked ends any run of refusals, exactly as it ends
            # a run of failures. Blocking is probabilistic, so one good render
            # genuinely is the end of it.
            "consecutive_blocks = :f "
            "REMOVE last_error"
        ),
        ExpressionAttributeValues={
            ":f": 0,
            ":v": _to_decimal(result["value"]),
            ":r": result["raw"],
            ":t": now,
            ":n": result["note"],
            ":s": result["status"],
            ":i": _to_decimal(result.get("items") or []),
            ":c": now if moved else (target.get("last_changed_at") or now),
            ":u": unchanged,
        },
    )

    condition_met = bool(result["condition_met"])

    # A repeating watch reports each posting once and keeps running. That needs
    # per-item identity, so a watch with nothing to identify degrades to
    # one-shot -- the safe direction, because it can then fire once too few and
    # never once per tick forever.
    items = result.get("items") or []
    dedup = bool(watch.get("repeating")) and bool(items)
    if dedup:
        seen = set(target.get("seen_item_ids") or [])
        fresh = [i for i in items if i.get("id") not in seen]
    else:
        fresh = items

    # Condition AND novelty. Without the second half a vacancy watch would
    # email every tick for as long as the posting stayed on the page, which is
    # worse than the silence it was built to fix.
    fires = condition_met and (bool(fresh) if dedup else True)

    print(
        f"target={target_id} status={result['status']} value={result['value']!r} "
        f"met={condition_met} model={used_model}"
        + (f" new={len(fresh)}/{len(items)}" if dedup else "")
    )

    reported, rank_spend = fresh, 0.0
    if fires and dedup:
        # Tier 1 for a stream. The board matched on a keyword or two, so "new"
        # means new and roughly relevant -- and roughly is where it stops.
        # Judging happens **per notification, not per check**, which is what
        # keeps this from undoing Phase 8b: $0.19/month for a jobs watch firing
        # twice a day, against $16.42 to judge every tick.
        reported, rank_spend = _rank(watch, target, fresh)

        # Remember everything that appeared, including what ranking set aside.
        # Remembering only what was *reported* would bring every rejected
        # posting back on the next tick, to be paid for and rejected again, for
        # as long as it stayed on the board.
        _remember(targets_table, target_id, target, fresh)

    # Ranking can leave nothing worth saying. That is a successful outcome, not
    # a silent failure: the postings were seen, judged, remembered and paid
    # for, and none of them was the job.
    #
    # Only a deduplicating watch has items to be left with none of. A price
    # watch has no items at all, and gating it on this emptiness would silence
    # every one-shot watch in the system.
    notifying = fires and (bool(reported) if dedup else True)
    if fires and not notifying:
        print(f"nothing relevant for watch {watch_id} -- checked, not emailed")

    if notifying:
        if dedup:
            # Status stays `active`: the watch is not finished, it just spoke.
            # `trigger_count` counts emails, because that is what the expiry
            # message reads back to the user.
            watches_table.update_item(
                Key={"watch_id": watch_id},
                UpdateExpression=(
                    "SET last_triggered_at = :t, trigger_count = :n, "
                    "rank_spend_usd = :r"
                ),
                ExpressionAttributeValues={
                    ":t": now,
                    ":n": int(watch.get("trigger_count") or 0) + 1,
                    ":r": _to_decimal(
                        float(watch.get("rank_spend_usd") or 0) + rank_spend),
                },
            )
        else:
            try:
                watches_table.update_item(
                    Key={"watch_id": watch_id},
                    UpdateExpression="SET #s = :s, triggered_at = :t",
                    # Fire once, however many shops cross at the same moment.
                    ConditionExpression="#s = :active",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":s": "triggered", ":t": now, ":active": "active",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                if not _already_fired(exc):
                    raise
                print(f"watch {watch_id} was already triggered by another "
                      f"target on this tick -- not emailing twice")
                return {
                    "checked": True,
                    "status": result["status"],
                    "condition_met": True,
                    "notified": False,
                    "new_items": None,
                    "reported_items": None,
                    "last_value": result["value"],
                }

        # What every shop charges, so the email can answer "is that the
        # cheapest?" instead of reporting the one shop that happened to tick.
        # Empty for a single-target watch, and empty is the pre-2026-08-05
        # email exactly.
        readings = ([] if across.mode(watch_condition) != across.BEST
                    else _readings(targets_table, watch, target, result,
                                   watch_condition, now))

        # Announce it and move on. The Checker doesn't know or care that a
        # Notifier exists -- anything interested can subscribe to the bus.
        events.put_events(Entries=[{
            "Source": "schedule-ai-app.checker",
            "DetailType": "WatchTriggered",
            "EventBusName": os.environ["EVENT_BUS_NAME"],
            "Detail": json.dumps({
                "watch_id": watch_id,
                "target_id": target_id,
                "url": target["url"],
                "prompt": watch.get("prompt", ""),
                "last_value": result["raw"] if result["raw"] is not None
                else result["value"],
                "note": result["note"],
                # What appeared, not merely how many. The email used to say
                # "1" and link to the search page, leaving the user to go and
                # find the posting themselves.
                "items": reported,
                # Every shop's current price, best first, each carrying when it
                # was read and whether that is recent enough to trust. The
                # Notifier renders it; nothing downstream decides anything from
                # it, which is deliberate -- see shared/across.py on why the
                # condition is still judged against this target's own reading.
                "readings": readings,
                # Tells the Notifier whether to tear the schedules down. A
                # repeating watch must survive its own notification.
                "repeating": dedup,
                "triggered_at": now,
            }, default=_json_safe),
        }])
        print(f"CONDITION MET for watch {watch_id} -- event emitted"
              + (f" ({len(reported)} reported)" if dedup else ""))

    return {
        "checked": True,
        "status": result["status"],
        "condition_met": condition_met,
        "notified": notifying,
        "new_items": len(fresh) if dedup else None,
        "reported_items": len(reported) if dedup else None,
        "last_value": result["value"],
    }
