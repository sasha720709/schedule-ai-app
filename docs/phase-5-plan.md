# Phase 5 — Production hygiene

Deliberately after Phase 8. Alarms watch specific code paths, and Phase 8
replaced the hot one; doing this first would have meant doing it twice.

**Status: done as of 2026-08-02.** All three alarms are live, the SNS
subscription is confirmed, and `enable_alarms` defaults to `true`. What
remains is under "Still open" at the bottom. The section below is kept as the
record of how the IAM block was cleared — it will be needed again the next
time this phase-by-phase permission model hits a service it has never used.

## ~~Blocked on you: one IAM change~~ — cleared 2026-07-31

The alarm code was **gated off** behind `enable_alarms = false` in
`terraform/app/variables.tf`, because the `schedule-ai-terraform` user could
not create SNS topics or CloudWatch alarms, and **could not grant itself the
permission** — by design it holds `iam:*Role` but not `iam:*Policy`, so it
cannot edit the customer-managed policy that governs it.

Without the gate, an unrelated `terraform apply` fails on a 403 for a resource
nobody was touching, which is a bad trade for an alarm that is not yet wired up.

The gate itself is **kept** now that the permissions exist. It costs one
variable and it is what makes the stack applicable from an account that has
not been through this grant.

### What was added (done, in the console)

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

### Then — done, both steps

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
state rather than assuming it works. It was clicked; the subscription carries
a real ARN. Re-check with:

```sh
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-east-1:851725214678:schedule-ai-app-alarms \
  --query 'Subscriptions[].SubscriptionArn' --output text
```

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

**`schedule-ai-app-notifier-errors`** — the other half of the retry cap. Both
EventBridge targets now carry `retry_policy { maximum_retry_attempts = 8,
maximum_event_age_in_seconds = 3600 }` instead of EventBridge's default of ~185
attempts over 24 hours. That is a deliberate trade and it is only acceptable
with this alarm attached: a notification that can never succeed is now
**dropped**, and dropping silently would mean a watch fires and nobody hears.
Threshold is one error, because one lost notification is the whole product
failing at the only moment it matters.

Note what this is not. A dead-letter queue would keep the failed *event* so it
could be replayed; a retry cap only stops the retrying. The DLQ needs SQS
permissions the deploy user does not have, and would repeat the whole IAM
dance above for a payload we can reconstruct from the watch row anyway.

## Also closed in this phase

Four items lifted straight out of the CLAUDE.md gap list, none of them large,
all of them the kind that rot quietly:

- **The Notifier's GSI query is now paginated** (`_all_targets()` follows
  `LastEvaluatedKey`). Unreachable at 1–3 targets per watch; the failure mode
  was half a watch's schedules left running and billing forever.
- **`schedule_arn` is cleared** on the same pass that deletes the schedule, so
  the table stops describing schedules that do not exist.
- **Wheels are pinned to the Lambda platform** in `planner/build.sh` and
  `checker/build.sh` (`--platform manylinux2014_x86_64 --implementation cp
  --python-version 3.12 --only-binary=:all:`). Both zips were rebuilt and
  redeployed under the new flags. `api/` and `authorizer/` have no binary
  dependencies and were left alone.
- **Retries are capped**, as above.

## Still open

Ordered by how much they would hurt.

- ~~**The Fetcher's memory across warm invocations.**~~ **Diagnosed and closed
  2026-08-02 — there is no leak, and the evidence for one never existed.**

  `Max Memory Used` in a Lambda REPORT line is a **container high-water mark**,
  not a per-invocation measurement. It cannot go down. So "1207 → 1249 → 1304"
  was not a rising trend to be explained; a non-decreasing sequence is the only
  thing that metric can produce.

  Shown directly rather than argued: in one warm container, after a 1.49MB
  page, `https://example.com` — 559 bytes of HTML — reported the same 887MB.
  So did a 3.5MB Wikipedia article. No per-invocation figure behaves that way.

  The controlled run this entry asked for was then done: **25 consecutive
  renders in one container**, page sizes 559 → 3,547,897 characters, in bursts,
  reached 887MB by the sixth render and stayed at 887–888MB for the remaining
  nineteen. Nothing crashed. Peak memory is Chromium's own baseline; page size
  barely enters into it.

  The explicit teardown added in 8b pass 3 (image pushed 2026-07-31 10:22, one
  commit later) therefore looks like the actual fix rather than a mitigation —
  the 1304MB death was most likely un-closed browsers accumulating in a frozen
  container. That last link is inference; reverting the image to prove it was
  not worth the deploy.

  **Reusable consequence:** never reason about this Lambda's memory from a
  single REPORT line again. Reproduce with the sequence probe — invoke with
  `--log-type Tail`, decode `LogResult`, and vary page size *within* one warm
  container, because that is the only thing the high-water mark can falsify.

- **The Fetcher's memory setting is now measurable, and was not before.**
  887MB peak of 2048MB allocated. The old warning against just lowering it
  still stands — Lambda scales CPU with memory and cost is memory × duration,
  so a smaller setting can render slower and cost the same while creeping
  toward the 60s timeout. But the probe above makes the experiment cheap:
  render a fixed page N times at 1024 / 1280 / 1536 / 2048 and compare
  memory × duration. 1024 is below the observed peak and should be expected
  to fail rather than merely slow down.
- **A value that is stale relative to the interval.** The CNN AAPL extractor
  reads "Last closed at $…", which moves once a day, on a watch checking every
  minute — while a live pre-market price sits on the same page unread. This is
  not `failed`: it is a correct reading of the wrong thing, so 8d cannot see it.
- **An unverifiable text filter.** A `count` of 0 with a healthy scope is either
  "no matching job yet" or "the filter is wrong", and nothing can tell them
  apart. Storing the unfiltered item count as a baseline would at least let a
  human judge.
- **No true DLQ on the Notifier.** Retries are capped at 8, which stops the
  24-hour retry storm but *discards* an event that never succeeds. Keeping the
  event needs SQS permissions the deploy user does not have.
- **`anthropic` is vendored twice** — Planner and Checker each carry ~7.7MB of
  the same dependency tree. A Lambda Layer would deduplicate it.
- ~~**Two Lambdas have no tests at all.**~~ Closed 2026-08-02: `notifier/` 19
  and `authorizer/` 24, bringing the suite to **305**, and it now runs on
  every push. What remains is that **nothing tests the chain end to end** —
  Planner → schedule → Checker → event → Notifier is still only ever verified
  by invoking real Lambdas against real AWS.
