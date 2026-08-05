# CLAUDE.md

Context for any Claude Code session working in this repo — read this first.

## Start here (last session: 2026-08-05, short)

Everything is committed, deployed and green. **AWS is idle**: zero schedules,
zero watches, zero targets, so nothing is billing. **673 tests** pass in ~2s
with `python -m pytest -q` from the repo root, and every suite also passes
alone (`for d in */; do pytest $d; done`).

### What happened on 2026-08-05

One commit, `f39bee5`: **marketplaces step 3**. It was written down as
"watch-level conditions" and turned out to be about the *reading*, not the
condition — see `docs/marketplaces-roadmap.md` §7 for the argument and the
numbers. The short version, because three of these are bugs about money:

1. **Currencies really were being compared**, despite §5 saying they were not.
   One condition, applied to every target, with `currency` a label copied off
   the first shop that answered — so `price < 2000` shekels was true of
   Amazon's $34.99. A shop in another currency is now **refused at plan time**,
   with the reason stored on the watch (`rejected`) and shown on the plan card.
   No exchange rate: the owner ruled on 2026-08-05 that the real answer is the
   user's own country, arriving with auth.
2. **The baseline came from whichever shop loaded first.** Now the *best*
   verified reading — cheapest for `<`, dearest for `>`.
3. **The baseline described the wrong object.** The Planner takes it before the
   questions are answered, so "xbox series x" measured 10% off a ₪139 headset
   while following a ₪1,899 console. `confirm` re-derives it from the pinned
   offers; `resolve_relative_condition` moved to `shared/condition.py` so the
   arithmetic can be run twice.
4. **Two emails for one event.** All of a watch's schedules are created in the
   same second, so EventBridge fires them together and two shops crossing the
   same threshold both published. The `triggered` transition is a conditional
   write now.
5. The email lists **every shop, best first**, with when each price was read;
   a quiet shop is shown and flagged, never dropped. Built only when the watch
   speaks, like ranking.

**Do not "finish" this by evaluating the condition against the aggregate.**
That was considered and rejected, and `shared/across.py` opens with why:
`min(prices) < X` is true exactly when some price is under X and every shop is
on the same interval, so it fires at the same instant — and where it would
differ it would be worse, because it would fire on a sibling's reading that is
up to an interval old. **Fire on your own fresh reading, report the picture.**

**Proven live, 2026-08-05, both halves.** A USD Amazon watch pinned to a new
Series X moved its baseline `429.98 → 1289` and its threshold `386.98 →
1160.10` at confirm — 429.98 was a *refurbished Series S*. An Israeli watch
planned `across: best`, kept Ivory and Bug, and **rejected Amazon** with the
reason on the row; the Checker logged
`[across] ivory 229.0 ILS | bug 2679.0 ILS`, emailed, and tore both schedules
down. Everything was deleted afterwards.

`planner/handler.py` **had no tests at all** before this — everything under
`planner/` went through `plan.py` with its collaborators injected, the exact
shape that hid both live bugs of 2026-08-02. `planner/test_planner_handler.py`
drives the Lambda itself. Note the file basename: `planner/test_handler.py`
collides with `api/test_handler.py` and pytest refuses to collect both.

### What happened on 2026-08-04, in order

Twelve commits, `10dcffb` through `f27164b`. Each has a long message; this is
the map.

1. **The missing email** (`10dcffb`). A watch was correct and silent, and the
   two are indistinguishable from outside. `next_check_at`, plus windowed
   schedules that can finally express an hour.
2. **Shares tiers 1–3** (`4cc7297`, `d5837c2`, `d83b04a`). Which instrument,
   which exchange, which baseline — and noticing when a value stops moving.
   **Tel Aviv works**, Sunday–Thursday.
3. **Vacancies, all four steps** (`5e52a3b`, `47e4e84`, `b227a2c`, `dfcfad0`,
   `b23d9c3`). The email reports the job rather than a count; watches repeat
   with per-item dedup and a 90-day term; **LinkedIn works keyless**; new
   postings are ranked against the request; and the plan card asks questions
   built from what the search actually returned.
4. **Marketplaces steps 1–2** (`b7c8a04`, `a3722d9`, `f27164b`). A shops
   registry, an `offers` extractor that prefers schema.org, **Amazon on our
   own browser**, and answers that *pin* which offer a price watch follows.

### What to do next, in the order I would do it

1. **Marketplaces step 4 — landed price.** An item ₪50 cheaper with ₪60
   shipping is not cheaper, and comparing sticker prices across shops compares
   the wrong number. This is the one remaining thing that makes a product
   watch *wrong* rather than imprecise.
