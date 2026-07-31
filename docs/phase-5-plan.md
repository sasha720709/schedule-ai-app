# Phase 5 — Production hygiene

Deliberately after Phase 8. Alarms watch specific code paths, and Phase 8
replaced the hot one; doing this first would have meant doing it twice.

## Blocked on you: one IAM change

The alarm code is written and committed, and **gated off** behind
`enable_alarms = false` in `terraform/app/variables.tf`. It is off because the
`schedule-ai-terraform` user cannot create SNS topics or CloudWatch alarms, and
**cannot grant itself the permission** — by design it holds `iam:*Role` but not
`iam:*Policy`, so it cannot edit the customer-managed policy that governs it.

Without the gate, an unrelated `terraform apply` fails on a 403 for a resource
nobody was touching, which is a bad trade for an alarm that is not yet wired up.

### What to add

In the AWS console, to the customer-managed policy **`schedule-ai-app-terraform`**
(the one CLAUDE.md says future permissions go in), add these two statements:

```json
{
  "Sid": "Alarms",
  "Effect": "Allow",
  "Action": [
    "cloudwatch:PutMetricAlarm",
    "cloudwatch:DeleteAlarms",
    "cloudwatch:DescribeAlarms",
    "cloudwatch:ListTagsForResource",
    "cloudwatch:TagResource",
    "cloudwatch:UntagResource"
  ],
  "Resource": "arn:aws:cloudwatch:*:*:alarm:schedule-ai-app-*"
},
{
  "Sid": "AlarmNotifications",
  "Effect": "Allow",
  "Action": [
    "sns:CreateTopic",
    "sns:DeleteTopic",
    "sns:GetTopicAttributes",
    "sns:SetTopicAttributes",
    "sns:Subscribe",
    "sns:Unsubscribe",
    "sns:GetSubscriptionAttributes",
    "sns:ListSubscriptionsByTopic",
    "sns:ListTagsForResource",
    "sns:TagResource"
  ],
  "Resource": "arn:aws:sns:*:*:schedule-ai-app-*"
}
```

`DescribeAlarms` has no resource-level support and will need `"Resource": "*"`
if AWS rejects the scoped form — split it into its own statement rather than
widening the whole block.

### Then

```sh
# The topic already exists: the first apply created it before failing on the
# read-back, and it was removed from Terraform state so apply would work again.
# Adopt it rather than letting Terraform try to create it a second time.
cd terraform/app
terraform import 'aws_sns_topic.alarms[0]' \
  arn:aws:sns:us-east-1:851725214678:schedule-ai-app-alarms

# then set enable_alarms = true (default in variables.tf, or -var)
terraform apply
```

The email subscription needs a confirmation click in the first message AWS
sends. Until then it sits in `PendingConfirmation` and the alarm fires into
nothing — **Terraform reports success either way**, so check the subscription
state rather than assuming it works.

## What the alarms are, and why these

**`schedule-ai-app-daily-spend`** — the one that matters. The account has AWS
budget alarms at $50/$100/$200 and they are structurally blind to the cost that
has always dominated this system, because Anthropic bills Anthropic and AWS
never sees it. `EstimatedCostUSD` is the Checker's own accounting.

It watches the **dimensionless** series. The Checker publishes the same number
twice — once split by fetch method and model use for reading, and once bare —
because a dimensioned metric cannot be alarmed on without naming its exact
dimension set. An alarm on `{browser, false}` would ignore every HTTP check and
every Tier 1 repair, which is exactly the spend worth catching.

**`schedule-ai-app-checker-errors`** — covers the shape no email can. A
degraded watch emails you itself; a Checker that is throwing means no tick ran,
so nothing knows to complain.

## Still open

Ordered by how much they would hurt.

- **The Fetcher's memory across warm invocations.** Three renders in one
  container reported 1207MB, 1249MB, 1304MB before Chromium died. Mitigated by
  an explicit teardown and one retry, **not diagnosed** — it may be a leak or
  three differently-sized pages. Needs a controlled run of the same URL N times.
- **A value that is stale relative to the interval.** The CNN AAPL extractor
  reads "Last closed at $…", which moves once a day, on a watch checking every
  minute — while a live pre-market price sits on the same page unread. This is
  not `failed`: it is a correct reading of the wrong thing, so 8d cannot see it.
- **An unverifiable text filter.** A `count` of 0 with a healthy scope is either
  "no matching job yet" or "the filter is wrong", and nothing can tell them
  apart. Storing the unfiltered item count as a baseline would at least let a
  human judge.
- **Platform-specific wheels.** `pip install -t` vendors binaries for whatever
  built the zip; `checker/build/` holds
  `_pydantic_core.cpython-312-x86_64-linux-gnu.so`. It works only because the
  Codespace matches the Lambda runtime. Fix with
  `--platform manylinux2014_x86_64 --only-binary=:all:`.
- **No DLQ on the Notifier.** EventBridge retries a failing target ~185 times
  over 24h and nothing catches a notification that can never succeed.
- **Stale `schedule_arn` values.** The Notifier deletes schedules without
  clearing the field, so rows point at schedules that no longer exist.
- **The Notifier's GSI query is not paginated.** `query()` returns at most 1MB;
  a watch with enough targets would keep some schedules alive forever.
- **`anthropic` is vendored three times now** — Planner, Checker and their
  shared modules. A Lambda Layer would deduplicate it.
