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

import classify as classify_mod
import cost
import kinds
from fetch import fetch_raw
from plan import resolve_relative_condition

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


def _browser_fetch(url: str) -> str:
    """Render the page in the Fetcher Lambda and return its markup.

    The Planner had no fetching at all before Phase 8b: it recommended URLs it
    had never opened, which is exactly how Amazon and Best Buy became runtime
    failures rather than plan-time ones.
    """
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
        # Which kind this is, decided once and cheaply, before anything
        # expensive runs. A quote never reaches the web search at all -- the
        # answer is a registry lookup, and paying Sonnet with search to
        # discover that was the single clearest waste in planning.
        decision = classify_mod.classify(request, kinds.names())
        kind = kinds.get(decision["kind"])
        result = kind.plan(request, decision.get("symbol"))
    except Exception as exc:  # noqa: BLE001 -- record on the row, never retry
        return _fail(watches_table, watch_id, exc)

    try:
        condition = result["condition"]
        # "goes down from the current" has no threshold until something has
        # actually been read. Resolved below, once a target verifies.
        relative_pct = result.get("relative_change_pct")
        baseline = None
        target_ids, verified, rejected, windows = [], [], [], []

        for target in result["targets"]:
            now = datetime.now(timezone.utc).isoformat()

            try:
                url = target.get("url")
                resolved = kind.resolve(
                    target, condition,
                    fetch_http=lambda u=url: fetch_raw(u),
                    fetch_browser=lambda u=url: _browser_fetch(u),
                )
                url = resolved["url"]
                print(f"{url} -> {resolved['fetch_method']}: {resolved['why']}")
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"
                name = target.get("url") or target.get("symbol", "known source")
                print(f"rejected target {name}: {message}")
                rejected.append({"url": name, "reason": message[:300]})
                continue

            if baseline is None:
                baseline = resolved["verified_value"]

            target_id = f"t_{uuid.uuid4().hex[:8]}"
            target_ids.append(target_id)
            verified.append(resolved["fetch_method"])
            windows.append(resolved.get("window"))
            targets_table.put_item(Item={
                "target_id": target_id,
                "watch_id": watch_id,
                "url": url,
                # The hint stops being the reading instruction and becomes the
                # repair instruction. That is the whole change, in one line.
                "extract_hint": resolved["extract_hint"],
                "fetch_method": resolved["fetch_method"],
                "extractor": _to_decimal(resolved["extractor"]),
                "verified_value": _to_decimal(resolved["verified_value"]),
                "verified_raw": resolved["verified_raw"],
                "verified_at": now,
                # Which slice of the week this target's schedule may run in.
                # Absent means continuously; see shared/schedules.py.
                **({"schedule_window": resolved["window"]}
                   if resolved.get("window") else {}),
            })

        # Only now is there a real number to be relative to. Doing this before
        # the fetch is what produced `price < 313.93` for "tell me when Apple
        # goes down" -- 5% below a stale figure from search results, while the
        # page said $333.43.
        condition = resolve_relative_condition(condition, relative_pct, baseline)

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
        # A window makes a watch cheaper by running it less. If the targets
        # disagree, take the continuous case: overstating cost refuses an
        # interval that was affordable, which is recoverable; understating it
        # creates a schedule that bills more than the budget allowed.
        window = windows[0] if len(set(windows)) == 1 else None
        floor = cost.min_interval_for_budget(
            targets=len(target_ids),
            fetch_method="browser" if browser else "http",
            uses_model=False,
            window=window,
        )
        interval = max(proposed, floor)

        # Flip to "proposed" only once the targets exist, so the status
        # never promises a plan the client cannot yet read.
        watches_table.update_item(
            Key={"watch_id": watch_id},
            UpdateExpression=(
                "SET #s = :s, #c = :c, check_interval_min = :i, "
                "planner_interval_min = :p, min_interval_min = :f, "
                # Stored so a wrong fork is visible on the plan card before the
                # user confirms, rather than discovered weeks later.
                "watch_kind = :k, planned_at = :t "
                "REMOVE plan_error"
            ),
            ExpressionAttributeNames={"#s": "status", "#c": "condition"},
            ExpressionAttributeValues={
                ":s": "proposed",
                ":k": kind.name,
                ":c": _to_decimal(condition),
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
