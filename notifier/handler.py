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


def _delete_schedules(watch_id: str) -> list:
    targets_table = dynamodb.Table(os.environ["WATCH_TARGETS_TABLE"])
    response = targets_table.query(
        IndexName="watch_id-index",
        KeyConditionExpression=Key("watch_id").eq(watch_id),
    )

    deleted = []
    for target in response.get("Items", []):
        name = f"schedule-ai-app-{target['target_id']}"
        try:
            scheduler.delete_schedule(Name=name)
            deleted.append(name)
        except scheduler.exceptions.ResourceNotFoundException:
            # Already gone -- fine, this is the state we wanted anyway.
            pass
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
