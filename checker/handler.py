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

`unavailable` and `failed` are written to different fields on purpose. A watch
that has quietly stopped being able to read its target must be distinguishable
from one that is patiently waiting, or it will wait forever.
"""

import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3

import cost
from condition import ConditionError, evaluate
from extract import FAILED, OK, UNAVAILABLE, SpecError, extract, plausible
from check import fetch_raw, fetch_text, judge
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


def _fetch(url: str, fetch_method: str, *, markup: bool) -> str:
    """Plain GET by default. The Planner marks a target "browser" when the page
    needs JavaScript to render, which costs a second Lambda and a few seconds --
    so it's opt-in per target, not the default.

    `markup=True` returns HTML for a compiled extractor; `markup=False` returns
    reduced text for the model, where every character is billed.
    """
    if fetch_method != "browser":
        return fetch_raw(url) if markup else fetch_text(url)

    response = lambda_client.invoke(
        FunctionName=os.environ["FETCHER_FUNCTION_ARN"],
        Payload=json.dumps({"url": url}),
    )
    payload = json.loads(response["Payload"].read())

    if "FunctionError" in response:
        raise RuntimeError(f"browser fetch failed: {payload}")

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


def _degrade(watches_table, targets_table, watch: dict, target: dict,
             result: dict, now: str, repair_spend: float) -> dict:
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
            ":t": now, ":e": str(result["error"])[:500], ":s": FAILED,
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
            "repair_spend_usd": round(repair_spend, 4),
            "degraded_at": now,
        }),
    }])
    return {"checked": True, "status": "degraded", "error": result["error"]}


def lambda_handler(event, context):
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
    # Read outside the try: the failure path reports cost by fetch method, and
    # must not itself fail on an unbound name.
    fetch_method = target.get("fetch_method", "http")
    used_model = "extractor" not in target
    watch_condition = _from_decimal(watch["condition"])

    try:
        runner = _tier_model if used_model else _tier0
        result = runner(target, watch_condition, fetch_method)
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

    targets_table.update_item(
        Key={"target_id": target_id},
        UpdateExpression=(
            "SET last_value = :v, last_raw = :r, last_checked_at = :t, "
            "last_note = :n, last_status = :s, consecutive_failures = :f "
            "REMOVE last_error"
        ),
        ExpressionAttributeValues={
            ":f": 0,
            ":v": _to_decimal(result["value"]),
            ":r": result["raw"],
            ":t": now,
            ":n": result["note"],
            ":s": result["status"],
        },
    )

    condition_met = bool(result["condition_met"])
    print(
        f"target={target_id} status={result['status']} value={result['value']!r} "
        f"met={condition_met} model={used_model}"
    )

    if condition_met:
        watches_table.update_item(
            Key={"watch_id": watch_id},
            UpdateExpression="SET #s = :s, triggered_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "triggered", ":t": now},
        )
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
                "triggered_at": now,
            }),
        }])
        print(f"CONDITION MET for watch {watch_id} -- event emitted")

    return {
        "checked": True,
        "status": result["status"],
        "condition_met": condition_met,
        "last_value": result["value"],
    }
