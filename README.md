# schedule-ai-app

An agentic worker that watches the web for a condition you name — a price, a
restock, a status change — and notifies you the moment it becomes true.
Built as a hands-on project for AWS serverless, agentic AI design, and
infrastructure as code.

## How it works

Two tiers, doing very different jobs:

- **Planner** (Claude Sonnet, with web search) runs once per watch. It turns
  a plain-English request into concrete target URLs, an extraction strategy
  and a check interval, then stops — a separate confirm step creates the
  schedules, so you see what a watch will cost before it starts billing.
- **Checker** runs on every scheduled tick, one per target, and answers
  exactly one question: has the condition become true. Cheap and frequent,
  by design.

Phase 8 is currently replacing the expensive half of that. Today the Checker
sends the page to Claude Haiku on every tick, which costs ~$0.0057 a check —
about $82/month for one target checked every three minutes. The Planner is
being changed to compile a **deterministic extraction spec** instead of
describing the task in English, dropping a check to ~$0.000004. See
`docs/phase-8-cheap-checks.md`.

```
User -> Web Chat UI -> API Gateway -> Planner Lambda -> DynamoDB
                                                       -> EventBridge Scheduler
EventBridge Scheduler -> Checker Lambda -> DynamoDB
                                         -> (on match) EventBridge event -> Notifier Lambda -> SES
```

## Stack

- **Frontend**: React + TypeScript, hosted on S3 + CloudFront
- **Backend**: Python Lambdas (Planner, Checker, Notifier, chat handler)
- **Data**: DynamoDB (`Watches`, `WatchTargets`)
- **Scheduling**: EventBridge Scheduler (one dynamic schedule per watch target)
- **Notifications**: SES
- **Infra**: Terraform, remote state in S3 with DynamoDB locking
- **Dev environment**: GitHub Codespaces (see `.devcontainer/`)

## Repo layout

```
.devcontainer/        Codespaces dev environment
planner/              Planner: plan.py (Claude + web search), Lambda handler
checker/              Checker: check.py (fetch + judge), Lambda handler
notifier/             Notifier: SES email + schedule teardown
fetcher/              Fetcher: Playwright/Chromium renderer, Dockerfile
api/                  Watch lifecycle API — every route behind one Lambda
authorizer/           Passcode authorizer for API Gateway
shared/               cost.py and extract.py, vendored into several zips
frontend/             React + TypeScript app, deployed by deploy.sh
docs/                 Phase plans
terraform/
  bootstrap/          one-time: creates the S3 + DynamoDB Terraform backend itself
  app/                tables, Lambdas, IAM, ECR, API Gateway, CloudFront, SES
```

Five of the six Lambdas are packaged by their own `build.sh`, which zips the
handler plus its pip dependencies into `dist/`. Terraform picks that zip up
directly. `boto3` is deliberately not bundled — the Lambda Python runtime
ships it. Modules in `shared/` are copied into each zip that needs them; a
Lambda Layer would deduplicate that and is a deferred gap.

## Running the frontend

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, allowed by the API's CORS config
./deploy.sh          # build, sync to S3, invalidate CloudFront
```

The app asks for a passcode on first load and keeps it in `localStorage`.
Read the current one with:

```bash
aws ssm get-parameter --name /schedule-ai-app/passcode \
  --with-decryption --query Parameter.Value --output text
```

`terraform output frontend_url` gives the deployed URL.

## Tests

```bash
pip install -r api/requirements-dev.txt
pytest api/ shared/ -q
```

145 tests, all offline: `boto3` is stubbed, nothing touches AWS, and the
whole suite runs in about a fifth of a second for nothing.

They cover the paths a manual Lambda invoke reaches worst — malformed
bodies, status conflicts, a confirm retried after a partial failure, budget
refusals, and the difference between "no value right now" and "this
extractor is broken". They have earned their keep twice: they caught a
cost-safety bug where an explicit `null` interval silently fell back to the
Planner's own, and a parser that read `512` out of "Steam Deck 512 GB OLED"
and would have reported it as a price.

The Planner, Checker, Notifier and Fetcher have no tests yet; that is a
Phase 5 item.

## Packaging

The Fetcher is different: Chromium and its system libraries are far past
Lambda's 250MB unzipped limit, so it ships as a container image. Its
`build.sh` builds and pushes to ECR, and Terraform stores only the image
URI. That means **pushing a new image to the same `:latest` tag will not
redeploy it** — `terraform apply` sees an unchanged URI string. Use
`aws lambda update-function-code --image-uri ...` after a rebuild.

## Status

Phases 0–3, 6, 4a, 4b and 8a are complete and running in AWS. The loop is
closed end to end: a plain-English request becomes a watch that checks
itself on a schedule, emails you when the condition comes true, and then
turns its own schedules off. It works on pages that render their value in
JavaScript, via a headless-Chromium Lambda the Planner opts into per target.
There is a live HTTP API behind a passcode, a deployed React app, and a
budget guardrail that refuses to schedule a watch costing more than
`MONTHLY_BUDGET_USD` a month.

**Phase 8 is in progress** — making a check nearly free. The extraction
engine exists and is tested; wiring the Planner and Checker to it is next.

See `CLAUDE.md` for the decision log, roadmap, and the known gaps found
while building.
