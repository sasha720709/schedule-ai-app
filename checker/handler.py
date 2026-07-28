"""Checker Lambda handler. One invocation = one scheduled tick for one
target: read the target, check it, write the result back.

Emitting an event on a match (and the Notifier that reacts to it) is
Phase 3 -- for now a match just flips the watch's status to "triggered"."""

import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3

from check import check

dynamodb = boto3.resource("dynamodb")
events = boto3.client("events")


def _from_decimal(value):
    """DynamoDB hands back every number as Decimal, which json.dumps can't
    serialize. Undo it at the read boundary -- the inverse of the Planner's
    _to_decimal on write."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_decimal(v) for v in value]
    return value


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

    # Already fired (or paused by hand) -- don't pay for a Claude call.
    if watch["status"] not in ("active", "planning"):
        print(f"watch {watch_id} status={watch['status']}, skipping check")
        return {"skipped": True, "status": watch["status"]}

    now = datetime.now(timezone.utc).isoformat()

    try:
        result = check(
            target["url"], target["extract_hint"], _from_decimal(watch["condition"])
        )
    except Exception as exc:  # noqa: BLE001 -- record it, don't crash the tick
        print(f"check failed for {target_id}: {type(exc).__name__}: {exc}")
        targets_table.update_item(
            Key={"target_id": target_id},
            UpdateExpression="SET last_checked_at = :t, last_error = :e",
            ExpressionAttributeValues={":t": now, ":e": f"{type(exc).__name__}: {exc}"[:500]},
        )
        return {"checked": False, "error": type(exc).__name__}

    targets_table.update_item(
        Key={"target_id": target_id},
        UpdateExpression=(
            "SET last_value = :v, last_checked_at = :t, last_note = :n "
            "REMOVE last_error"
        ),
        ExpressionAttributeValues={
            ":v": result.get("last_value"),
            ":t": now,
            ":n": result.get("note", ""),
        },
    )

    condition_met = bool(result.get("condition_met"))
    print(
        f"target={target_id} value={result.get('last_value')!r} "
        f"met={condition_met} note={result.get('note')!r}"
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
                "last_value": result.get("last_value"),
                "note": result.get("note", ""),
                "triggered_at": now,
            }),
        }])
        print(f"CONDITION MET for watch {watch_id} -- event emitted")

    return {
        "checked": True,
        "condition_met": condition_met,
        "last_value": result.get("last_value"),
    }
