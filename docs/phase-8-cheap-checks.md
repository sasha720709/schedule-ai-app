# Phase 8 — Cheap checks

**The current priority.** Agreed 2026-07-30 after a strategy review. Executed
out of numerical order, ahead of Phases 5, 4c and 7 — the same way Phase 6
was taken ahead of Phase 4, and for the same reason: the product does not
work well enough yet to be worth polishing.

Full reasoning, including the arguments against parts of it, is in the
strategy memo artifact `b78f3eac-7584-49cd-b8aa-5c5f382d6c6d`. This document
is the executable version and is deliberately self-contained.

## Why this is not an optimisation

At $50–80 a month **per watched item**, nobody uses this product — including
its owner. A price tracker that costs more than the discount it finds is a
demo. So per-check cost is not a tuning task to slot in after the interesting
work; it is the product work. The UI is the side quest.

## The actual defect

The Planner writes an extraction hint in English. Then a language model
re-reads that English and re-solves the same problem, from scratch, on every
tick — roughly 14,400 identical acts of reasoning a month to answer a
question that was already answered once, at planning time.

**The Planner should compile a checker, not describe a task.** Not
`"find the 512GB OLED price"` but a typed extraction spec a dozen lines of
Python executes for free, forever. That is the planner/executor pattern taken
to its conclusion; today the executor is still an interpreter.

## Numbers

One target, 3-minute checks (14,400 checks/month). Component costs measured
in Phase 6 where measurements exist.

| Architecture | Per check | Per month |
|---|---:|---:|
| Today — browser + Haiku | $0.0057 | $82.08 |
| Today — HTTP + Haiku | $0.0055 | $79.20 |
| Content hash, 90% skipped | $0.0006 | $8.20 |
| Compiled extractor + browser | $0.00017 | $2.45 |
| **Compiled extractor + JSON endpoint** | **$0.0000042** | **$0.06** |

Two things to notice. Content hashing — the fix currently written in the gap
list — is about a tenth as good as it looks next to the alternative. And the
interval stops mattering once a check is nearly free, which is the real goal:
*how often should this be checked* should be answered by the question, not by
the invoice.

## Decisions taken, and the ones argued down

**Compile extraction specs at plan time.** Agreed.

**Make the Planner more expensive on purpose.** Agreed. Planning is amortised
over ~14,000 checks; going from $0.05 to $0.20 is invisible and buys
everything downstream. This also fixes the biggest quality bug in the system
as it stands: **the Planner has never once opened a page it recommends.** It
web-searches and hands over targets sight-unseen. Every Phase 2 failure
(Amazon, Best Buy) would have been caught at plan time by a single fetch.

**Self-hosting a model — rejected, twice over.** It optimises a cost that
Phase 8 deletes, and the calls that remain (planning, rare repair) are
low-volume and judgement-heavy, the worst possible shape for dedicated
hardware. A `g4dn.xlarge` is ~$384/month idle or not, about 7,700 Sonnet
plans; break-even needs ~250 new watches a day. A smaller self-hosted model
would also be *worse* at the one job where a mistake is systematic and
permanent — a bad selector, URL or interval is wrong for the whole life of
the watch.

**Parsers fail silently, and that is the real risk here.** A model says "I
couldn't find the price". A CSS selector whose page was redesigned returns
nothing, and `nothing` is indistinguishable from `condition not met`. The
answer is not to keep the model in the reading path but to keep it in the
*repair* path, plus a verified baseline so drift is detectable at all.

