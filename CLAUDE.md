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

## Current status

**Phase 2 complete and verified against the real AWS account.** The whole
loop runs unattended: Planner Lambda (Claude Sonnet + web search) turns a
request into DynamoDB rows and creates one EventBridge Scheduler schedule
per target; each schedule fires the Checker Lambda (Haiku) on its own
interval; the Checker fetches, judges, writes back, and flips the watch to
`triggered` on a match.

Proven end-to-end on watch `w_ea349f2f` ("top Hacker News story over 50
points"): Planner picked a 5-minute interval and two targets, the Checker
extracted "314 points", the condition tripped, and CloudWatch confirmed
Scheduler independently invoking the Checker every 5 minutes afterwards.

Phases 0 and 1 are complete: repo, devcontainer, Codespaces, IAM user
`schedule-ai-terraform`, the Terraform state backend, and the offline
Planner prototype (`planner/plan.py`).

Live AWS resources: `schedule-ai-app-watches` and
`schedule-ai-app-watch-targets` (DynamoDB), `schedule-ai-app-planner` and
`schedule-ai-app-checker` (Lambda), plus their IAM roles and the
`schedule-ai-app-scheduler-invoke-checker` role that schedules assume.

## Roadmap

0. Environment & IaC foundation — **done**
1. Planner, offline — **done.** Prove the Planner logic works before
   touching infrastructure.
2. Serverless core — **done.** Planner + Checker in Lambda, DynamoDB
   tables, EventBridge Scheduler wired end-to-end.
3. Notifications — **current.** Checker emits an event on a match,
   Notifier Lambda sends an SES email. Also the natural home for
   deleting a triggered watch's schedules (see known gaps).
4. API + web chat UI — API Gateway + chat Lambda exposing the Planner as a
   tool, React frontend deployed to S3 + CloudFront.
5. Production hygiene — CloudWatch alarms, retries/DLQ, structured
   logging.
6. Stretch — GitHub Actions CI/CD via OIDC (no static keys), headless
   browser or dedicated API integrations for high-value watch targets.
