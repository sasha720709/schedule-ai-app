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

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3

import cost
from fetch import fetch_raw
from plan import build_extractor, search

dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")


def _to_decimal(value):
    """DynamoDB's boto3 resource API rejects native floats; it wants Decimal."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_decimal(v) for v in value]
    return value


def _fetch(url: str, fetch_method: str) -> str:
    """Open the page, the way the Checker will. Markup, not text.

    The Planner had no fetching at all before Phase 8b: it recommended URLs it
    had never opened, which is exactly how Amazon and Best Buy became runtime
    failures rather than plan-time ones.
    """
    if fetch_method != "browser":
        return fetch_raw(url)

    response = lambda_client.invoke(
        FunctionName=os.environ["FETCHER_FUNCTION_ARN"],
        Payload=json.dumps({"url": url}),
    )
    payload = json.loads(response["Payload"].read())
    if "FunctionError" in response:
        raise RuntimeError(f"browser fetch failed: {payload}")
    if "html" not in payload:
        raise RuntimeError("browser fetch returned no `html`; redeploy the Fetcher")
    return payload["html"]


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
        result = search(request)
    except Exception as exc:  # noqa: BLE001 -- record on the row, never retry
        return _fail(watches_table, watch_id, exc)

    try:
        condition = result["condition"]
        # "value" watches read something already on the page and wait for it to
        # change; "presence" watches wait for something to appear that is not
        # there yet. The second kind cannot be verified by finding its value,
        # because not having one is its whole premise -- see build_extractor.
        shape = result.get("watch_shape", "value")
        target_ids, verified, rejected = [], [], []

        for target in result["targets"]:
            url = target["url"]
            fetch_method = target.get("fetch_method", "http")
            now = datetime.now(timezone.utc).isoformat()

            # Verify before storing. A target whose value cannot be read today
            # will not start being readable later, and offering it would mean
            # the plan card promising a reading nobody has ever seen.
            try:
                raw = _fetch(url, fetch_method)
                built = build_extractor(
                    url, target["extract_hint"], condition, raw, shape=shape)
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"
                print(f"rejected target {url}: {message}")
                rejected.append({"url": url, "reason": message[:300]})
                continue

            target_id = f"t_{uuid.uuid4().hex[:8]}"
            target_ids.append(target_id)
            verified.append(fetch_method)
            targets_table.put_item(Item={
                "target_id": target_id,
                "watch_id": watch_id,
                "url": url,
                # The hint stops being the reading instruction and becomes the
                # repair instruction. That is the whole change, in one line.
                "extract_hint": target["extract_hint"],
                "fetch_method": fetch_method,
                "extractor": _to_decimal(built["extractor"]),
                "verified_value": _to_decimal(built["verified_value"]),
                "verified_raw": built["verified_raw"],
                "verified_at": now,
            })

        if not target_ids:
            raise RuntimeError(
                "no target could be verified: "
                + "; ".join(f"{r['url']} ({r['reason']})" for r in rejected)
            )

        # The model picks check_interval_min unaided, and has been observed
        # proposing 10, 20 and 30 minutes for near-identical requests. Nothing
        # stops it proposing 1. Clamp it up to whatever the monthly budget
        # actually affords -- and keep what it asked for, so the plan card can
        # show that its suggestion was overridden and why.
        #
        # uses_model is now False: this is what makes Phase 8b visible in the
        # bill. The same $5 budget that forced ~51-minute intervals when every
        # tick paid for Haiku now permits the floor, with no constant changed.
        proposed = int(result["check_interval_min"])
        browser = any(method == "browser" for method in verified)
        floor = cost.min_interval_for_budget(
            targets=len(target_ids),
            fetch_method="browser" if browser else "http",
            uses_model=False,
        )
        interval = max(proposed, floor)

        # Flip to "proposed" only once the targets exist, so the status
        # never promises a plan the client cannot yet read.
        watches_table.update_item(
            Key={"watch_id": watch_id},
            UpdateExpression=(
                "SET #s = :s, #c = :c, check_interval_min = :i, "
                "planner_interval_min = :p, min_interval_min = :f, planned_at = :t "
                "REMOVE plan_error"
            ),
            ExpressionAttributeNames={"#s": "status", "#c": "condition"},
            ExpressionAttributeValues={
                ":s": "proposed",
                ":c": _to_decimal(result["condition"]),
                ":i": _to_decimal(interval),
                ":p": _to_decimal(proposed),
                ":f": _to_decimal(floor),
                ":t": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(watches_table, watch_id, exc)

    clamped = " (raised from %dmin to fit the budget)" % proposed if interval > proposed else ""
    print(f"planned {watch_id}: {len(target_ids)} target(s), "
          f"interval {interval}min{clamped}")

    return {
        "watch_id": watch_id,
        "target_ids": target_ids,
        "status": "proposed",
        "check_interval_min": interval,
    }