**Conditional GET beats content hashing.** `If-None-Match` against a stored
`ETag` yields `304 Not Modified` in ~200 bytes: no body, no parse, no model,
no write. Hashing still pays for the full fetch first, and real pages change
constantly for reasons nobody cares about (rotating ads, timestamps, "5
people are viewing this").

**Once the model is gone the browser is the dominant cost** — ~$0.00016 a
render against ~$0.000002 for a plain fetch, so ~80×. Which redefines the
Planner's job: not "find a page to watch" but **find the cheapest reliable
source of this fact.** Apple's share price is available as JSON; a Planner
told to hunt for an API turns a Chromium render into a 2KB GET.

## The target architecture

A tiered Checker. The model leaves the hot path and returns only on failure.

| Tier | When | Cost | What happens |
|---|---|---:|---|
| **0** | >99% of ticks | ~$0.000004 | Conditional GET; a `304` ends the tick. Otherwise fetch, run the compiled extractor, coerce, compare. Pure Python. |
| **1** | extractor miss, or value implausible vs baseline | ~$0.005 | One Haiku call re-reads the page and re-derives the spec. If the new spec verifies, store it and carry on. A site redesign costs one check, not a dead watch. |
| **2** | repair keeps failing | — | Mark the watch `degraded` and tell the owner. A watch that cannot read its target must say so. |

What the Planner stores instead of prose:

```json
{
  "url": "https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
  "fetch_method": "http",
  "extractor": {
    "kind": "jsonpath",
    "path": "$.chart.result[0].meta.regularMarketPrice",
    "parse": "float"
  },
  "verified_at": "2026-07-30T09:40:00Z",
  "verified_value": 271.42,
  "extract_hint": "regular market price of AAPL"
}
```

`extract_hint` does not disappear — it becomes the *repair* instruction
rather than the *reading* instruction. That is the whole change in one line.

`verified_value` also strengthens the confirm step already built: the plan
card stops saying "I intend to read the price" and starts saying "I read
$629.00 just now — start watching?"

## A schema gap found while reasoning about this

"Tell me when Apple shares go down 5% from current" **cannot be expressed
today.** `condition` is `{metric, op, value}`, strictly absolute. A relative
condition needs a captured baseline and a computed threshold — a second,
independent reason the Planner must fetch at plan time, because there is no
"5% down" without knowing what it is 5% down from. Add `baseline` and a
relative op while the schema is being touched anyway.

## Steps

### 8a — Guardrails · **do first, hours not days**

The only work that protects the owner while the rest is built.

- A single shared cost model (`shared/cost.py`), copied into each Lambda's
  zip by its `build.sh`. No Layer yet — that stays a deferred gap.
- **Budget per watch, not an interval floor.** Deriving the minimum interval
  from a monthly budget means that when Phase 8b cuts per-check cost by
  ~1000×, tight intervals become allowed automatically with no constant to
  remember to change.
- The Planner clamps whatever interval the model proposed up to the
  budget-derived floor, and the prompt is told the floor exists.
- `POST /watches/{id}/confirm` rejects an interval whose estimated cost
  exceeds the budget, and returns the estimate either way.
- The Checker emits an `EstimatedCostUSD` CloudWatch metric per check, so
  spend is visible *somewhere*. **AWS budget alarms cannot see Anthropic
  spend** — the alarms at $50/$100/$200 are blind to the dominant cost of
  this system. An actual alarm on this metric needs
  `cloudwatch:PutMetricAlarm` added to the deploy user, so it is grouped with
  Phase 5.

### 8b — Compiled extractors · the main event

- Typed extractor kinds: `jsonpath`, `css`, `regex`, each with a `parse`
  coercion (`float`, `currency`, `int`, `text`).
- Pure-function executors, so this is the one part of the system that is
  genuinely pleasant to test — write tests alongside, like the api Lambda's.
- The Planner fetches the candidate URL, proposes a spec, **runs it**,
  confirms a plausible value, and stores it with `verified_value`. A plan
  that cannot be verified is never offered.
- Prompt rewritten to prefer JSON endpoints over pages, and to emit a spec
  rather than prose.
- The Checker's Tier 0 path executes the spec. No model.

### 8c — Conditional GET · small, independent

Store `etag` and `last_modified` per target, send `If-None-Match` /
`If-Modified-Since`, treat `304` as "unchanged" and end the tick before
fetching a body. Orthogonal to 8b and close to free.

### 8d — Tiered self-heal and `degraded`

Tier 1 repair, Tier 2 give-up, a `degraded` status, and a notification when a
watch has stopped being able to read its target. This is what earns the right
to leave a deterministic extractor running unattended.

## What comes after

Phase 5 (hygiene — now worth doing, because the design is settled), then
Phase 4c (the interface, better built against the final data model), then
Phase 7 (CI/CD — lowest ratio: offline tests already exist where they matter,
deploys are infrequent and manual, and there is one user).
