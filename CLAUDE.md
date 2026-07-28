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

## Known gaps, found by running Phase 2 (deferred on purpose)

Deliberately not fixed yet — the plan is to get the full product shape
working first, then do a hardening pass.

- **Planner JSON parsing is fragile.** `plan.py` assumes the last text
  block is clean JSON. Observed failing for real once in Lambda
  (`JSONDecodeError` on an empty last block); the same request succeeded
  on retry. Fix later with forced structured output (a tool call) or a
  retry-on-parse-failure loop. `checker/check.py` already uses a slightly
  sturdier outermost-`{...}` parse.
- **Plain HTTP GET does not work on real retail sites.** Confirmed
  against all three Steam Deck targets: Steam renders price in JS,
  Best Buy times out (bot protection), Amazon returns a page with no
  visible price. The Checker handles this correctly (records the miss,
  never invents a value) but cannot actually watch these sites. This is
  the Phase 6 headless-browser/scraping-API item, now confirmed as
  necessary rather than hypothetical.
- **Prices are geo-dependent.** steamdeck.com served EUR from the
  Codespace; a Lambda in `us-east-1` may see USD. Extraction hints and
  conditions carry a currency, but nothing pins the fetch's locale.
- **Triggered watches keep their schedules.** The Checker flips a watch
  to `triggered` and then early-returns on every later tick (cheap — no
  Claude call), but the EventBridge schedule keeps firing forever.
  Nothing deletes schedules yet. Do this when the Notifier lands in
  Phase 3, since that's the natural place to decide a watch is finished.
- **No partial-failure handling in the Planner.** If schedule creation
  fails halfway through a multi-target plan, earlier targets keep their
  schedules and the `Watches` row is never written.
- **The Planner doesn't know what the Checker can do.** Seen in Phase 3:
  for a Hacker News watch the Planner picked
  `hacker-news.firebaseio.com/v0/topstories.json` with a hint saying
  "take the first ID, *then* fetch `/item/{id}.json` to read its score."
  The Checker does exactly one GET and cannot chain requests, so it
  correctly reported it couldn't extract a value — but that target is
  dead weight, and every tick still pays for a fetch and a Haiku call.
  Cheap fix when hardening: state the Checker's actual capability (one
  plain GET, no chaining, no JS) in the Planner's system prompt.
- **A permanently-failing Notifier retries for a day.** EventBridge
  rule targets default to ~185 retries over 24h. Nothing catches a
  notification that can never succeed. Phase 5's DLQ item.

## Current status

**Phases 0–3 complete and verified against the real AWS account.** The
product loop is closed: a plain-English request becomes a watch that
checks itself on a schedule and emails when it comes true, then shuts
its own schedules off.

Proven end-to-end twice. Phase 2 on watch `w_ea349f2f`: Planner picked a
5-minute interval and two targets, the Checker extracted "314 points",
and CloudWatch confirmed Scheduler invoking the Checker unprompted every
5 minutes. Phase 3 on watch `w_68c179cb`: the Checker read "404 points",
emitted `WatchTriggered`, and ~1s later the Notifier had sent the email
and deleted both schedules.

Live AWS resources: `schedule-ai-app-watches` /
`schedule-ai-app-watch-targets` (DynamoDB); `schedule-ai-app-planner`,
`-checker`, `-notifier` (Lambda); `schedule-ai-app-bus` +
`schedule-ai-app-watch-triggered` rule (EventBridge); a verified SES
identity; and four IAM roles (one per Lambda, plus
`schedule-ai-app-scheduler-invoke-checker` that schedules assume).

**IAM note:** the `schedule-ai-terraform` user's inline policies hit AWS's
2048-character *aggregate* limit during Phase 3. They were consolidated
into one customer-managed policy, `schedule-ai-app-terraform`, and the
inline ones deleted. Add future permissions there. It uses action
wildcards (`lambda:*`, `iam:*Role`) to fit, but every statement is still
scoped to `schedule-ai-app-*` resources.

## Roadmap

0. Environment & IaC foundation — **done**
1. Planner, offline — **done.** Prove the Planner logic works before
   touching infrastructure.
2. Serverless core — **done.** Planner + Checker in Lambda, DynamoDB
   tables, EventBridge Scheduler wired end-to-end.
3. Notifications — **done.** Checker emits `WatchTriggered` onto a custom
   bus, a rule routes it to the Notifier, which emails via SES and then
   deletes that watch's schedules.
4. API + web chat UI — API Gateway + chat Lambda exposing the Planner as a
   tool, React frontend deployed to S3 + CloudFront. **Deferred** below
   Phase 6 by choice: the owner wants the app to actually work on real
   sites before it gets a nice interface.
5. Production hygiene — CloudWatch alarms, retries/DLQ, structured
   logging.
6. Headless browser — **current, mid-flight.** See "Picking up Phase 6"
   below.
7. Stretch — GitHub Actions CI/CD via OIDC (no static keys).

## Picking up Phase 6 (in progress)

All code is written and committed; what remains is Docker-dependent.

Done already:
- `fetcher/` — `handler.py` (Playwright renders one URL, returns text),
  `Dockerfile` (Playwright base image + `awslambdaric`), `build.sh`
  (docker build → ECR push).
- `terraform/app/ecr.tf` — repo + a lifecycle rule expiring untagged
  images, since a ~2GB image billed per GB adds up.
- `checker/` — `check.py` split into `fetch_text` and `judge` so text can
  come from either source; `handler.py` dispatches on `fetch_method` and
  invokes the Fetcher for `"browser"`.
- `planner/plan.py` — prompt now emits `fetch_method` and states the
  Checker's real limits (one GET, no chaining, no bot-protected sites),
  which also fixes the Phase 3 multi-step-hint gap.
- `terraform/app` — Fetcher Lambda (`package_type = "Image"`, 2048MB),
  its role, and the Checker's permission to invoke it.
- `.devcontainer/devcontainer.json` — added the docker-in-docker feature.

Remaining, in order:
1. Add ECR permissions to the `schedule-ai-app-terraform` managed policy:
   `ecr:*` on `arn:aws:ecr:us-east-1:851725214678:repository/schedule-ai-app-*`,
   plus `ecr:GetAuthorizationToken` on `*` (docker login needs it before
   any repo is known).
2. Rebuild the Codespace so Docker exists.
3. `terraform apply -target=aws_ecr_repository.fetcher -target=aws_ecr_lifecycle_policy.fetcher`
   — the registry must exist before an image can be pushed, and the image
   before the Lambda can be created.
4. `fetcher/build.sh` (pulls ~2GB of base image the first time).
5. Full `terraform apply` to create the Fetcher Lambda and rewire the
   Checker.
6. Test: create a watch on a Steam page, confirm the Planner marks it
   `fetch_method: "browser"` and that the Checker gets a real price.
   Watch the Fetcher's first cold start — if it exceeds the Checker's
   60s timeout, raise the Checker's timeout rather than the Fetcher's.
