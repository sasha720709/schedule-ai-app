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
- **IAM**: the `schedule-ai-terraform` user gets AWS-managed policies added
  one at a time, per phase, as each new AWS service is introduced —
  deliberately not broad access up front. Currently: `AmazonS3FullAccess`,
  `AmazonDynamoDBFullAccess`.
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

## Current status

**Phase 1, in progress.** `planner/plan.py` is a local prototype proving
the Planner idea (plain-English request -> structured JSON plan) using
Claude + the `web_search_20250305` tool, no AWS involved yet.

Phase 0 is complete and verified: repo, devcontainer (Python, Node,
Terraform, AWS CLI, GitHub CLI, Claude Code), GitHub Codespaces, IAM user
`schedule-ai-terraform`, and the Terraform state backend (S3 bucket +
DynamoDB lock table) all exist and were confirmed working against the real
AWS account.

## Roadmap

0. Environment & IaC foundation — **done**
1. Planner, offline — **current.** Prove the Planner logic works before
   touching infrastructure.
2. Serverless core — move Planner + Checker into Lambda, DynamoDB tables,
   EventBridge Scheduler wired end-to-end.
3. Notifications — Checker emits an event on a match, Notifier Lambda
   sends an SES email.
4. API + web chat UI — API Gateway + chat Lambda exposing the Planner as a
   tool, React frontend deployed to S3 + CloudFront.
5. Production hygiene — CloudWatch alarms, retries/DLQ, structured
   logging.
6. Stretch — GitHub Actions CI/CD via OIDC (no static keys), headless
   browser or dedicated API integrations for high-value watch targets.
