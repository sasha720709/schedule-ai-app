"""Planner Lambda handler. Wraps plan.py's plan() with persistence and
scheduling: writes the plan into DynamoDB, then creates one EventBridge
Scheduler schedule per target so the Checker starts running on its own."""

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3

from plan import plan

dynamodb = boto3.resource("dynamodb")
scheduler = boto3.client("scheduler")


def _to_decimal(value):
    """DynamoDB's boto3 resource API rejects native floats; it wants Decimal."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_decimal(v) for v in value]
    return value


def _rate_expression(minutes: int) -> str:
    """EventBridge Scheduler wants the unit singular at 1: 'rate(1 minute)'."""
    return f"rate({minutes} minute{'s' if minutes != 1 else ''})"


def _create_schedule(target_id: str, interval_min: int) -> str:
    response = scheduler.create_schedule(
        Name=f"schedule-ai-app-{target_id}",
        ScheduleExpression=_rate_expression(interval_min),
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": os.environ["CHECKER_FUNCTION_ARN"],
            "RoleArn": os.environ["SCHEDULER_ROLE_ARN"],
            "Input": json.dumps({"target_id": target_id}),
        },
    )
    return response["ScheduleArn"]


def lambda_handler(event, context):
    request = event["request"]
    result = plan(request)

    watch_id = f"w_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    interval_min = int(result["check_interval_min"])

    watches_table = dynamodb.Table(os.environ["WATCHES_TABLE"])
    targets_table = dynamodb.Table(os.environ["WATCH_TARGETS_TABLE"])

    target_ids = []
    for target in result["targets"]:
        target_id = f"t_{uuid.uuid4().hex[:8]}"
        target_ids.append(target_id)

        item = {
            "target_id": target_id,
            "watch_id": watch_id,
            "url": target["url"],
            "extract_hint": target["extract_hint"],
            "fetch_method": target.get("fetch_method", "http"),
        }
        # Write the row before the schedule exists, so the Checker can never
        # fire at a target it can't read; then rewrite it with the ARN.
        targets_table.put_item(Item=item)

        item["schedule_arn"] = _create_schedule(target_id, interval_min)
        targets_table.put_item(Item=item)

    watches_table.put_item(Item={
        "watch_id": watch_id,
        "user_id": "default",
        "prompt": request,
        "condition": _to_decimal(result["condition"]),
        "status": "active",
        "check_interval_min": _to_decimal(result["check_interval_min"]),
        "created_at": now,
    })

    return {"watch_id": watch_id, "target_ids": target_ids, "status": "active"}
