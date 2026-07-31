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

Split into two passes, so the engine could be reviewed on its own before
anything depended on it.

#### 8b pass 1 — the engine · **done**

`shared/extract.py` plus 95 tests. Typed kinds `jsonpath`, `css` and `regex`,
each with a `parse` coercion (`float`, `currency`, `int`, `text`, `bool`),
an optional `unavailable_if` predicate, and a `plausible()` check against the
plan-time value.

Decisions worth not relitigating:

- **Three outcomes, not two** — `ok` / `unavailable` / `failed`. `failed`
  means the extractor is broken and 8d must escalate it; `unavailable` means
  there is legitimately no value today and 8d must ignore it. `unavailable_if`
  is evaluated *before* the value is read, because an out-of-stock page
  usually still shows a price.
- **Money is stricter than a number.** `currency` requires a symbol or a
  two-digit minor unit. The tests caught the permissive parser reading `512`
  out of "Steam Deck 512 GB OLED" — a capacity that would have fired an
  "under $600" watch instantly. `float` stays permissive for points and counts.
- **JSONPath is deliberately minimal** — dotted keys and integer indices only.
  No wildcards, filters or recursive descent: a path the Planner cannot verify
  is a path that breaks quietly.
- **beautifulsoup4 + soupsieve, not lxml.** Both are pure Python; lxml ships
  compiled wheels and this repo already documents `pip install -t` vendoring
  platform-specific binaries that work only because the Codespace happens to
  match the Lambda runtime.

Verified against live pages as well as fixtures: a real JSON rate endpoint,
real Hacker News HTML where CSS and regex independently agree, and a real
headline correctly refused as money.

`beautifulsoup4` was added to `checker/requirements.txt` and
`planner/requirements.txt`, but **the zips were deliberately not rebuilt** —
nothing imports it yet, so the deployed functions are unchanged and Terraform
reports no drift. Pass 2 rebuilds them.

#### 8b pass 2 — wire it up · **done**

All six items below are built, deployed and verified live. Two things were
found on the way that changed the engine rather than merely consuming it:

- **`scope`**, an optional CSS selector that narrows the document *and* acts
  as a liveness anchor. A whole-document `unavailable_if` is unsound — on the
  real 1.49MB Steam Deck page, "Out of stock" matched the Docking Station and
  a localised string table, reporting an in-stock item as `unavailable`, the
  one outcome 8d must never escalate. Inside a proven scope, a miss is
  legitimate absence; without one it stays `failed`.
- **`count`**, because absence is a first-class answer for most watches that
  are not about a price. A vacancy watch is absent by definition until it
  fires; on a real HN jobs page, asking for a Rust role returned `failed`,
  which under 8d would have paid for a Haiku repair on every tick forever.

Also added: `shared/condition.py`. Nothing in this codebase had ever
*evaluated* a condition — the model compared in its own head — so Tier 0
needed a real comparator. An op it cannot understand raises rather than
answering "not met", because a silent False is a watch that is alive, billed,
checked on schedule and structurally incapable of ever firing.



1. **The Fetcher must return HTML.** It currently returns
   `page.inner_text()`, plain text with no markup, so **CSS extractors cannot
   work on browser-rendered pages at all.** Return both: `html` for Tier 0
   extraction, `text` for Tier 1 repair prompts, where fewer tokens is the
   point. Remember this is a container-image Lambda —
   `terraform apply` will *not* redeploy it on a `:latest` push; use
   `aws lambda update-function-code`.
2. **Planner emits and verifies a spec.** Fetch the candidate URL, propose an
   extractor, run it, confirm a plausible value, store `verified_value` and
   `verified_at`. A plan that cannot be verified is never offered. This is
   also what makes the plan card say "I read $629.00 just now" rather than
   "I intend to read the price".
3. **Prompt rewritten** to emit a spec rather than prose, and to prefer a JSON
   endpoint over a page — once the model leaves the hot path the browser is
   the dominant cost, ~80× a plain fetch.
4. **Checker Tier 0** executes the spec with no model call. Keep `judge()`
   for 8d's repair path; it stops being the default, not the codebase.
5. **Schema**: add `extractor`, `verified_value`, `verified_at` to
   `WatchTargets`. Existing rows have none, so the Checker must fall back to
   the model path when `extractor` is absent rather than failing.
6. Rebuild and deploy all three zips; `shared/extract.py` needs vendoring into
   the Checker and Planner the way `cost.py` already is.

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
