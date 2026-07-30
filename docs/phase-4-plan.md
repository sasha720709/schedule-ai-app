# Phase 4 — API + web chat UI

**Status: sub-phase 4a complete 2026-07-30, verified over real HTTP.
All five decisions settled (bottom of this document). 4b is next.**

Written 2026-07-29, at the end of the session that finished Phase 6;
decisions recorded the following day.

## The uncomfortable discovery that starts this phase

**There are no watch lifecycle operations at all.** No list, no pause, no
cancel, no delete. The only way to create a watch is to invoke the Planner
Lambda; the only way to remove one is hand-editing DynamoDB and deleting
EventBridge schedules by name — which is literally what was done three
times during the Phase 6 session to clear test rows.

A UI cannot ship on that. So Phase 4 does not start with React. It starts
with the API that should already exist, and that API is worth having even
if a frontend never ships.

`DELETE` is the one to get right: it must remove the target rows, the
watch row, **and** the EventBridge schedules together. Miss the schedules
and the watch keeps billing forever with nothing pointing at it.

## The constraint that shapes everything

The Phase 6 Planner run took **19.5 seconds** (web search + Sonnet +
schedule creation). API Gateway HTTP APIs hard-cap at **29 seconds**.

A synchronous `POST /watches` is therefore one slow web search away from
timing out — and worse, the client would have no way to know whether the
watch had been created or not.

So the API must be asynchronous. Pleasingly, the schema already
anticipated this: the `planning` status has been in the data model since
the first draft and has never meant anything. Now it does.

- `POST /watches` invokes the Planner with `InvocationType='Event'`
- returns `202` immediately with a `watch_id` and `status: "planning"`
- the client polls `GET /watches/{id}` until status leaves `planning`

This is the standard async-job pattern and is a large part of what this
phase teaches.

## Product vision

**Chat is right for creating a watch and wrong for managing them.** Plain
English is the magic of this product. But "show me my watches" as a chat
transcript is strictly worse than a list, and "cancel the third one" is
worse than a button.

So: a two-pane app. Chat on the left to create, a live watch list on the
right to manage.

The more important idea is a **review step** between planning and
committing:

```
1. You type:      "tell me when the Steam Deck OLED drops under $450"
2. Immediately:   watch appears in the list, status "planning", spinner
3. Planner runs:  ~20s (searching the web...)
4. You see a PLAN CARD, not a done deal:

      +----------------------------------------------+
      | store.steampowered.com/sale/steamdeck...      |
      | browser render - every 10 min                 |
      | "read the 512GB OLED refurbished price"       |
      | trigger when price < $450 USD                 |
      |            [ Start watching ]   [ Adjust ]    |
      +----------------------------------------------+

5. You accept -> schedules created -> status active
```

The review step earns its place three times over:

- **It makes the agent inspectable.** You see *why* it chose that URL,
  that interval, `browser` over `http`. For a project whose stated goal
  is learning agentic design, that is the whole point made visible.
- **It prevents expensive mistakes.** Today the Planner creates schedules
  the instant it decides. A 5-minute interval chosen on a whim is about
  $50/month, committed silently. A confirm step turns that into a number
  you see first.
- **It fixes a real bug for free.** Splitting "propose" from "commit"
  means the Planner stops creating schedules mid-flight, which is exactly
  the *no partial-failure handling* gap in CLAUDE.md. Plan writes rows;
  confirm creates schedules; a failure in between leaves nothing
  orphaned.

**Decided: plan-then-confirm.** This is the one decision that changes
`planner/handler.py` rather than only the frontend, so it is described
concretely under "The Planner split" below.

## Proposed API surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/watches` | 202 + `watch_id`, kicks off planning |
| `GET` | `/watches` | List all, with status and last value |
| `GET` | `/watches/{id}` | Detail, including targets |
| `POST` | `/watches/{id}/confirm` | Create schedules, status -> `active` |
| `PATCH` | `/watches/{id}` | Pause, resume, edit threshold or interval |
| `DELETE` | `/watches/{id}` | Delete targets + watch + **schedules** |

Implement as **one `api` Lambda with internal routing**, not one Lambda
per route: fewer cold starts, one IAM role, far less Terraform.

## Auth

Shared passcode, as decided long ago. Sent in an `Authorization` header,
hash stored in SSM Parameter Store, validated by a **Lambda authorizer**
(worth learning as a concept), frontend keeps it in `localStorage`.

Being honest about what this is: **not real security.** Anyone with the
passcode has full control, there is no session expiry and no per-user
isolation. Appropriate for single-user; it needs replacing before anyone
else touches the system. Note `user_id` is currently hardcoded to
`"default"` everywhere.

## Sub-phases

| | Deliverable | Why this order |
|---|---|---|
| **4a** | Lifecycle API + authorizer, curl-testable | **Done.** Unblocked everything; valuable even with no UI |
| **4b** | S3 + CloudFront + minimal React that lists watches | Proves hosting, CORS and deploy before real UI work |
| **4c** | Chat creation flow + plan review card | The actual product surface |
| **4d** | *(optional)* Multi-turn clarification | "Which PS5 model?" — nice, not necessary |

