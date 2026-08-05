"""Notifier Lambda: reacts to a WatchTriggered event by emailing the owner,
then tearing down that watch's schedules so it stops checking.

Deletion happens only after the email is away. If SES fails, the Lambda
raises, the schedules survive, and the watch keeps ticking -- better a
duplicate check than a match nobody hears about."""

import os
from datetime import datetime, timezone
from urllib.parse import urljoin

import boto3
from boto3.dynamodb.conditions import Key

ses = boto3.client("ses")
dynamodb = boto3.resource("dynamodb")
scheduler = boto3.client("scheduler")


def _all_targets(targets_table, watch_id: str) -> list:
    """Every target row, following pagination.

    A single `query()` returns at most 1MB. With 1-3 targets per watch that
    limit is unreachable today, which is exactly the kind of bug that survives
    for years and then quietly keeps half a watch's schedules alive -- billing
    forever -- the first time something creates a bigger watch. Listed in the
    known gaps since Phase 3; closing it costs four lines.
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


def _delete_schedules(watch_id: str) -> list:
    """Every schedule this watch owns, whichever shape it uses.

    A time-triggered watch stores `targets: []` and owns its schedule directly
    (`docs/phase-9-watch-kinds.md` §8), so a teardown that only walked targets
    would delete nothing and leave it billing. The watch-level delete is tried
    for every watch; on a condition watch there is no such schedule and the
    call is a no-op, which is cheaper than storing a flag to decide with.
    """
    targets_table = dynamodb.Table(os.environ["WATCH_TARGETS_TABLE"])

    deleted = []
    for target in _all_targets(targets_table, watch_id):
        name = f"schedule-ai-app-{target['target_id']}"
        try:
            scheduler.delete_schedule(Name=name)
            deleted.append(name)
        except scheduler.exceptions.ResourceNotFoundException:
            # Already gone -- fine, this is the state we wanted anyway.
            pass

        # Clear the pointer as well as the schedule. The field used to be left
        # behind, so every triggered watch's rows pointed at schedules that no
        # longer existed -- harmless while nothing read it back, but a table
        # that lies about the world is a debugging tax on everything after it.
        #
        # Swallowed on purpose, and this was learned the hard way. Adding this
        # call in Phase 5 introduced a dynamodb:UpdateItem the Notifier's role
        # did not have. The email had already gone out, so the AccessDenied
        # failed the whole invocation *after* the side effect that matters --
        # and EventBridge dutifully retried, which means a second email, and a
        # third, up to the retry cap. Tidying a field is cosmetic; it must
        # never be able to re-notify a human. Anything after the email is
        # best-effort by construction, not by permission.
        try:
            targets_table.update_item(
                Key={"target_id": target["target_id"]},
                UpdateExpression="REMOVE schedule_arn",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"could not clear schedule_arn on {target['target_id']}: "
                  f"{type(exc).__name__}: {exc}")

    try:
        scheduler.delete_schedule(Name=f"schedule-ai-app-{watch_id}")
        deleted.append(f"schedule-ai-app-{watch_id}")
    except scheduler.exceptions.ResourceNotFoundException:
        pass  # a condition watch has none, which is the common case

    return deleted


def _format_items(items: list, base_url: str) -> str:
    """The postings themselves, as text and link.

    This replaces the single worst line in the product. A `count` extractor
    returned an integer, so a triggered vacancy watch emailed the user the word
    **"1"** and a link to the *search page* -- leaving them to go and find the
    posting, which is most of the work they asked to be spared.

    Links are joined here because this is the first place that knows both the
    href and the page it came from. `extract.py` only ever sees a payload, and
    a relative href is meaningless without the URL it was written on.
    """
    lines = []
    for item in items:
        text = (item.get("text") or "").strip() or "(untitled)"
        href = (item.get("href") or "").strip()
        score = item.get("score")
        # Ranked items lead with their score, because that is the thing being
        # scanned for. Unranked ones look exactly as they did before -- ranking
        # never blocks a notification, so an email may carry both.
        head = f"[{score}/10] " if isinstance(score, int) else ""
        lines.append(f"  {head}{text}")
        why = (item.get("why") or "").strip()
        if why:
            lines.append(f"    {why}")
        if href:
            lines.append(f"    {urljoin(base_url, href)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _age(stamp, now=None) -> str:
    """"just now", "40 min ago", "6 h ago" -- for a price read at another tick."""
    if not isinstance(stamp, str) or not stamp:
        return "at an unknown time"
    try:
        taken = datetime.fromisoformat(stamp)
    except ValueError:
        return "at an unknown time"
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=timezone.utc)
    minutes = int(((now or datetime.now(timezone.utc)) - taken).total_seconds() // 60)
    if minutes < 2:
        return "just now"
    if minutes < 90:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"


def _format_readings(readings: list) -> str:
    """Every shop, best first -- the answer to "is that actually the cheapest?".

    The watch knew this all along and never said it. A three-shop watch emailed
    one price and a link, and the obvious next question was one the system
    could have answered for free.

    **When each price was read is part of the price.** Shops are checked on the
    same interval but not in the same second, and a shop that has gone quiet is
    carried here rather than dropped -- showing two shops when the user asked
    about three is the silent-omission failure this project keeps removing.
    """
    lines = []
    for reading in readings:
        name = reading.get("shop") or reading.get("target_id") or "(a shop)"
        currency = reading.get("currency") or ""
        price = reading.get("value")
        amount = f"{price:g}" if isinstance(price, (int, float)) else str(price)
        when = _age(reading.get("at"))
        note = "  -- not confirmed recently" if reading.get("stale") else ""
        lines.append(f"  {amount}{' ' + currency if currency else ''}"
                     f"  {name}  ({_delivery(reading)}, read {when}){note}")
        href = (reading.get("url") or "").strip()
        if href:
            lines.append(f"    {href}")

    # Measured 2026-08-05: no shop probed publishes a delivery cost on a search
    # page at all, so this line is the usual case rather than the exception.
    # Saying "cheapest" of numbers that exclude delivery is the wrong claim,
    # and it is the one the roadmap set out to stop -- an item 50 cheaper with
    # 60 delivery is not cheaper. The fix is not arithmetic nobody can do, it
    # is not claiming to have done it.
    if any(_state(r) == "unknown" for r in readings):
        lines.append("")
        lines.append("  Delivery is not included where it says so above — "
                     "these shops do not publish it before checkout.")
    return "\n".join(lines)


def _state(reading) -> str:
    return ((reading.get("shipping") or {}).get("state")) or "unknown"


def _delivery(reading) -> str:
    """What delivery adds, in words, for one line of the summary."""
    ship = reading.get("shipping") or {}
    state = ship.get("state")
    if state == "free":
        return "free delivery"
    if state == "extra":
        amount = ship.get("amount")
        return (f"includes {amount:g} delivery"
                if isinstance(amount, (int, float)) and amount
                else "delivery included")
    return "before delivery"


def _format_email(detail: dict) -> tuple:
    prompt = detail.get("prompt", "(unknown request)")
    items = detail.get("items") or []
    repeating = bool(detail.get("repeating"))
    url = detail.get("url", "(unknown)")

    if repeating and items:
        best = max((i["score"] for i in items
                    if isinstance(i.get("score"), int)), default=None)
        rating = f" (best {best}/10)" if best is not None else ""
        subject = f"{len(items)} new{rating}: {prompt[:44]}"
    else:
        subject = f"Watch triggered: {prompt[:60]}"

    found = (_format_items(items, url) if items
             else f"  {detail.get('last_value') or '(no value recorded)'}")

    # Only a multi-shop watch has this, and its absence is the email exactly as
    # it read before 2026-08-05.
    readings = detail.get("readings") or []
    across_shops = (f"\nAcross every shop being watched:\n"
                    f"{_format_readings(readings)}\n") if readings else ""

    body = f"""Your watch just came true.