3. **Calendar reminders.** Two jobs, not one: the `.ics` file is easy
   (fifteen lines, no OAuth, `ses:SendRawEmail`), but the **trigger** is Phase
   9 step 3b and does not exist. Build the reminder kind first — attaching an
   `.ics` to a condition-watch email is near-pointless, because "Apple dropped
   below $300" already happened and a calendar entry in the past is clutter.
   `docs/phase-9-watch-kinds.md` §10, and §8 for the decision it depends on
   (a reminder stores `targets: []` and its schedule invokes with
   `{"watch_id": ...}`, so the Checker's entry point has to branch).
4. **Decide the auth shape** — see "Where the product is going". It does not
   have to be *built* early, it has to be *decided* early.
5. Then 4c (the designed chat UI) and the deploy half of Phase 7.

### The five roadmap documents, all current

| | |
|---|---|
| `docs/shares-roadmap.md` | tiers 1–3 done; tier 4 (history) after the frontend |
| `docs/vacancies-roadmap.md` | **all four steps done** |
| `docs/marketplaces-roadmap.md` | steps 1–3 done; **step 4 (landed price) next**; §7 is the step-3 write-up |
| `docs/phase-9-watch-kinds.md` | §10b is the missing-email write-up; §10 is what is left |
| `docs/architecture-review-2026-07-31.md` | every architectural decision to that date |

### Things carried forward that are easy to lose

1. **The previous-close baseline is settled: it is fine.** Decided by the
   owner — asked on a Sunday, Friday's close *is* "current". What that exposes
   instead: **"any change" is a guarantee, not a condition.** A stock never
   reopens at the previous close, so `relative_change_pct: 0` fires in the
   first seconds of the next session. Measured: baseline 306.40, first check
   306.49, fired — 0.03% on the first tick. Do **not** fix this by inventing a
   percentage; `plan.py` forbids that on purpose and fabricating 5% is a bug
   already shipped once. `docs/shares-roadmap.md` §1.
2. **The owner wants plain language, not jargon** — see "How the owner wants
   to work". Explain in consequences, not in terms.
3. **Run the real thing before believing it.** Today alone, running it found:
   `fetch.py` never decompressed gzip (paying 45× for nothing); LinkedIn
   rewrites every link on every response; LinkedIn's guest endpoint alternates
   between two result sets; Amazon keys on `data-asin` because its links carry
   the result *position*; and absent-vs-empty `watched_ids` meant a console
   watch followed a ₪29 game. **None of these was reachable offline.**
4. **The recurring-watch idea for shares is shelved, not dropped** — "stamp it
   each time it drops 5%". Decide the re-arm rule before writing code: 5% from
   the original baseline and 5% from the last alert are different products.
   `docs/shares-roadmap.md` §6.
5. **One orphaned target row** was found and removed by hand at the end of the
   2026-08-04 session. Most likely a race in my own testing (deleting a watch
   while the Planner was still writing its targets), but if `DELETE
   /watches/{id}` ever leaves a row behind in ordinary use, that is a real leak
   worth chasing. **Not seen again on 2026-08-05** — both deletes reported the
   right target counts and a scan afterwards returned zero.
6. **`terraform plan` needs two variables that live nowhere in the repo.**
   `TF_VAR_anthropic_api_key="$ANTHROPIC_API_KEY"` and
   `TF_VAR_notify_email=...` (read it back with `aws lambda
   get-function-configuration --function-name schedule-ai-app-notifier --query
   'Environment.Variables.NOTIFY_EMAIL'` rather than guessing). There is no
   committed `.tfvars`, on purpose.

### Design rules established today, worth not re-deriving

- **A correct system that explains nothing is a broken product.** The missing
  email was silence, not a defect.
- **A guardrail that returns 500 is an outage**, not a guardrail.
- **Prefer a published standard to a selector.** `schema.org/Product` is a
  contract shops maintain because Google reads it.
- **Identity comes from the site's own id, never from a link or from text.**
  Links carry tracking; text carries "2 days ago".
- **Answers mean opposite things for a stream and for a thing.** A job that
  misses a preference ranks lower; a product that is not the pinned one is not
  the product.
- **Model calls are paid per notification, not per check.** That is the whole
  reason ranking does not undo Phase 8b.
- **Nothing about ranking or questions may block a notification, or block
  creating a watch.** Any failure degrades to the previous behaviour.

## Where the product is going (owner, 2026-08-04)

Three ways to watch and then stop: a **share price**, a **job vacancy**, a
**thing for sale**. Then **calendar reminders**, then auth and the frontend,
then the product is done. No expansion beyond that — the remaining work is
polish on those three.

Worth knowing before acting on it:

- **They are not three features.** They are one engine at three levels of
  trust in the source — `quote` (registry, we own the extractor), `value`
  (searched, compiled, verified), `presence` (searched, compiled as a
  counter). All three already run live. "Finish the three" is one hardening
  pass plus a short per-kind list, not three projects.
- **Auth is not polish.** `user_id` is hardcoded `"default"` in every query
  and `NOTIFY_EMAIL` is one env var — a single recipient baked into the
  Notifier. A second user changes every table read, the SES send and the
  passcode model at once. Decide the shape early even if it is built late,
  or the polish gets redone on top of a single-user assumption.
- **Calendar reminders are two jobs, not one.** The `.ics` file really is
  easy (fifteen lines, no OAuth, `ses:SendRawEmail`). The *trigger* is Phase 9
  step 3b and does not exist. Attaching an `.ics` to a condition-watch email
  is near-pointless — "Apple dropped below $300" already happened, and a
  calendar entry in the past is clutter. Build the reminder kind first.

**`docs/marketplaces-roadmap.md` is the marketplaces analysis** (2026-08-04),
written before any code. Three things from it worth knowing:

- **Amazon is no longer out of reach, and Phase 6's note saying so is out of
  date.** Six consecutive Fetcher renders of an Amazon search returned 16–22
  product cards with real prices and no captcha. Its official
  Product Advertising API is **deprecated since 15 May 2026 and closed to new
  customers**; third-party scrapers cost $0.0074–$0.0147 per Amazon request
  **on top of a $49/month floor**, which is ten times the whole per-watch
  budget. Use our own browser: $0.000186/check, $0.13/month hourly.
- **The hard part is not the source, it is the product.** Today's Amazon render
  showed *Xbox Series X 1TB $754.94* next to *Xbox Series X 1TB Digital
  (Renewed) $649.99* — a "cheapest Xbox" watch fires on the refurbished one.
  A price watch on the wrong variant is confidently wrong, on a schedule, about
  money. `shared/questions.py` from the vacancies work is the tool for this.
- **A product watch should watch a product, not a page** — a basket of shops
  with the condition over the *best offer*. That needs **watch-level
  conditions**, which do not exist: the Checker evaluates per target, one
  target per tick, and each fires independently. That is the real engineering
  in this phase, not the scraping.

**Marketplaces step 1 is built** (2026-08-04): `shared/shops.py` (Ivory, Bug,
Amazon), a new **`offers` extractor**, and a `product` kind. `offers` prefers
**schema.org JSON-LD** — a contract shops maintain because Google reads it, and
it carries currency, stock, the offer's link and an `sku` that a CSS selector
cannot reach — falling back to a selector for the three of four shops that
publish none. Currencies are never compared: ILS and USD are separate targets
with separate thresholds.

**The live run is the argument for step 2, not a success story.** Planning
"Xbox Series X below 2000 shekels" gave three working shops at $0.067/month
whose cheapest offers were a **headset (₪139), WWE 2K26 ($34.99) and Suicide
Squad (₪29)** — no console anywhere, and `price < 2000` already true, so the
watch would have fired within minutes on an accessory. It was not confirmed.
Until `questions.py` is wired to products, a "cheapest" product watch watches
whatever a shop lists first.

**Marketplaces step 2 is built too**: the answers now **pin** which offers a
product watch follows (`watched_ids` on each target), where for jobs the same
answers are a ranking *preference*. Same machinery, opposite meaning — a job
that misses a preference scores lower, a product that is not the pinned one is
simply not the product. The plan card's wording branches on `repeating` because
saying the wrong one would be a lie about what happens next.

**Marketplaces step 3 is built** (2026-08-05): a watch with several shops now
has **one reading**, `shared/across.py`. The condition is still judged against
the ticking target's own fresh number and the aggregate is what gets *reported*
— read the module docstring before "finishing" that, the split is deliberate.
Three money bugs fell out of building it: currencies were being compared after
all, the relative baseline came from whichever shop loaded first, and it was
taken *before* the answers pinned the product, so "10% cheaper" measured a
headset while the watch followed a console. All three are fixed, all three were
invisible offline, and the third only shows up at `confirm`.

**Two bugs only the live run could find.** `watched_ids` absent and empty are
different — empty means "the answers ruled this shop out", and conflating them
made two Israeli shops fall back to a ₪29 game for a console watch; empty is
now `unavailable`. And **Amazon's identity is `data-asin`**: its links are
sponsored-click redirects with a fresh base64 blob every request, and its plain
links embed the result *position* in the path (`/ref=sr_1_3`), so every pinned
product vanished on the next check. `data-uuid` and `data-index` are
deliberately excluded — Amazon sets both, and both are per-render.

**`docs/vacancies-roadmap.md` is the vacancies analysis** (2026-08-04): what
the `presence` kind does today, what a check costs, and why the owner's
"personalised, not just any match" idea is affordable. Three things from it
worth knowing without opening it:

- **A vacancy check is a plain HTTP GET with no model — $0.0000041, or
  $0.012/month at 15-minute checks.** Cost is *not* the constraint for this
  feature, unlike shares. The Israeli job boards stayed `http` in the 8b live
  run; a JS-rendered board (LinkedIn) would cost 45× and still be under $2.
- ~~**The triggered email says `What was found: 1`.**~~ Fixed 2026-08-04.
  `count` now carries `items` (text, href, and a stable sha1 `id`), the email
  lists the jobs with links, and the plan card says "50 listed today, 5 of
  which match". **Repeating watches** shipped with it: a `presence` watch
  keeps running, reports each posting once, and expires after 90 days —
  the first thing here that does not stop by itself.
- **Judging costs per new posting, not per check**, so personalisation is
  cheap: ~$0.03/month for a niche query against $49/month if a model ran every
  tick. It also *fixes* the kind's worst failure mode — a text filter cannot
  be verified against a posting that does not exist yet, so the selector
  should be deliberately loose and the model should decide.

**Vacancies steps 1 and 2 are done** (2026-08-04) — matched items, the
plan-time preview, repeating watches with per-item dedup, and a 90-day term.
**Target selection is solved too** — there is now a `jobs` kind backed by
`shared/job_boards.py`, a registry exactly like `sources.py`. A blacklist was
rejected: it treats the symptom, and the searching Planner would pick a
different unusable board next week. **LinkedIn works keyless and without a
browser** via `/jobs-guest/jobs/api/seeMoreJobPostings/search` — verified from
a Lambda for Beer Sheva *and* New York — plus `drushim.co.il` for Hebrew
listings. A jobs request no longer runs Sonnet-with-web-search at all, so the
price went **down**: $0.012/month. The unverifiable `:-soup-contains(...)`
filter is gone rather than mitigated, because the board filters server-side.

The original failing request now plans: `kind jobs, repeating True`, two HTTP
targets, 35 + 10 listings, $0.0119/mo. Live: `new=10/10 new=25/25` → 2 emails,
then `new=0/…` three ticks running → silence.

**Step 3 is done too** — `shared/rank.py` judges what appeared against the
whole request and reports it best-first with a score and a reason. **Paid per
notification, not per check**: $0.19/month for a jobs watch firing twice a day
against $16.42 to judge every tick, which is what keeps this from undoing
Phase 8b. Three rules not to undo: ranking **never withholds a job** (only
outright-irrelevant items are held back), it **never blocks a notification**
(any failure sends the items unranked), and **what it sets aside is still
remembered** — remembering only what was reported would bring every rejected
posting back on the next tick forever. It reads the summary *card*, so hours
and pay are invisible; the prompt says an unstated criterion is not a failed
one. `llm.py` moved from `planner/` to `shared/` so the Checker could reuse
`ask()` rather than grow a second `client or Anthropic()`.

**Step 4 is done, and the roadmap was wrong about it.** Clarifying questions
were deferred behind multi-turn chat (4d); they need none. `shared/questions.py`
builds them **from what the search actually returned**, so every option is a
property real postings have, with the ids of the items it covers — narrowing
today's list is an exact set intersection. They live on the plan card and the
answers travel with confirm.

**The one thing refused:** answers must NOT filter future postings. They
describe *today's* results; a hard filter built from them would silently
exclude tomorrow's good job and count zero while looking healthy — the bug
class that broke the presence text filter. So they filter today's visible list
and become *ranking preferences* (`as_criteria` → `rank.py`) from then on: a
future job that misses one scores lower, never disappears. Two prompt rules
came from watching it get this wrong live: options must describe **items**, not
flexibility ("Open to other cities [29]" carried only the jobs *not* in the
city, hiding the local one), and must be **durable** ("How recent should the
posting be?" is meaningless for a posting that appears tomorrow).

**Three findings no offline test could have produced.** LinkedIn rewrites every
job link on every response (`refId`), so identity must key on
`data-entity-urn` — otherwise a repeating watch re-reports every job every
tick forever. LinkedIn's guest endpoint **alternates between two result sets**
(19 distinct jobs across 5 fetches), which is harmless only because
deduplication exists. And `shared/fetch.py` had been **paying 45× for
nothing** — it never decompressed gzip, so ordinary pages arrived as line
noise and escalated to Chromium. $3.22/mo → $0.003/mo on the same watch.
`shared/test_fetch.py` and `shared/test_job_boards.py` are new.

**`docs/shares-roadmap.md` is the finish-the-shares plan**, written 2026-08-04
with the arguments and the evidence, including three things it recommends
*not* building (holiday calendars, quote self-healing, a paid data API).
**Tiers 1, 2 and 3 are done and proven live**; the second-source item was
withdrawn by the owner and the recurring-watch idea is shelved as optional.
What is left on it is Tier 4 (a `Checks` history table), deliberately after
the frontend.

Two corrections the owner made on 2026-08-04, both recorded in §6:

- **"Change from current" was a test request.** Real ones are "5% down",
  "7% down", "goes up from current". Note that **"goes up from current" is
  still `relative_change_pct: 0`**, so the fires-at-the-open warning still
  applies to it.
- **"Stamp each time it drops 5%" is not the history item — it is a recurring
  watch, and that does not exist.** Today a watch that fires is finished: the
  Notifier emails, deletes the schedules, and `triggered` is terminal. There
  is no "while the watch is still running". Building it needs a **re-arm
  rule** decided first (does the baseline reset to the new price or stay at
  the original? those mean very different things), repeat-alert suppression,
  and an end condition, since a recurring watch bills forever where a one-shot
  stops by itself.

## What this project is

`schedule-ai-app`: an agentic worker that watches the web for a condition
(a price, a restock, a status change) and notifies the owner when it becomes
true. It's a personal learning project — the explicit goals are hands-on
experience with AWS serverless (Lambda, EventBridge Scheduler), agentic AI
design, Terraform/IaC, and GitHub Codespaces. See README.md for the stack
and repo layout.

## How the owner wants to work

- Decisions get made by discussion first, code second. Don't jump straight
  to implementation on anything architectural without checking in.
- Learning style: build first, explain concepts in context right when we
  hit them, rather than upfront theory.
- The owner is new to AWS, Terraform, and Codespaces specifically (some
  prior general AWS exposure, no Terraform or Codespaces before this
  project) — explain concepts as they come up, don't assume familiarity.
- Prefers the graphical Claude Code panel (VS Code extension) over the raw
  terminal CLI when both are viable.

## Decisions already made, and why

- **Single repo, personal GitHub account, public visibility.** No
  organization, no split infra/app repos — simplicity for a solo project
  that also doubles as a portfolio piece.
- **Python for all Lambdas** (Planner, Checker, Notifier, chat handler).
  **React + TypeScript** for the frontend.
- **Two-tier agent design** (the owner's own idea): a **Planner** (Claude
  Sonnet, with the `web_search` tool) runs once per watch to resolve target
  URLs, an extraction strategy, and a check interval. A **Checker** (Claude
  Haiku, no search) runs on every scheduled tick, one per target — cheap
  and frequent, on purpose. This is a real "planner/executor" pattern.
- **EventBridge Scheduler** (not EventBridge Rules) — one dynamic schedule
  per watch target, created at runtime.
- **Terraform remote state**: S3 bucket + DynamoDB lock table, created once
  by `terraform/bootstrap`, the one module that intentionally uses local
  state since it creates the backend everything else will use.
- **IAM**: the `schedule-ai-terraform` user gets permissions added one at a
  time, per phase, as each new AWS service is introduced — deliberately not
  broad access up front. AWS-managed: `AmazonS3FullAccess`,
  `AmazonDynamoDBFullAccess`. Phase 2 added *custom scoped* inline policies
  instead of managed ones (the owner chose scoping over
  `IAMFullAccess`/`AWSLambda_FullAccess`), each restricted to
  `schedule-ai-app-*` resources: IAM role management, Lambda function
  management, CloudWatch Logs read, EventBridge Scheduler read/delete.
  Expect to discover missing actions by hitting `AccessDenied` — that's the
  intended tradeoff of tight scoping.
- **Secrets**: AWS and Anthropic credentials live only as *repository-level*
  GitHub Codespaces secrets (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `AWS_DEFAULT_REGION`, `ANTHROPIC_API_KEY`) — repo-level, not personal
  account-level, specifically so an unrelated future project can't collide
  with or overwrite these.
- **Claude Code's own billing**: Claude Code itself (this agent, doing the
  coding) should authenticate with the owner's Pro/Max subscription, not
  `ANTHROPIC_API_KEY`. That key is reserved for the app's own runtime calls
  (Planner/Checker), so dev-tool cost and app-runtime cost stay on separate
  bills. If a session prompts to approve the API key, decline it.
- AWS budget alarms are set at $50 / $100 / $200.

## Fetching strategy (decided in Phase 6, with evidence)

A headless-browser probe was run from the Codespace against the sites that
failed in Phase 2. Result, and the reason the design looks like it does:

- **A browser fixes JS rendering.** steamdeck.com and the Steam store page
  went from unreadable to yielding real prices (`779,00€`, `919,00€`).
- **A browser does not fix bot protection.** Best Buy reset the connection
  (`ERR_HTTP2_PROTOCOL_ERROR`) and Amazon interrupted navigation, against a
  real Chromium. They fingerprint datacenter IPs; Lambda's are worse than
  the Codespace's. Beating that needs residential proxies, i.e. a paid
  scraping API.
- **Decision: build the browser Lambda, skip the paid API.** It is free,
  unlocks the large class of JS-rendered-but-unprotected sites, and teaches
  container-image Lambdas. Amazon and Best Buy are accepted as out of
  reach; the Planner's prompt now steers away from them.
- The EUR prices above are a Codespace-geo artifact. A Lambda in
  `us-east-1` should see USD, which incidentally resolves the Phase 2
  currency gap.

`fetch_method` (`"http"` | `"browser"`) is chosen per target by the
Planner and stored on the `WatchTargets` row. The browser lives in its own
Lambda so that ordinary HTTP checks never pay Chromium's ~2GB memory and
multi-second cold start.

## Open decisions, not yet finalized

- Auth on the eventual web UI: currently planned as a single shared
  passcode (no Cognito), since it's single-user for now.
- Fetching JS-heavy or bot-protected target sites: starting with plain
  HTTP GET; expect this to break on some real targets, to be revisited
  with a headless browser or a scraping API once we know which sites
  actually need it.

## Known gaps (deferred on purpose)

Deliberately not fixed yet — the plan is to get the full product shape
working first, then do a hardening pass. Ordered roughly by when they
will start to hurt.

### Blocking for Phase 4

- **There are no watch lifecycle operations at all.** The only way to
  create a watch is to invoke the Planner; the only way to list, pause,
  cancel or delete one is manual DynamoDB and Scheduler surgery — which
  is literally what was done to clear the Phase 2/3/6 test rows in this
  session. A UI cannot ship without `list`, `get`, `pause`/`resume` and
  `delete`, and `delete` in particular has to remove the target rows,
  the watch row, *and* the EventBridge schedules, or it leaks billing.
  This is Phase 4's real first task, ahead of any React.
- **`user_id` is hardcoded to `"default"`.** Fine while the passcode
  makes it single-user, but every query will need revisiting the moment
  a second person exists. Worth keeping the field rather than removing
  it.
- **The Planner is slow enough to break a synchronous HTTP API.** The
  Phase 6 run took 19.5s (web search + Sonnet + schedule creation).
  API Gateway HTTP APIs cap at 29s. A synchronous `POST /watches` is
  one slow web search away from timing out, and the client would have
  no idea whether the watch was created. The `planning` status already
  in the schema exists for exactly this: return 202 immediately, let
  the client poll.

### Correctness and robustness

- **Planner JSON parsing is fragile.** `plan.py` assumes the last text
  block is clean JSON. Observed failing for real once in Lambda
  (`JSONDecodeError` on an empty last block); the same request succeeded
  on retry. Fix later with forced structured output (a tool call) or a
  retry-on-parse-failure loop. `checker/check.py` already uses a slightly
  sturdier outermost-`{...}` parse.
- ~~**The Notifier leaves stale `schedule_arn` values behind.**~~ Fixed in
  Phase 5. It now `REMOVE`s the field on the same pass that deletes the
  schedule, so the table stops claiming a watch points at a schedule that
  no longer exists.
- ~~**The Notifier's GSI query is not paginated.**~~ Fixed in Phase 5.
  `_all_targets()` follows `LastEvaluatedKey`. It was unreachable with 1–3
  targets per watch, but the failure mode — half a watch's schedules left
  alive and billing forever — was worth four lines.
- **Every Lambda has tests; the chain still does not.** 673 of them (counts
  in "Current status"). What is missing is any test that runs the whole chain
  end to end — Planner → schedule → Checker → event → Notifier is still
  verified only by invoking real Lambdas, and the 2026-08-02 live run found
  **two bugs that all 378 tests of the time could not see.** Both are written
  up under Phase 9; the short version is that a suite where every test injects
  its collaborators cannot see a wiring bug, and a per-action IAM policy fails
  on the action you added last.

  Two conventions that are load-bearing rather than stylistic. **Three
  Lambdas have a module named `handler`**, so a suite must load its own by
  path under a unique name (`importlib.util.spec_from_file_location`) — a
  plain `import handler` hands whichever one pytest imported first to all of
  them, silently, with the tests still green. And **`boto3` is stubbed
  unconditionally**, not behind a `if not installed` guard: a guard makes the
  result depend on collection order and on whether the machine has a real
  boto3 with a usable region, which a CI runner does not. Every suite must
  pass alone and in any order — `for d in */; do pytest $d; done`.

### Product shape

- **Nothing keeps a *history* of what was checked.** `last_value` is still
  overwritten on every tick, so there is no way to draw "the price over the
  last month". `docs/shares-roadmap.md` §2.6, deliberately after the frontend.

- ~~**Nothing notices a value that has stopped moving.**~~ Fixed 2026-08-04.
  The Checker records `last_changed_at` and `unchanged_checks`; the API
  computes `stale` at read time; the UI says "last moved 3 h ago" and warns
  when a windowed target goes a whole session without a tick. Three things
  not to undo: it **reports and never acts** (a still value is normal for most
  watches — acting on it re-creates the false positive `unavailable`/`failed`
  exists to prevent); the threshold is **one trading session**, derived from
  the window rather than a constant, and a target with no window gets no flag;
  and the comparison is on the **raw text**, because
  `Decimal("306.49") == 306.49` is False and comparing parsed numbers would
  report a change on every check of a static price.

- ~~**Non-US symbols are silently wrong.**~~ Fixed 2026-08-04.
  `sources.describe()` reads `exchange`, `currencyCode` and `name` out of the
  response already fetched, stores them on the target and shows them on the
  plan card, so "SAP" resolving to the **NYSE ADR in USD** is now visible
  before confirming. An uncovered symbol (`WALMEX-MX`; CNBC does not carry the
  Bolsa Mexicana at all) raises `NotCovered` with a sentence instead of
  `no key 'last' at ...`. **Minor units are deliberately not converted** — Tel
  Aviv quotes in agorot and London in pence, so Bank Leumi genuinely reads
  7,377 rather than 73.77, which is what TASE itself displays. A relative
  condition has no units; an absolute one ("below 70 shekels") would compare
  against 7,377 and fire instantly. That trap is open.

- ~~**The trading window is hardcoded to New York.**~~ Fixed 2026-08-04.
  `sources.EXCHANGE_WINDOWS` maps the exchange to a window and
  `QuoteKind.window` is now only the fallback. `tase_hours`
  (**SUN–THU**, `Asia/Jerusalem`), `xetra_hours` and `lse_hours` added.
  Proven live: "Bank Leumi on the Tel Aviv Stock Exchange" → `LUMI-IL` →
  `cron(*/5 9-18 ? * SUN,MON,TUE,WED,THU *)`. An exchange missing from the
  table degrades to US hours rather than raising.

- **The quote source has no fallback, and the owner has accepted that risk.**
  `shared/sources.py` holds one URL with `self_heals = False`. On 2026-08-04
  CNBC served the Lambda normally and returned **HTTP 403 to the Codespace**,
  so the IP-blocking that already ended Yahoo and stooq is live on the current
  source. If AWS's range is blocked, every share watch breaks at once and it
  looks like per-watch extractor failure. **Judged not a real risk by the
  owner on 2026-08-04**; the second-source work is withdrawn, not forgotten.
  `docs/shares-roadmap.md` §2.2 and Tier 2 item 4.

- **A `product` watch drops shops that price in another currency**, and this
  is the right behaviour only until there is a user. Nothing converts money,
  deliberately — a stale exchange rate is a confident email about a bargain
  that is not one. The real fix is the user's own country and currency, which
  the owner decided on 2026-08-05 belongs with auth: ask at sign-up, or read
  it off the Google account. Until then an Israeli watch simply has no Amazon
  in it, and says so on the plan card. `docs/marketplaces-roadmap.md` §7.

- **"Just tell me the cheapest, every morning" is still inexpressible**, on
  purpose. There is no threshold in that sentence, so it is not a condition
  watch — it is a scheduled report, and the trigger it needs is Phase 9 step
  3b, the same one calendar reminders need. The `readings` payload the email
  now carries is what it will use. Building a second firing mechanism to serve
  one phrasing would have been the expensive way there.

- **A watch cannot be edited.** No changing the threshold, the interval,
  or a bad target URL — the only recourse is delete and re-plan, which
  pays for a fresh Sonnet call and web search.
- **No partial-failure handling in the Planner.** If schedule creation
  fails halfway through a multi-target plan, earlier targets keep their
  schedules and the `Watches` row is never written.

### Operations and cost

- ~~**A permanently-failing Notifier retries for a day.**~~ Capped in
  Phase 5. Both EventBridge targets carry `retry_policy` — 8 attempts over
  one hour instead of ~185 over 24. This is a **retry cap, not a DLQ**: a
  notification that can never succeed is now *dropped* rather than
  retried forever, and the `notifier-errors` alarm is the thing that tells
  you it happened. A real DLQ keeps the event and needs SQS permissions the
  deploy user does not have.
- ~~**Every tick pays for a Haiku call.**~~ Removed in Phase 8b — that was
  the whole point of the phase. Tier 0 runs a compiled extractor with no
  model call; a model appears only on `failed`, once, via 8d's repair.
- ~~**Lambda zips are built for whatever machine ran `build.sh`.**~~ Fixed
  in Phase 5. `planner/build.sh` and `checker/build.sh` now pass
  `--platform manylinux2014_x86_64 --implementation cp --python-version 3.12
  --only-binary=:all:`, so the wheels are pinned to the Lambda runtime
  rather than to whatever built them. Both zips were rebuilt and redeployed
  under the new flags. `api/` and `authorizer/` have no binary dependencies
  and were left alone.
- **The `anthropic` bundle is vendored twice.** Planner and Checker each
  carry ~7.7MB of the same dependency tree. A Lambda Layer would
  deduplicate it. Cosmetic at this size; worth it if a fourth zip
  Lambda appears.
- **The Anthropic API key lives in Terraform state and Lambda env vars.**
  `sensitive = true` only hides a value from CLI *output*; it is stored
  in plaintext in the state file (S3, `encrypt = true`) and readable by
  anyone with `lambda:GetFunctionConfiguration`. Acceptable for a
  single-user project with a scoped IAM user; the real fix is Secrets
  Manager or SSM Parameter Store fetched at runtime.
- **The Fetcher is over-provisioned.** 2048MB allocated, **887MB peak**
  across 25 varied renders (measured 2026-08-02; the older "915MB" came
  from a single REPORT line, which is a container high-water mark and so
  says less than it appears to). Do not just lower it — Lambda scales CPU
  with memory, and cost is memory × duration, so less memory can render
  slower and cost the same or more while creeping toward the 60s
  timeout. Now cheap to settle: the sequence probe in `docs/phase-5-plan.md`
  run at 1280 / 1536 / 2048. 1024 sits below the observed peak.

## Current status

**As of the end of 2026-08-04.** Phases 0–3, 6, 4a, 4b, 8a, 8b, 8d and 5 are
complete. Phase 9's kinds and schedule windows are built, deployed and proven
live; what is left of it is the *time-triggered* half (`once`, `reminder`) and
delivery channels. The owner's three product features are done except
marketplaces steps 3–4. 8c is deferred with numbers. Still open: 4c (the
designed chat UI) and the deploy half of 7.

**Five watch kinds exist**, and three of them never touch a web search:

| kind | where the target comes from | searches? |
|---|---|---|
| `quote` | `shared/sources.py` — CNBC, per exchange | no |
| `jobs` | `shared/job_boards.py` — LinkedIn guest API, drushim | no |
| `product` | `shared/shops.py` — Ivory, Bug, Amazon | no |
| `presence` | web search, compiled counter | yes |
| `value` | web search, compiled extractor | yes |

**673 offline tests**, ~2s, no AWS and no cost: `python -m pytest -q` from
the repo root. By area — `shared/` 325, `planner/` 132, `api/` 80,
`authorizer/` 24, `checker/` 71, `notifier/` 35, `fetcher/` 6. They run on
every push (`.github/workflows/tests.yml`).

**The whole cycle was proven live on 2026-08-02, both kinds of watch.**
`w_b9b8efab` (value): planned a Steam browser target, verified `$789.00`,
confirmed at 2 min, checked with **no model call**, met its condition,
emailed and deleted its own schedule — 43 seconds end to end.
`w_fbd02db8` (quote): classified as `quote`, resolved AAPL through
`shared/sources.py`, verified `$308.91` on the wire, derived its relative
threshold from *that* reading, and was scheduled
`cron(*/5 9-16 ? * MON-FRI *)` in `America/New_York`. Both deleted after.

**Re-proven on 2026-08-04, after the windowed-schedule fixes.** `w_46310d2d`
(quote, AAPL): planned in 4s, confirmed at **60 min** — the interval that
returned 500 nine times the night before — producing
`cron(0 9-16 ? * MON-FRI *)` in `America/New_York` and reporting
`next_check_at: 17:00Z`. Retuned to 5 min via PATCH, which moved the answer to
`16:35:00Z`; the Checker fired at **16:35:05**, read `306.49` against a
baseline of `306.40` with `model=False`, met its condition, sent **one** email
and deleted its own schedule. `zoneinfo` works in the Lambda runtime — worth
knowing, since `next_fire_after` degrades to `None` rather than crashing if it
ever does not.

Earlier proofs, kept because they cover paths the 2026-08-02 run did not:
Phase 2 `w_ea349f2f` (Scheduler invoking the Checker unprompted every 5
minutes), Phase 3 `w_68c179cb` (`WatchTriggered` → Notifier → email →
teardown in ~1s), Phase 6 `w_cd9975d8` (the Planner steering away from
Amazon and Best Buy on its own), 8d `w_71eab15f` (a corrupted `scope`
repaired by Haiku for $0.008, then three failures → `degraded`).

Live AWS resources: `schedule-ai-app-watches` /
`schedule-ai-app-watch-targets` (DynamoDB); `schedule-ai-app-planner`,
`-checker`, `-notifier`, `-fetcher`, `-api`, `-authorizer` (Lambda, the
Fetcher a container image); `schedule-ai-app-fetcher` (ECR, with an
untagged-image expiry rule); `schedule-ai-app-bus` +
`schedule-ai-app-watch-triggered` and `-watch-degraded` rules
(EventBridge, both targets retry-capped); a `schedule-ai-app` HTTP API
with a `$default` stage; a verified SES identity; an S3 bucket +
CloudFront distribution for the frontend; the `schedule-ai-app-alarms`
SNS topic with a confirmed email subscription and three CloudWatch
alarms; and seven IAM roles (one per Lambda, plus
`schedule-ai-app-scheduler-invoke-checker` that schedules assume).

**There are no schedules** as of 2026-08-05, and the tables are empty —
verified by scan after the step-3 live run, which deleted both of its watches.
The system is idle and billing nothing.

**The API is live** at the `api_endpoint` Terraform output
(`https://0xz7v8yx0i.execute-api.us-east-1.amazonaws.com`). Every request
needs an `Authorization` header carrying the passcode. That passcode is a
SecureString at SSM `/schedule-ai-app/passcode`, created **outside
Terraform on purpose** so it never enters the state file — deliberately
unlike `ANTHROPIC_API_KEY`. Read it with
`aws ssm get-parameter --name /schedule-ai-app/passcode --with-decryption
--query Parameter.Value --output text`; rotate it with `put-parameter
--overwrite` (the authorizer caches it per execution environment, so a
rotation takes effect on the next cold start).

**Measured Phase 6 numbers**, worth keeping for cost work: Fetcher cold
start 1544ms init / 10.2s total, warm renders ~4.7s, 915MB of 2048MB
used. A full browser check (Fetcher + Checker + Haiku) costs roughly
$0.0057, of which ~97% is the Haiku call — the browser is not the
expensive part. The expensive part is `check_interval_min`, which the
Planner chooses on its own: a single target at 5-minute intervals is
about $50/month, at 10 minutes about $25, at 60 minutes about $4.

**Container-image Lambdas do not redeploy via `terraform apply`.**
Terraform stores only the image *URI*. Push a new image to the same
`:latest` tag and the URI string is unchanged, so Terraform reports no
changes while the running code stays stale. Updating the Fetcher needs
`aws lambda update-function-code --image-uri ...` (then
`aws lambda wait function-updated`), or a versioned tag instead of
`:latest`. This bit us once during Phase 6 and will again.

**IAM note:** the `schedule-ai-terraform` user's inline policies hit AWS's
2048-character *aggregate* limit during Phase 3. They were consolidated
into one customer-managed policy, `schedule-ai-app-terraform`, and the
inline ones deleted. Add future permissions there. It uses action
wildcards (`lambda:*`, `iam:*Role`) to fit, but every statement is still
scoped to `schedule-ai-app-*` resources.

## Roadmap

Listed in **execution order**, which is not numerical order. Phase numbers
are stable because the docs and commit history refer to them; the order they
get built in is a separate decision, revised as the project learns things.
Phase 6 was pulled ahead of 4, and Phase 8 is now pulled ahead of 5, 4c and 7.

| | Phase | State |
|---|---|---|
| ✅ | **0** · Environment & IaC foundation | done |
| ✅ | **1** · Planner, offline | done |
| ✅ | **2** · Serverless core | done |
| ✅ | **3** · Notifications | done |
| ✅ | **6** · Headless browser | done — pulled ahead of 4 |
| ✅ | **4a** · Lifecycle API + authorizer | done |
| ✅ | **4b** · Hosting + minimal React | done |
| ✅ | **8a** · Budget guardrails | done |
| ✅ | **8b pass 1** · Extraction engine | done — `shared/extract.py`, 95 tests |
| ✅ | **8b pass 2** · Wire Planner + Checker | done — verified live, 222 tests |
| ✅ | **8b pass 3** · Cheap fetch by default | done — browser only where proven necessary |
| ⏸️ | **8c** · Conditional GET | **deferred** — saves ~$0.05/mo, unsound on browser |
| ✅ | **8d** · Tiered self-heal | done — verified live, repair $0.008 |
| ✅ | **5** · Production hygiene | done — 3 alarms live, IAM unblocked, 4 gaps closed |
| 🔨 | **9** · Watch kinds, schedules, delivery | kinds + windows done and proven live; left: `once`/`reminder` (time-triggered) and delivery channels |
| ✅ | **10a** · Shares finished | tiers 1–3 done — exchange, baseline, staleness. Tier 4 (history) after the frontend |
| ✅ | **10b** · Vacancies finished | all four steps — items, repeating+dedup, ranking, grounded questions |
| 🔨 | **10c** · Marketplaces | steps 1–3 done (shops, `offers`, Amazon, pinning, one reading); **4 next** |
| ⬜ | **11** · Calendar reminders | `.ics` is easy; the *trigger* is Phase 9 step 3b and does not exist |
| ⬜ | **4c** · Designed chat interface | the side quest, deliberately late |
| ◐ | **7** · CI/CD via GitHub OIDC | tests-on-push done; the **deploy** half stays last |

**Phases 10a/10b/10c are the owner's product plan**, not a renumbering of the
original roadmap — see "Where the product is going". They are one engine used
at five levels of trust in the source: `quote` and `jobs` and `product` are
registry-driven, `value` and `presence` still search.

9. **Watch kinds — next.** `planner/plan.py` is 634 lines of which ~215 are
   prompts, and `SEARCH_PROMPT` now carries the rules for three request types
   at once. Two shipped bugs were **rules in one prompt interfering** (a
   presence watch could not be planned; a relative threshold was fabricated),
   so the argument for splitting is a measured failure rate, not tidiness.
   Three axes get separated: what makes a watch fire (condition vs **time** —
   a 9am reminder cannot be expressed today), where its target came from
   (search vs registry), and how the owner is told (email vs calendar vs
   chat). Market hours become a `cron(...)` + timezone schedule so **no code
   in the Checker learns what a stock market is**. Note the honest correction
   recorded in the doc: windows are a *correctness* fix worth ~$0.14/month,
   not a cost fix — the $5 budget was never binding for HTTP targets.

   **Step 1 of 5 is done.** `plan.py` is 219 lines instead of 634;
   `planner/kinds/` holds `base.py` (a four-method `Kind`), `value.py` and
   `presence.py` behind a registry, with `llm.py` for model plumbing and
   `prompts.py` for the kind-agnostic prompts. Behaviour is unchanged and the
   33 Planner tests were only re-pointed at the moved symbols, not rewritten.
   Deployed and smoke-tested. Two things to know before step 2:

   - **Schedules belong to the watch, decided 2026-08-02.** A reminder stores
     `targets: []` and its schedule invokes with `{"watch_id": ...}`;
     condition-triggered watches keep `{"target_id": ...}`. The cheap
     alternative — a synthetic target row — was refused because it makes the
     table describe something that does not exist, the same class of lie
     removed from the Notifier in Phase 5.
   - **The zip is flat, so `shared/kinds.py` would collide** with the
     `kinds/` package directory. Name any shared kind module `watch_kinds.py`.

   **Step 2a and the window are done and deployed.** Adding `quote` broke the
   step-1 abstraction immediately and usefully: `Kind` was four methods about
   *compiling* an extractor, and a quote compiles nothing, so it would have had
   to stub all four. The axis moved instead — `Kind.resolve()` is now the one
   method every kind implements, `CompiledKind` adds the four and implements
   `resolve` in terms of them, and `QuoteKind` extends `Kind` directly. The
   `known_source` if-statement in `planner/handler.py` is gone.

   `shared/schedules.py` builds `rate(...)` or `cron(...)`+timezone, and
   `cost.py` now prices from real checks-per-month instead of assuming 43,200
   — a windowed watch was being quoted ~4x high. **Two corrections worth not
   re-making**, both recorded in the plan doc: market-hours windows are *not*
   a cost win (the $5 budget was never binding for HTTP, the saving is 14¢),
   and they are *not* a correctness win either — a frozen out-of-hours quote
   is the last real price, so it causes no false fires. The real reason is
   that a 1-minute quote watch makes 33,120 pointless requests a month to a
   free third-party endpoint we cannot afford to lose. The genuine
   correctness bug found while checking that: **a watch created outside
   trading hours takes its baseline from the previous close**, so "goes down
   from the current" asked on a Sunday measures against Friday. Still open.

   **Step 2b is done, and the phase's own success criterion is finally met.**
   Routing left the prompt: `planner/classify.py` picks the kind with one
   small Haiku call before anything expensive runs, and `Kind.plan()` is how
   each kind turns a request into targets. **`SEARCH_PROMPT` went 4,686 →
   3,545 characters**, losing the KNOWN SOURCES and WATCH SHAPE paragraphs;
   it is now *handed* `watch_shape` instead of deciding it. A quote never
   runs Sonnet-with-web-search at all now — one Haiku call for condition and
   cadence, and the target is the symbol.

   The doc's "rules first, model second" did not survive contact: no lexical
   rule separates "how much is Apple" from "how much is an Apple pencil at
   Best Buy". The rules became **gates around the answer** — kind must be
   registered, a `quote` must carry a symbol `sources.py` accepts — and
   anything failing a gate degrades to `value`. A misclassification must cost
   a suboptimal plan, never a rejected request.

   **Proven end to end on 2026-08-02, and the live run earned its keep.** Two
   watches: `w_b9b8efab` (value) planned a browser target, verified $789.00,
   confirmed at 2 min, checked with `model=False`, met its condition, emailed
   and tore its schedule down — the full loop in 43 seconds. `w_fbd02db8`
   (quote) classified as `quote`, resolved AAPL through the registry, verified
   $308.91 on the wire, computed its relative threshold from that reading, and
   got `cron(*/5 9-16 ? * MON-FRI *)` in `America/New_York` — the window, live.

   **Two bugs that 378 offline tests could not see, both from this session:**

   - `AttributeError: 'NoneType' object has no attribute 'messages'` on every
     non-quote plan. Moving the compile step out of `plan.py` dropped a
     `client or Anthropic()`. No test caught it because **every test passes a
     scripted client — the one argument that is never None in a test and
     always None in the Lambda.** The default now lives in `llm.ask` alone.
   - The Phase 5 `schedule_arn` cleanup needed a `dynamodb:UpdateItem` the
     Notifier's role did not have. The email had already gone out, so the
     AccessDenied failed the invocation *after* the side effect that matters,
     and EventBridge retried: **three duplicate emails to a real inbox.** Both
     halves are fixed — the permission is granted, *and* the tidy-up is now
     swallowed, because nothing after the email may re-notify a human.

   The lesson worth keeping: a suite where every test injects its
   collaborators cannot see a wiring bug, and a Lambda whose IAM is scoped
   per-action will fail on the action you added last, at the worst moment.

   **Step 2b's window then failed its first overnight use, on 2026-08-04, and
   not in the way anyone was watching for.** Full write-up in
   `docs/phase-9-watch-kinds.md` §10b. The owner made an Apple watch at 23:33
   Israel time and woke to nothing. **The schedule was correct**: 23:33 Israel
   is 16:33 New York, three minutes past the last slot of `9-16` on a Monday,
   so the first check was 09:00 Tuesday — and the watch was deleted at 11:24
   Israel, four and a half hours before it would have run. Nothing was broken
   and nothing said so.

   - **The fix is a sentence, not a schedule.** `schedules.next_fire_after()`
     computes the first fire; `confirm`, `PATCH` and `GET /watches/{id}` all
     return `next_check_at`, and the UI renders "next check 16:00, in 16 h".
     It is **computed, never stored** — right for about one interval, then a
     lie, which is what Phase 5 removed from the Notifier.
   - **A guardrail that returns 500 is an outage.** `confirm` answered
     `500 ValueError: a windowed schedule cannot use a 60-minute interval`
     **nine times in twenty minutes** to someone asking for an hourly quote
     watch. The refusal was right that a cron *minute* step cannot express an
     hour (`*/60` collapses to `0`) and wrong that nothing could: a cron
     *hour* step always could — `cron(0 9-16 ? * MON-FRI *)`.
   - **Inexpressible intervals snap up, never down.** 51 minutes is not exotic
     — `cost.py` derives the interval floor from a monthly budget, so an
     arbitrary number arrives on the ordinary path. Up is the safety argument:
     fewer checks, so a snapped schedule can only cost less than the estimate
     the budget gate approved.

   The lesson: **a correct system that explains nothing is a broken product.**
   No test could have caught this — every assertion about the schedule was
   true. Check the other places the product knows something the user does not.

Details of the finished phases:

0. Environment & IaC foundation — **done**
1. Planner, offline — **done.** Prove the Planner logic works before
   touching infrastructure.
2. Serverless core — **done.** Planner + Checker in Lambda, DynamoDB
   tables, EventBridge Scheduler wired end-to-end.
3. Notifications — **done.** Checker emits `WatchTriggered` onto a custom
   bus, a rule routes it to the Notifier, which emails via SES and then
   deletes that watch's schedules.
6. Headless browser — **done.** Fetcher Lambda (container image on ECR)
   renders JS pages; the Planner picks `fetch_method` per target. Taken
   out of order, ahead of Phase 4, so the app would actually work on real
   sites before it got a nice interface.
4. API + web chat UI — **4a and 4b done, 4c deferred behind Phase 8.**
   4a: lifecycle API, authorizer, HTTP API. 4b: S3 + CloudFront + a
   minimal unstyled React app. 4c is the designed interface and is
   deliberately last-but-one: it is better built against the data model
   Phase 8 leaves behind than retrofitted to it.
8. **Cheap checks — current.** See `docs/phase-8-cheap-checks.md`.
   The Checker stops calling a language model on every tick; the Planner
   compiles a deterministic extractor instead of describing a task in
   English. Target: ~$0.06/month for a 3-minute watch against ~$82 today.

   **8a is done.** `shared/cost.py` is the single definition of what a
   check costs, vendored into three zips by their `build.sh`. Limits are a
   **monthly budget per watch** (`MONTHLY_BUDGET_USD`, default $5), not an
   interval floor — the minimum interval is *derived*, so when 8b cuts
   per-check cost by ~1000× the same budget will permit 1-minute checks
   with no constant to change. Three gates: the Planner clamps whatever
   interval the model proposed (keeping `planner_interval_min` and
   `min_interval_min` on the row), `confirm` refuses an unaffordable one
   before creating any schedule, and `PATCH` enforces the same budget so
   it cannot be walked around. The Checker publishes `EstimatedCostUSD` to
   CloudWatch per check — spend was previously visible nowhere, since AWS
   budget alarms cannot see Anthropic charges.

   Verified live: the Planner proposed 30 min (~$8.45/mo), the floor
   computed 51, the stored interval was clamped to 51 at $4.97. Confirm at
   3 min was refused and created no schedule.

   **8b pass 1 is done.** `shared/extract.py` compiles a typed spec
   (`jsonpath` / `css` / `regex` + a `parse` coercion) into a value, with
   95 tests. Nothing is wired to it yet — the Planner still writes prose
   and the Checker still calls Haiku every tick. Extraction has **three**
   outcomes, `ok` / `unavailable` / `failed`, and never two: `failed` means
   the extractor is broken and 8d escalates it, `unavailable` means there
   is legitimately no value today and 8d must not.

   **8b pass 2 is done and verified end to end against the real account.**
   The Fetcher returns `html` alongside `text`; the Planner opens every page
   it proposes and stores only extractors it has actually run; the Checker's
   Tier 0 executes them with no model call. `shared/condition.py` evaluates
   conditions deterministically — nothing in this codebase ever did that
   before, because the model used to compare in its own head.

   Live proof on watch `w_670564e5`: the Planner rendered the Steam Deck
   page, Haiku read `$789.00`, Sonnet compiled a scoped CSS extractor, and
   `extract.py` verified it before the plan was offered. Scheduled ticks then
   read `789.0` every 3 minutes with `model=False`. **Confirm at 3 minutes
   was refused in 8a and is now accepted at $2.32/month, under the same
   unchanged $5 budget** — which is exactly what expressing the guardrail as
   a budget rather than an interval floor was for. Per check: $0.00587 →
   $0.00019, about 31×.

   Findings worth not rediscovering:

   - **Sonnet 5 runs adaptive thinking by default, and `max_tokens` caps
     thinking *plus* text.** A compile call with `max_tokens=1024` returned a
     lone `ThinkingBlock` and no text, failing as "No text in response" —
     which reads like a malformed reply, not a token ceiling. Budgets in
     `plan.py` are now generous and named.
   - **A whole-document `unavailable_if` is unsound.** "Out of stock" matched
     the Docking Station and a localised string table on the same 1.5MB page,
     reporting an in-stock item as `unavailable` — the one outcome 8d must
     never escalate. `scope` fixes it and doubles as a liveness anchor.
   - **`count` exists because absence is a first-class answer.** A vacancy
     watch is absent by definition until it fires; without `count` it read as
     a broken extractor forever, which 8d would have answered with a Haiku
     repair on every tick (~$237/month at 1-minute intervals).
   - **The Checker is billed for waiting on the browser.** Warm Tier 0 ticks
     took 6545ms, not the 0.4s `cost.py` assumed; `CHECKER_SECONDS_DETERMINISTIC`
     is now split by fetch method. The HTTP figure is still an estimate and is
     flagged in the file as unmeasured.
   - **A presence watch could not be planned at all.** Found by the owner
     running a real request: "tell me when a student cloud engineer vacancy
     appears in Beer Sheva" failed with `nothing to watch`. The Planner
     demanded a literal value on the page before compiling anything, so the
     `count` kind was unreachable in exactly the case it was added for. The
     search step now classifies `watch_shape` as `value` or `presence`; a
     presence watch anchors on a **neighbouring listing** instead of on the
     thing wanted, compiles a counter over the list it sits in, and treats
     `verified_value: 0` as a passing verification. Same class of mistake the
     engine had, one layer up — worth checking for a third time before
     assuming absence is handled everywhere.
   - **Verifying a count at zero proves almost nothing on its own**, so the
     Planner re-runs the item selector with its `:-soup-contains(...)` filters
     stripped and requires a non-zero match. A wrong item class otherwise
     counts zero today, counts zero forever, and never reports a fault. What
     stays unprovable is the text filter itself — nobody can verify a match
     against a posting that does not exist yet. Storing the unfiltered count
     as a health baseline is a good input for 8d.
   - **Cost gates must know whether a tick uses a model.** The api Lambda
     hardcoded `uses_model=True`, so after 8b it overstated the bill 36× and
     would have refused intervals the budget affords.
   **8b pass 3 — cheap fetch by default.** The Planner no longer trusts the
   model's `fetch_method`. It tries a plain GET first, compiles and verifies
   against the raw HTML, and renders in Chromium only when that fails. A
   browser check is **45x** an HTTP one — $0.000186 against $0.0000041, or
   $8.05/month against $0.18 at one-minute intervals — so the choice was too
   expensive to leave to a prompt. Escalation runs both ways: a target marked
   `http` that turns out to need rendering used to be rejected and is now
   retried and kept. Verified live: Steam Deck correctly stays `browser`
   (its price genuinely is not in the raw HTML), the Israeli job boards stay
   `http`.

   **8c is deferred, with numbers.** Conditional GET saves ~$0.05/month at
   100% `304` hit rate, and is **unsound on browser targets** — the value
   there comes from an API the page's JavaScript calls, so the HTML can be
   byte-identical while the price moves. It can only be applied to the half
   that is already nearly free. Revisit if the HTTP share of targets grows.

   Two more failures found by running the product rather than the tests:

   - **The read step was judging, not reading.** Haiku returned
     `literal: null` with the note "priced at $949.00, which does not meet the
     condition of price < $700" — refusing to report a value because the
     condition was unmet. Every watch is created while its condition is unmet,
     so this made planning nearly impossible. `READ_PROMPT` now says so in
     capitals. Reading and judging were one model call before 8b; separating
     them in code is not enough, the prompt has to separate them too.
   - **The Fetcher dies when called in quick succession.** Three renders in
     one warm container reported 1207MB, 1249MB, then 1304MB before Chromium
     died with `TargetClosedError`. Pass 3 made this visible by calling the
     Fetcher in bursts at plan time rather than once per tick. Page, context
     and browser are now closed explicitly and a failed render is retried once
     in a fresh browser. **Diagnosed 2026-08-02 and closed** — see Phase 5's
     plan doc. There is no leak: `Max Memory Used` is a container high-water
     mark that cannot decrease, so that sequence was an artefact of the metric,
     and 25 consecutive renders under the current image hold flat at 887MB.

   **Relative conditions, closed at last.** Found by the owner: "tell me when
   Apple shares go down from the current" produced `price < 313.93` while the
   page said `$333.43`. That threshold is 5% below $330.45 — a figure from
   *search results*, never from the page — and the 5% was invented outright.
   The condition is written during the search step, before any page is opened,
   so asking for an absolute threshold there guarantees a fabricated one. The
   search step now returns `relative_change_pct` and leaves `value` null;
   `resolve_relative_condition()` computes the threshold from `verified_value`
   after the extractor has been proven, and stores `baseline` alongside it.
   "Goes down" is `pct: 0` — **any** decrease. This is the schema gap the plan
   predicted before the phase started and pass 2 failed to close.

   **Still open on that watch:** the extractor CNN yielded reads
   `"Last closed at $..."` — a figure that moves once a day — while the watch
   checks every minute, and the same page shows a live pre-market price the
   watch cannot see. Nothing in the system currently notices that a value is
   stale relative to the check interval. Candidate for 8d or Phase 5.

8d. **Tiered self-heal — done.** `shared/repair.py` puts Haiku back, once,
   and only on `failed`. Never on `unavailable`: repairing an extractor that
   correctly reports "not yet" would pay a model on every tick forever, which
   is the cost the phase exists to remove.

   **Repairs share the watch's monthly budget** rather than getting an
   allowance of their own — one guarantee instead of two, and it self-limits
   with no MAX_REPAIRS constant to age badly. A repaired spec is verified
   against the same page it was derived from before it is stored, exactly as
   at plan time, and the old spec is kept in `previous_extractor`.

   `DEGRADE_AFTER = 3` is a separate signal from the budget: on a long
   interval the money bounds repairs too slowly to be useful as an alarm.
   Degrading **deletes the schedules**, like a triggered watch — continuing to
   check something known to be broken bills every tick to re-learn a settled
   fact.

   Verified live on watch `w_71eab15f`. Its `scope` was corrupted by hand in
   DynamoDB; the next tick failed, Haiku recompiled the spec to
   `[data-uri*="quotes"]`, the new spec verified at $333.43, and the tick
   returned `ok` — total cost **$0.008**. Then the URL was pointed at a page
   with no price at all: three ticks failed, the watch went `degraded`, the
   email was sent and the schedule was deleted.

   **Known sources (2026-07-31).** Market quotes stopped being re-reasoned:
   the same "watch the Apple price" request had produced four different sites
   in four runs (CNN reading a once-a-day "Last closed at" for a per-minute
   watch, two sites that block datacenter IPs). `shared/sources.py` is a
   registry — the model only resolves "Apple" → AAPL, and the URL (CNBC's
   keyless quote JSON; Yahoo 429s and stooq 404s from Lambda) plus a canned
   jsonpath come from the file. Still verified live before the plan is
   offered; two consecutive runs produce identical targets. Deliberately
   narrow: product prices and anything with a "where" keep the searching
   path. Full review of every architectural decision to date:
   `docs/architecture-review-2026-07-31.md`.

5. Production hygiene — **done, see `docs/phase-5-plan.md`.** Three alarms
   are live and in state: `daily-spend`, `checker-errors`, `notifier-errors`,
   all firing into the `schedule-ai-app-alarms` SNS topic. `enable_alarms`
   now defaults to `true`; the gate stays in the code because it is what
   lets the whole stack apply from an account that has not been granted the
   alarm permissions.

   The IAM block is cleared. `schedule-ai-terraform` was granted scoped
   `cloudwatch:*Alarm*` and `sns:*` on `schedule-ai-app-*` on 2026-07-31 —
   by hand in the console, because the user holds `iam:*Role` but
   deliberately not `iam:*Policy` and so cannot widen its own permissions.
   The pre-existing SNS topic was `terraform import`ed rather than
   recreated. **The email subscription is confirmed** — check this rather
   than assuming, since Terraform reports success on a subscription that is
   still `PendingConfirmation` and would deliver nothing.

   The Checker publishes `EstimatedCostUSD` **twice** — once with
   dimensions for reading, once bare for alarming. This resolves the gotcha
   recorded in 8a: a dimensioned metric cannot be alarmed on without naming
   its exact dimension set, so an alarm on `{browser, false}` would have
   ignored every HTTP check and every Tier 1 repair.

   Also closed in this phase, all previously in the gap list: the Notifier's
   unpaginated GSI query, its stale `schedule_arn` values, platform-specific
   wheels in the Planner and Checker zips, and unbounded EventBridge retries
   (capped at 8/hour, with the `notifier-errors` alarm as the backstop —
   a cap is not a DLQ, and the difference is that the event is now dropped).

   The Fetcher's memory question was **diagnosed and closed on 2026-08-02**:
   no leak, and the metric that suggested one cannot decrease by construction.
   What Phase 5 did **not** finish is listed under "Still open" in the plan
   doc — chiefly that a value which is stale relative to its check interval is
   still invisible to the system.
7. **Split, and the cheap half is done.** The phase was one item — "CI/CD
   via OIDC" — and it turned out to be two with very different ratios.

   **Tests on push: done** (`.github/workflows/tests.yml`). No credentials,
   no AWS, cannot deploy. It catches the one thing worth catching for a solo
   project: a change pushed without anyone having run the suite. Needs
   `pytest` + `beautifulsoup4` and nothing else, listed in
   `requirements-dev.txt` — `anthropic`, `boto3` and `playwright` are stubbed
   by the tests rather than installed, deliberately, so that no future test
   can quietly reach a real client and start costing money.

   **Deploy on push: still last, on purpose.** The value of automating a
   deploy scales with frequency × blast radius × team size, and all three are
   small here. More to the point, the single most dangerous deploy path is
   one a pipeline does *not* fix: Terraform compares the Fetcher's image
   *URI*, which does not change on a `:latest` push, so a pipeline would
   report success while the running code stayed stale — faithfully, every
   time. Fix that with a versioned tag before automating anything.

## Phase 4 — 4a and 4b done, 4c next

The plan is written up in full in **`docs/phase-4-plan.md`**. **4a (the
lifecycle API) and 4b (hosting + a minimal React app) are both complete
and verified.** 4c is the designed chat interface.

The app is live at **https://dy98z46k9nqcs.cloudfront.net** — enter the
passcode from SSM on first load; it is kept in `localStorage`. Deploy a
new build with `./frontend/deploy.sh`, which builds, syncs and invalidates.
Terraform owns the bucket and distribution but *not* their contents, the
same split as the Fetcher's container image.

4b details worth not re-learning:

- **The SPA fallback must rewrite 403, not just 404.** A private S3 bucket
  returns 403 for a missing object, because it will not admit what does not
  exist. Handle only 404 and every client-side route breaks.
- **`deploy.sh` invalidates only `/index.html`.** Vite content-hashes asset
  filenames, so a new build produces names CloudFront has never cached.
  Invalidating `/*` is the common reflex and is billed per path past 1000
  a month.
- **CORS is now named origins, not `*`** — the CloudFront domain plus
  `http://localhost:5173` for `npm run dev`.
- Origin Access Control, not the older Origin Access Identity that most
  tutorials still show.
- `frontend/.env.production` is committed on purpose: `VITE_API_BASE` is
  not a secret. The passcode never enters the bundle — verified.

The watch status machine, as built:

```
planning ──→ proposed ──→ active ──→ triggered
    │                     │  ⇅
    └──→ failed           │  paused
                          └──→ degraded
```

`planning` means the Planner is running; `proposed` means a plan is ready
and awaiting confirmation; `failed` carries `plan_error`. Only `active` is
ever checked. `degraded` was added by 8d: the extractor broke, repair did
not fix it, and after `DEGRADE_AFTER` failures the schedules are deleted —
so like `triggered`, it is a terminal state that costs nothing. The
frontend renders it in the error colour with an explanation, since it is
the one status the user did not ask for and cannot act on except by
recreating the watch.

Things learned in 4a that are easy to trip over again:

- **A rejected passcode returns 403, not 401.** API Gateway uses 401 only
  when the identity source is absent entirely. The frontend must treat
  both as "bad passcode".
- **CORS preflight does not invoke the authorizer.** That is what makes a
  browser able to call an authorized route, and it comes from the
  `cors_configuration` block rather than an `OPTIONS` route.
- **The Planner's interval genuinely varies run to run** — 10, then 20,
  then 30 for near-identical requests. This is exactly why confirm exists.
- Do not poll the API in a tight `curl` loop expecting it to pace itself;
  round trips are ~0.2s, so 45 iterations elapse in ~9s against a ~20s
  Planner.

**All five open decisions were settled on 2026-07-30:**

1. **Plan-then-confirm.** The Planner writes rows and stops; a separate
   `POST /watches/{id}/confirm` creates the schedules. This is the one
   that changes `planner/handler.py` — see "The Planner split" in the
   plan doc. It also means the Checker must stop treating `planning` as
   checkable.
2. **Chat + watch list hybrid** — chat creates, a list manages.
3. **One `api` Lambda** with internal routing.
4. **Multi-turn chat deferred** to sub-phase 4d.
5. **CloudFront URL**, no custom domain.

Two things to know before starting:

- **Phase 4 does not start with React.** It starts with the watch
  lifecycle API (list / get / pause / delete), because none of it exists
  and a UI cannot be built without it. See the "Blocking for Phase 4"
  entries under Known gaps.
- **The Planner takes ~19.5s against a 29s API Gateway ceiling.** The API
  has to be asynchronous. The `planning` status already in the schema is
  the intended mechanism.

## How Phase 6 actually went

Two bugs were latent in the committed code and only surfaced on the first
real build. Both are fixed; both are the kind that look like the image is
broken when the problem is one line of packaging.

- **Buildx emits an OCI *index*, not an image.** Modern Buildx attaches an
  attestation manifest by default, which makes the push a multi-platform
  manifest list. Lambda accepts only a single-platform manifest and
  rejects an index at function-create time with a message that blames the
  image rather than the manifest type. Fixed with `--provenance=false`
  in `fetcher/build.sh`.
- **The Playwright base image has no `playwright` package.**
  `mcr.microsoft.com/playwright/python` ships the browsers (at
  `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`) and every system library,
  but not the Python bindings — `import playwright` fails out of the box,
  giving `Runtime.ImportModuleError`. Fixed by installing
  `playwright==1.61.0` in the Dockerfile, pinned to the image tag so the
  bindings can never drift from the browser build. No `playwright
  install` step is needed; the browsers are already on disk.

Two predictions from the Phase 6 design notes were confirmed:

- **The currency gap closed itself.** The Codespace probe saw `779,00€`;
  the Lambda in `us-east-1` sees `$629.00`. Geography was the whole
  explanation, so the Phase 2 "prices are geo-dependent" gap is resolved
  for the default case. Nothing still pins locale explicitly, so a watch
  that *needs* a non-US price has no way to ask for one.
- **Cold start was never the problem.** A ~2GB image inits in 1.5s,
  because Lambda chunks and caches image blocks rather than pulling the
  whole thing. The Checker's 60s timeout was never in danger and did not
  need raising.

One verification gotcha, since it will happen again: `aws ecr
describe-repositories` with no arguments asks for `repository/*` and will
return `AccessDenied` even when the policy is correct, because the policy
is deliberately scoped to `repository/schedule-ai-app-*`. Verify with
`aws ecr describe-repositories --repository-names schedule-ai-app-fetcher`
instead — `RepositoryNotFoundException` is the success case.
