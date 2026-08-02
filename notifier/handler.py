"""Notifier Lambda: reacts to a WatchTriggered event by emailing the owner,
then tearing down that watch's schedules so it stops checking.

Deletion happens only after the email is away. If SES fails, the Lambda
raises, the schedules survive, and the watch keeps ticking -- better a
duplicate check than a match nobody hears about."""

import os

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
    return deleted


def _format_email(detail: dict) -> tuple:
    prompt = detail.get("prompt", "(unknown request)")
    subject = f"Watch triggered: {prompt[:60]}"

    body = f"""Your watch just came true.

What you asked for:
  {prompt}

What was found:
  {detail.get('last_value') or '(no value recorded)'}

Where:
  {detail.get('url', '(unknown)')}

Why this counts:
  {detail.get('note') or '(no explanation recorded)'}

Checked at {detail.get('triggered_at', 'unknown time')}.

This watch has now stopped checking.

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

    deleted = _delete_schedules(watch_id)
    print(f"deleted {len(deleted)} schedule(s) for watch {watch_id}: {deleted}")

    return {"notified": True, "watch_id": watch_id, "schedules_deleted": deleted}
