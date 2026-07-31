# CLAUDE.md

Context for any Claude Code session working in this repo — read this first.

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
- **The Notifier leaves stale `schedule_arn` values behind.** It deletes
  the schedules but never clears the field, so a triggered watch's target
  rows keep pointing at schedules that no longer exist. Confirmed on the
  Phase 2/3 leftovers: every one carried an ARN for a deleted schedule.
  Harmless today because nothing reads the field back, but it makes the
  table lie about the state of the world.
- **The Notifier's GSI query is not paginated.** `query()` returns at
  most 1MB; a watch with enough targets would silently keep some of its
  schedules alive forever. Not reachable with 1–3 targets per watch,
  but it is a real correctness bug rather than a style note.
- **There are no tests.** Not one. Every verification so far has been a
  manual Lambda invoke against real AWS, which is slow, costs money, and
  cannot check the paths that matter most (a malformed Planner response,
  a Fetcher timeout, a condition that is exactly at the boundary).
  `judge()` and `_parse_json()` are pure functions and would be trivial
  to test offline.

### Product shape

- **Nothing keeps a history of what was checked.** `last_value` is
  overwritten on every tick, so there is no way to draw "the price over
  the last month," and no way to tell a genuinely stable price from a
  target that has silently been failing to extract for a week.
- **A watch cannot be edited.** No changing the threshold, the interval,
  or a bad target URL — the only recourse is delete and re-plan, which
  pays for a fresh Sonnet call and web search.
- **No partial-failure handling in the Planner.** If schedule creation
  fails halfway through a multi-target plan, earlier targets keep their
  schedules and the `Watches` row is never written.
- **No partial-failure handling in the Planner.** If schedule creation
  fails halfway through a multi-target plan, earlier targets keep their
  schedules and the `Watches` row is never written.

### Operations and cost

- **A permanently-failing Notifier retries for a day.** EventBridge
  rule targets default to ~185 retries over 24h. Nothing catches a
  notification that can never succeed. Phase 5's DLQ item.
- **Every tick pays for a Haiku call even when the page has not
  changed.** The dominant cost of the whole system is ~5k input tokens
  per check, and most checks on a slow-moving price see byte-identical
  text. Hashing the fetched text and skipping `judge()` on an unchanged
  hash is the single highest-leverage cost fix available — plausibly
  10–100×, far more than any model swap. Trimming `MAX_PAGE_CHARS` by
  pre-filtering to the region around the hint is the second.
- **Lambda zips are built for whatever machine ran `build.sh`.**
  `pip install -t` vendors platform-specific wheels — `checker/build/`
  contains `_pydantic_core.cpython-312-x86_64-linux-gnu.so`. It works
  only because the Codespace is Python 3.12 / x86_64 Linux, matching
  the Lambda runtime. Build on a Mac and the zip deploys fine, then
  dies at import. Fix in the hardening pass:
  `pip install ... --platform manylinux2014_x86_64 --only-binary=:all:`.
  Deliberately *not* done during Phase 6 — it changes `source_code_hash`
  on two working Lambdas and would muddy the diagnosis if the browser
  work went wrong.
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
- **The Fetcher is over-provisioned and unmeasured.** 2048MB allocated,
  915MB actually used. Do not just lower it — Lambda scales CPU with
  memory, and cost is memory × duration, so less memory can render
  slower and cost the same or more while creeping toward the 60s
  timeout. Needs measurement at 1024 / 1536 / 2048, not a guess.

## Current status

**Phases 0–3 and 6 complete and verified against the real AWS account.**
The product loop is closed and now works on JavaScript-rendered pages.

Proven end-to-end three times. Phase 2 on watch `w_ea349f2f`: Planner
picked a 5-minute interval and two targets, the Checker extracted "314
points", and CloudWatch confirmed Scheduler invoking the Checker
unprompted every 5 minutes. Phase 3 on watch `w_68c179cb`: the Checker
read "404 points", emitted `WatchTriggered`, and ~1s later the Notifier
had sent the email and deleted both schedules. Phase 6 on watch
`w_cd9975d8`: the Planner marked a Steam store target
`fetch_method: "browser"` and picked *only* that target (no Amazon, no
Best Buy — the prompt steering works), the Checker invoked the Fetcher,
Chromium rendered the page, and Haiku read `$629.00` against a `< $450`
condition. All three test watches have since been deleted; both tables
and the schedule list are empty.

Live AWS resources: `schedule-ai-app-watches` /
`schedule-ai-app-watch-targets` (DynamoDB); `schedule-ai-app-planner`,
`-checker`, `-notifier`, `-fetcher`, `-api`, `-authorizer` (Lambda, the
Fetcher a container image); `schedule-ai-app-fetcher` (ECR, with an
untagged-image expiry rule); `schedule-ai-app-bus` +
`schedule-ai-app-watch-triggered` rule (EventBridge); a `schedule-ai-app`
HTTP API with a `$default` stage; a verified SES identity; and seven IAM
roles (one per Lambda, plus `schedule-ai-app-scheduler-invoke-checker`
that schedules assume).

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
| 🔨 | **8d** · Tiered self-heal | **next** — start here |
| ⬜ | **5** · Production hygiene | after 8, so it instruments a settled design |
| ⬜ | **4c** · Designed chat interface | the side quest, deliberately late |
| ⬜ | **7** · CI/CD via GitHub OIDC | lowest ratio, last |

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
     in a fresh browser. **Whether the growth is a real leak or just three
     pages of different sizes is not established** — the retry is a mitigation,
     not a diagnosis, and this belongs in Phase 5.

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

5. Production hygiene — CloudWatch alarms, retries/DLQ, structured
   logging, plus the items in "Known gaps" above. Deliberately after
   Phase 8: alarms watch specific code paths and Phase 8 replaces the
   hot one, so doing this first means doing it twice.
7. Stretch — GitHub Actions CI/CD via OIDC (no static keys). Last on
   purpose. Honest counter-argument: a pipeline would reduce the risk of
   Phase 8's large change. But offline tests already exist where they
   matter most, deploys are infrequent and manual, and there is one user.

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
    │                       ⇅
    └──→ failed           paused
```

`planning` means the Planner is running; `proposed` means a plan is ready
and awaiting confirmation; `failed` carries `plan_error`. Only `active` is
ever checked.

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
