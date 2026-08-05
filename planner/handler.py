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

import across
import classify as classify_mod
import cost
import kinds
import schedules
from fetch import fetch_raw
from plan import resolve_relative_condition
from questions import build as build_questions

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


# What a time-triggered watch stores instead of targets and a condition.
_TIME_FIELDS = ("fire_at", "fire_timezone", "reminder_title", "reminder_note",
                "repeat")


def _baseline(baselines: list, condition: dict) -> dict:
    """Which reading "the current price" means, across several shops.

    Taking the first one was fine while a watch had a single target and wrong
    the moment it had three: "10% cheaper than now" measured against whichever
    shop's page happened to load first, and the other two were then judged
    against a threshold derived from a shop they have nothing to do with.

    The best reading is the honest answer, and it is also the conservative one
    in both directions. For a `<` watch the baseline is the cheapest offer, so
    the threshold sits lower and the watch fires later; for a `>` watch it is
    the dearest, so the threshold sits higher and it fires later. Measuring at
    the same end of the range the condition is judged at is the whole rule.
    """
    if not baselines:
        return {"value": None, "at": None, "source": None}
    direction = across.direction(condition)
    if direction is None or len(baselines) == 1:
        return baselines[0]
    numeric = [b for b in baselines
               if isinstance(b["value"], (int, float))
               and not isinstance(b["value"], bool)]
    if not numeric:
        return baselines[0]
    pick = max if direction == "max" else min
    return pick(numeric, key=lambda b: b["value"])


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
        target_ids, verified, rejected, windows = [], [], [], []
        verified_items = []
        # One entry per verified target, so the baseline can be taken from the
        # best of them rather than from whichever shop answered first.
        baselines = []

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

            # The condition was written before anything was fetched, so its
            # currency is null on every quote planned so far while the payload
            # says USD or ILS. A bare "7377" for Bank Leumi is a number nobody
            # can sanity-check.
            if resolved.get("currency") and not condition.get("currency"):
                condition["currency"] = resolved["currency"]

            # A shop pricing in another currency cannot be compared with this
            # threshold, and there is no honest way to make it one. Converting
            # needs an exchange rate, which is a second thing to be wrong
            # about -- quietly, in an email about money.
            #
            # This was not a hypothetical. The watch has exactly one condition
            # and it was applied to every target, so `price < 2000` (shekels)
            # was true of Amazon's $34.99 and the watch would have fired on it.
            # The roadmap says currencies are never compared; until today the
            # code did not agree. Refusing the target says so on the plan card,
            # where "Amazon prices in USD" is information, rather than creating
            # one that can only be wrong.
            if not across.comparable(resolved, condition):
                message = (
                    f"prices in {resolved['currency']}, and this watch is "
                    f"about {condition['currency']} -- no exchange rate is "
                    f"used, so the two cannot be compared"
                )
                name = target.get("shop") or url
                print(f"rejected target {name}: {message}")
                rejected.append({"url": name, "reason": message})
                continue

            baselines.append({
                "value": resolved["verified_value"],
                "at": now,
                # Was the market open when this number was read? The owner
                # accepted the previous close as a baseline, which makes saying
                # which one it was the whole remaining job.
                "source": (
                    "live"
                    if schedules.in_window(datetime.now(timezone.utc),
                                           resolved.get("window"))
                    else "previous_close"
                ),
            })

            target_id = f"t_{uuid.uuid4().hex[:8]}"
            target_ids.append(target_id)
            verified.append(resolved["fetch_method"])
            verified_items.append(resolved.get("verified_items") or [])
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
                # Which instrument the symbol actually resolved to. Absent for
                # every kind but `quote`. Shown on the plan card so that "AMX"
                # coming back as América Móvil's NYSE listing in dollars is
                # visible before confirming rather than never.
                **{k: resolved[k]
                   for k in ("instrument_name", "exchange", "currency", "shop")
                   if resolved.get(k)},
                # What the extractor matched at plan time, and how many items
                # the list holds in total. A count verified at zero is honest
                # and completely uninformative on its own; "47 listed today, 3
                # of which match" is what lets a person judge the filter
                # before paying for a schedule.
                **({"verified_items": _to_decimal(resolved["verified_items"])}
                   if resolved.get("verified_items") else {}),
                **({"unfiltered_count": _to_decimal(resolved["unfiltered_count"])}
                   if resolved.get("unfiltered_count") is not None else {}),
            })

        # Questions worth asking, built from what the search actually returned
        # rather than written in advance. A generic form asks about hours that
        # no posting mentions and a city every result already shares; questions
        # derived from the live list can only ask what discriminates.
        #
        # Never required, and never allowed to fail a plan: no questions is a
        # perfectly good outcome and is also what any error produces.
        found = [item for row in verified_items for item in row]
        if "questions" in result:
            # A kind that knows its own open question asks it directly. The
            # searching path derives questions from what was found; a reminder
            # found nothing and its one fork -- once or every day -- is not
            # discoverable from data. Same shape on the wire either way, so the
            # plan card and `confirm` need no second vocabulary.
            questions, questions_spend = result["questions"], 0.0
        else:
            questions, questions_spend = build_questions(request, found)
        if questions:
            print(f"asked {len(questions)} question(s), "
                  f"${questions_spend:.4f}")

        # Only now is there a real number to be relative to. Doing this before
        # the fetch is what produced `price < 313.93` for "tell me when Apple
        # goes down" -- 5% below a stale figure from search results, while the
        # page said $333.43.
        # More than one shop and an ordered operator means the watch has a
        # single best reading, and saying so on the row is what lets the plan
        # card, the email and the Checker all talk about the same number
        # instead of three unrelated ones. See shared/across.py -- notably why
        # this does *not* move where the condition is evaluated.
        if len(baselines) > 1 and across.direction(condition):
            condition["across"] = across.BEST

        chosen = _baseline(baselines, condition)
        condition = resolve_relative_condition(
            condition, relative_pct, chosen["value"],
            baseline_at=chosen["at"], baseline_source=chosen["source"])

        # A time-triggered watch has nothing to verify, on purpose: the
        # schedule firing is the event. Every other kind must produce at least
        # one working target or the plan is not a plan.
        if not target_ids and kind.trigger != "time":
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

        if kind.trigger == "time":
            # Nothing is polled, so the floor -- a monthly allowance divided by
            # a per-check cost -- has nothing to divide. Skipping it is not a
            # loophole: a watch that runs exactly once cannot exceed a monthly
            # budget however the arithmetic is arranged.
            interval = floor = proposed
        else:
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
                "watch_kind = :k, planned_at = :t, repeating = :r, "
                "questions = :q, rejected = :rej"
                # Aliased, every one of them. `repeat` is a DynamoDB reserved
                # word and writing it bare fails the whole update with a
                # ValidationException -- caught only by planning a real
                # reminder, because the test double does not know the reserved
                # list. Aliasing all of them costs nothing and removes the
                # need to remember which words are on it.
                + "".join(f", #{k} = :{k}" for k in _TIME_FIELDS if k in result)
                + " REMOVE plan_error"
            ),
            ExpressionAttributeNames={
                "#s": "status", "#c": "condition",
                **{f"#{k}": k for k in _TIME_FIELDS if k in result},
            },
            ExpressionAttributeValues={
                ":s": "proposed",
                ":k": kind.name,
                # A property of the kind, resolved here so it is on the row --
                # visible on the plan card, and changeable per watch later
                # without touching the class. A vacancy is a stream; a price
                # crossing a threshold is an event. See Kind.repeating.
                ":r": bool(kind.repeating),
                ":q": _to_decimal(questions),
                # Which shops were looked at and not kept, and why. A watch
                # that quietly became two shops instead of three is the
                # silence-reads-as-broken failure again: the user asked about
                # Amazon, Amazon is not there, and nothing said so. Stored
                # rather than only printed, because a CloudWatch log is not a
                # place a person will look.
                ":rej": _to_decimal(rejected),
                ":c": _to_decimal(condition),
                ":i": _to_decimal(interval),
                ":p": _to_decimal(proposed),
                ":f": _to_decimal(floor),
                ":t": datetime.now(timezone.utc).isoformat(),
                # A time-triggered watch's whole plan: when, where in the
                # world, and what to put on the calendar. Absent for every
                # other kind, which is why they are written as their own
                # clause rather than folded into `condition` -- a reminder has
                # no condition, and pretending otherwise is how the `targets`
                # assumption got made in the first place.
                **{f":{k}": result[k] for k in _TIME_FIELDS if k in result},
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
