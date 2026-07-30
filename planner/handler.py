"""Planner Lambda handler: turn a plain-English request into a stored
plan, and stop there.

Since Phase 4 the Planner deliberately does NOT create schedules. It
writes the proposed targets, moves the watch to "proposed", and leaves
committing to the API's confirm endpoint. Two reasons:

- The Planner chooses `check_interval_min` itself, and that interval is
  the dominant cost in the whole system -- 5 minutes is roughly $50/month
  per target, 60 minutes roughly $4. That number should be seen before it
  starts billing, not after.
- Planning now touches no external resource, so it cannot half-fail and
  leak schedules that nothing points at. Every write here is to DynamoDB.

Invoked asynchronously by the api Lambda, which has already written the
watch row in "planning" status and handed a watch_id back to the client.
"""

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3

from plan import plan

dynamodb = boto3.resource("dynamodb")


def _to_decimal(value):
    """DynamoDB's boto3 resource API rejects native floats; it wants Decimal."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_decimal(v) for v in value]
    return value


def _fail(watches_table, watch_id: str, exc: Exception) -> dict:
    """Record why planning failed, on the watch itself.

    Deliberately does not re-raise. Lambda retries a failed async
    invocation twice, and a retry here would re-run the web search and
    write a second set of target rows. The error is also more useful on the
    row, where the UI can show it, than as a CloudWatch error metric -- and
    a watch stuck in "planning" forever is exactly how the orphaned rows
    found in earlier phases came to exist.
    """
    message = f"{type(exc).__name__}: {exc}"
    print(f"planning failed for {watch_id}: {message}")
    watches_table.update_item(
        Key={"watch_id": watch_id},
        UpdateExpression="SET #s = :s, plan_error = :e",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "failed", ":e": message[:500]},
    )
    return {"watch_id": watch_id, "status": "failed", "error": message}


def lambda_handler(event, context):
    watch_id = event["watch_id"]
    request = event["request"]

    watches_table = dynamodb.Table(os.environ["WATCHES_TABLE"])
    targets_table = dynamodb.Table(os.environ["WATCH_TARGETS_TABLE"])

    try:
        result = plan(request)
    except Exception as exc:  # noqa: BLE001 -- record on the row, never retry
        return _fail(watches_table, watch_id, exc)

    try:
        target_ids = []
        for target in result["targets"]:
            target_id = f"t_{uuid.uuid4().hex[:8]}"
            target_ids.append(target_id)
            targets_table.put_item(Item={
                "target_id": target_id,
                "watch_id": watch_id,
                "url": target["url"],
                "extract_hint": target["extract_hint"],
                "fetch_method": target.get("fetch_method", "http"),
            })

        # Flip to "proposed" only once the targets exist, so the status
        # never promises a plan the client cannot yet read.
        watches_table.update_item(
            Key={"watch_id": watch_id},
            UpdateExpression=(
                "SET #s = :s, #c = :c, check_interval_min = :i, planned_at = :t "
                "REMOVE plan_error"
            ),
            ExpressionAttributeNames={"#s": "status", "#c": "condition"},
            ExpressionAttributeValues={
                ":s": "proposed",
                ":c": _to_decimal(result["condition"]),
                ":i": _to_decimal(result["check_interval_min"]),
                ":t": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(watches_table, watch_id, exc)

    print(f"planned {watch_id}: {len(target_ids)} target(s), "
          f"interval {result['check_interval_min']}min")

    return {"watch_id": watch_id, "target_ids": target_ids, "status": "proposed"}