What you asked for:
  {prompt}

What was found:
{found}

Where:
  {url}
{across_shops}
Why this counts:
  {detail.get('note') or '(no explanation recorded)'}

Checked at {detail.get('triggered_at', 'unknown time')}.

{"This watch is still running -- it will keep looking and will only tell you about things it has not already shown you." if repeating else "This watch has now stopped checking."}

-- schedule-ai-app
"""
    return subject, body


def _format_degraded_email(detail: dict) -> tuple:
    """A watch that broke is a different message from a watch that fired.

    Conflating them would be the same mistake the extraction engine makes when
    it collapses `unavailable` into `failed`: two states that need opposite
    responses, reported identically. One says "the thing you wanted happened",
    the other says "I can no longer tell you whether it happened".
    """
    prompt = detail.get("prompt", "(unknown request)")

    # A watch that ran out its term is not a watch that broke. Reusing this
    # event type is a plumbing decision -- both need "email, then tear down" --
    # but the wording the user reads has to tell the truth.
    if detail.get("reason_kind") == "expired":
        fired = int(detail.get("trigger_count") or 0)
        return (
            f"Watch finished: {prompt[:60]}",
            f"""Your watch reached the end of its term and has stopped.

What you asked for:
  {prompt}

It told you about {fired} thing{"" if fired == 1 else "s"} while it ran.

