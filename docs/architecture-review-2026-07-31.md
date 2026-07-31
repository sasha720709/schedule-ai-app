# Architecture review — 2026-07-31

Written at the owner's request after Phase 8 closed: a recap of what was
built, a verdict on each architectural decision made along the way, and an
honest list of what is still not right. Decisions here were re-examined, not
just summarised — where the review disagreed with the build, the build was
changed the same day (marked **redone**).

## What exists now, in one paragraph

A watch is created in plain English. The Planner (Sonnet) routes it: market
quotes go straight to a canned source registry with nothing searched and
nothing compiled; everything else gets a web search, a plan-time fetch of
every candidate page, and a compiled extraction spec (`jsonpath` / `css` /
`regex` / `count`) that is executed and verified against the live page before
the plan is ever offered. Checks then run on EventBridge schedules with **no
model in the loop** — pure Python reads the value and judges the condition —
at $0.000004 (http) to $0.00019 (browser) per check, against $0.0057 before
Phase 8. When an extractor breaks, Haiku repairs it once, verified before
trust, charged to the same $5/month budget as the checks; if repair cannot
help, the watch degrades loudly: email, schedules deleted, reason on the row.

## Decisions reviewed

### Held up (kept as designed)

- **Budget, not constants.** Every limit is derived from `MONTHLY_BUDGET_USD`
  — the interval floor, the repair allowance. When 8b cut per-check cost
  1000×, 1-minute intervals became legal with no constant edited. This
  decision paid for itself twice and is the pattern to repeat.
- **Three extraction outcomes** (`ok`/`unavailable`/`failed`), with `scope`
  as the anchor that makes absence distinguishable from breakage. Everything
  in 8d hangs off this and it survived contact with real pages.
- **Verify at plan time, on the real page.** Every fabrication bug this
  project has found (below) was caught because something was finally checked
  against reality. The Planner never offering an unverified plan is the
  single most load-bearing behaviour in the system.
- **Prove the cheap fetch instead of asking for it.** http-vs-browser is
  settled by trying, not by the model's opinion; 45× cost difference.
- **Repair on `failed` only, never `unavailable`.** The one distinction that
  keeps the repair path from becoming the hot path again.

### Redone during this review

- **Market quotes were being re-reasoned per request.** Four runs of "watch
  the Apple price" produced four different sites: CNN (an extractor reading
  *"Last closed at"* — a once-a-day figure — for a per-minute watch),
  stockprices.dev (Cloudflare), stooq.com (404), CNN again. Wrong shape of
  intelligence: where to look up a quote is a fact, not a judgement.
  **Fix: `shared/sources.py`**, a known-source registry. The model's only
  job is resolving "Apple" → `AAPL`; the URL (CNBC's keyless quote JSON,
  chosen by testing — Yahoo 429s and stooq 404s from datacenter IPs) and the
  canned jsonpath come from the registry, and the spec is still verified
  live before the plan is offered. Two consecutive runs now produce
  byte-identical targets. Deliberately narrow: sneakers, electronics,
  anything with a "where" still takes the searching path, because those
  genuinely need it.
- **`EstimatedCostUSD` could not be alarmed on** (dimensioned-only). Now
  published twice, once bare. (Done under Phase 5, same day.)
- **The frontend carried a second copy of the cost model**, three orders of
  magnitude stale — quoting $300/month for an $0.18 watch. Rate now comes
  from the API; the browser keeps only the arithmetic.

### Disagreed with, documented, but deliberately not redone

- **`DEGRADE_AFTER = 3` is a constant** in a codebase whose stated pattern is
  deriving limits from the budget. Kept because it measures a different
  thing (evidence of permanence, not money) and no budget expression of "not
  a transient blip" is simpler than `3`.
- **`plan.py`'s `plan()` is now a misleading name** — it only runs the search
  step. Kept for the offline CLI and api-lambda tests; renaming would churn
  three call sites for aesthetics.
- **Relative conditions take their baseline from the first verified target.**
  A multi-target relative watch is ambiguous by construction; fine while
  targets-per-watch is 1–3 and same-fact.

## Known fabrication bugs, all found by *using* the product

Recorded together because they are one lesson, not four bugs: **a model
asked for an answer it cannot know will invent one.** Every fix was the same
move — take the decision away from the model and give it to something that
had actually looked.

1. Judged instead of read: refused to report $949 because the condition
   wanted < $700. (Fixed in READ_PROMPT.)
2. Fabricated threshold: "goes down from current" became `< 313.93` — 5%
   below a search-result price, on a page saying $333.43. (Fixed:
   `resolve_relative_condition` after verification.)
3. Presence watches unplannable: demanded a literal value for a thing whose
   absence is the premise. (Fixed: neighbour anchor + `count`.)
4. Different site every run for the same fact. (Fixed: known sources.)

## Not done right, still — ordered by hurt

1. **Stale value ≠ wrong value.** The CNBC `last` rests outside trading
   hours while `ExtendedMktQuote.last` moves; the old CNN extractor read a
   daily close on a per-minute watch. Nothing notices a value that is
   *fresher-looking than it is*. Needs a per-source staleness contract
   (e.g. `last_time` read alongside the value); no design yet.
2. **Text filters on presence watches are unverifiable** until the awaited
   thing exists. Mitigated by the stripped-filter probe; a stored unfiltered
   baseline count for 8d/UI would let a human judge drift.
3. **Fetcher memory growth** (1207→1304MB over three warm renders) is
   mitigated by teardown+retry, **not diagnosed**.
4. **The owner's own pre-registry watches** (Google Finance, hashed CSS
   classes like `.N6SYTe`) are exactly the fragile shape the registry now
   avoids — they will churn and lean on 8d repair. Recreating them is one
   click and routes them through the registry.
5. Zips are platform-dependent (`pip install -t` on matching arch only);
   `anthropic` vendored 3×; Notifier GSI unpaginated; stale `schedule_arn`
   after trigger; no DLQ; `user_id` hardcoded. All in `docs/phase-5-plan.md`.
6. **Alarms are written but gated off** pending one manual IAM change (the
   deploy user cannot grant itself SNS/CloudWatch; JSON in the plan doc).