If 4d happens, keep conversation history **in the browser** and send the
full message array each turn. No conversations table, no server state, no
new DynamoDB design.

## What this phase teaches

API Gateway HTTP APIs (and why HTTP over REST APIs: cheaper, simpler,
sufficient) · CORS and preflight · Lambda authorizers · the async
202-plus-polling pattern · CloudFront with **Origin Access Control** (the
modern replacement for OAI) · SPA fallback routing (403/404 ->
`index.html`) · cache invalidation · Terraform managing built frontend
assets · React state for polling.

## Cost

Effectively zero. API Gateway HTTP is $1 per million requests;
CloudFront's perpetual free tier covers 1TB egress and 10M requests per
month; S3 is pennies. Nothing here moves the bill — the Haiku calls
remain the only real cost in this system.

## The Planner split (consequence of decision 1)

`planner/handler.py` currently does two jobs in one invocation: it calls
`plan()`, and it creates an EventBridge schedule per target. Plan-then-
confirm separates them.

- **Plan** (async, triggered by `POST /watches`): call `plan()`, write the
  `Watches` row with `status: "planning"` and the `WatchTargets` rows, and
  **stop**. Create no schedules. Store the proposed
  `check_interval_min` on the watch so the UI can show it and price it.
- **Confirm** (`POST /watches/{id}/confirm`): read the watch and its
  targets, create one schedule per target, write each `schedule_arn` back,
  flip the watch to `active`. If the caller passes an adjusted interval,
  use that instead of the Planner's.

Two things fall out of this for free:

- The *no partial-failure handling* gap disappears from the planning
  path. Planning becomes pure writes with nothing external to leak; if
  confirm fails halfway it can be retried idempotently, because
  `create_schedule` is keyed on a deterministic name
  (`schedule-ai-app-{target_id}`) and the already-created ones can be
  detected rather than duplicated.
- A watch abandoned at the `planning` stage costs nothing. It is rows in
  DynamoDB and no schedules, so the failure mode of a bad plan is a
  wasted Sonnet call rather than a recurring bill.

Note the Checker currently treats `planning` as checkable — it runs on
`status in ("active", "planning")`. That has to change to `active` only,
or a confirmed-but-not-yet-flipped watch could tick early.

## Decisions — settled 2026-07-30

1. **Plan-then-confirm.** The Planner proposes; the owner commits. Chosen
   for an inspectable agent, no silently-committed expensive interval, and
   the partial-failure fix that comes with it.
2. **Chat + watch list hybrid.** Chat creates, a list manages. Pure chat
   was rejected because listing and cancelling read worse as a
   transcript; a form-only dashboard was rejected because it throws away
   the plain-English planning that is the point of the project.
3. **One `api` Lambda** with internal routing, not one per route.
4. **Defer multi-turn chat** to sub-phase 4d.
5. **Use the CloudFront URL**, no custom domain — Route 53 and ACM add
   cost and setup for no learning not already covered.

## Related note from the same session: the cost question

The owner asked whether self-hosting a small model for the Checker would
beat paying for Haiku. Answer: no, at this scale, for three reasons.

- **Economics.** The cheapest practical AWS GPU (`g4dn.xlarge`, ~$0.53/hr,
  ~$384/month) buys about 70,000 Haiku checks. One target at a 10-minute
  interval uses 4,320/month, so break-even is roughly **16 targets running
  continuously** — before counting build and maintenance time. The
  workload is bursty and scales to zero, which is the worst possible shape
  for dedicated inference hardware. AWS has no cheap serverless *GPU*;
  SageMaker Serverless Inference is CPU-only.
- **CPU inference is too slow for this prompt.** Prefill of 5,000 tokens
  through a 0.5B model is roughly `2 x 0.5e9 x 5000 = 5 TFLOPs`. A 2GB
  Lambda gives a bit over one vCPU, realistically 30-50 GFLOPS: **100+
  seconds**, past the 60s timeout.
- **The task is harder than it looks and fails destructively.** The Phase
  6 test page carried five prices ($629.00, $759.00, $279.00, $319.00,
  $359.00) and the Checker had to pick the 512GB OLED refurbished one and
  notice it was out of stock. A sub-1B model plausibly grabs the wrong
  one — and `condition_met: true` is irreversible: it emails and deletes
  the schedules. Reliable structured JSON under "never invent a value" is
  exactly where tiny models are weakest.

**The real optimization is not calling the model at all.** Hash the
fetched text and skip `judge()` when it is unchanged — on a
weekly-moving price checked every 10 minutes, well over 99% of fetches
are byte-identical. That is worth 10-100x, against maybe 2x for a model
swap. Pre-filtering the page to the region around the hint is worth
another ~10x. Both beat any model change, and both make the *API* model
cheaper too, since its cost falls linearly with tokens while self-hosted
compute still pays for wall-clock.