Nothing is broken -- a watch that keeps running rather than stopping at its
first result is given a term so a forgotten one cannot go on checking for
years. Create it again if you still want it.

Where:
  {detail.get('url', '(unknown)')}

Ended at {detail.get('degraded_at', 'unknown time')}.
""")

    # Being shut out by a shop is not a broken watch, and the difference is
    # the whole reason the state exists: the owner can do something about the
    # first -- wait, or watch somewhere else -- and nothing at all about a
    # redesigned page. Saying "an automatic repair was attempted" here would
    # also be a lie: no repair is ever attempted on a refusal, on purpose.
    if detail.get("reason_kind") == "blocked":
        return (
            f"A shop stopped letting us look: {prompt[:44]}",
            f"""Your watch has been stopped because the site refused it, repeatedly.

What you asked for:
  {prompt}

What happened:
  {detail.get('reason', '(no reason recorded)')}

Nothing about the watch is broken. Large shops turn automated requests away
sometimes, and the block is often temporary and often specific to where the
request comes from -- so the same watch may work again in a day, or from a
different shop entirely.

Nothing was spent trying to repair it: there was nothing to repair.

Where:
  {detail.get('url', '(unknown)')}

Checking has stopped, so this is no longer costing anything. Re-create the
watch to try again.

Detected at {detail.get('degraded_at', 'unknown time')}.
""")

    subject = f"Watch stopped working: {prompt[:60]}"
    body = f"""Your watch can no longer read its target, so it has been stopped.

What you asked for:
  {prompt}

What went wrong:
  {detail.get('reason', '(no reason recorded)')}

Where:
  {detail.get('url', '(unknown)')}

This usually means the site was redesigned. An automatic repair was attempted
and did not work, which cost about ${detail.get('repair_spend_usd', 0):.3f}.

Checking has stopped, so this is no longer costing anything. Re-create the
watch when you want it back -- it will read the page fresh and build a new
extractor.

Detected at {detail.get('degraded_at', 'unknown time')}.
"""
    return subject, body


def lambda_handler(event, context):
    detail = event["detail"]
    watch_id = detail["watch_id"]

    degraded = event.get("detail-type") == "WatchDegraded"
    subject, body = (_format_degraded_email(detail) if degraded
                     else _format_email(detail))
    address = os.environ["NOTIFY_EMAIL"]

    ses.send_email(
        Source=address,
        Destination={"ToAddresses": [address]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}},
        },
    )
    print(f"emailed {address} for watch {watch_id}")

    # A repeating watch survives its own notification -- that is the entire
    # difference. Tearing its schedules down here would turn "tell me about
    # every new vacancy" back into "tell me about the first one", silently and
    # with the email already sent.
    if not degraded and detail.get("repeating"):
        print(f"watch {watch_id} is repeating -- schedules left in place")
        return {"notified": True, "watch_id": watch_id, "schedules_deleted": []}

    deleted = _delete_schedules(watch_id)
    print(f"deleted {len(deleted)} schedule(s) for watch {watch_id}: {deleted}")

    return {"notified": True, "watch_id": watch_id, "schedules_deleted": deleted}
