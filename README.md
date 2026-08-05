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

Phase 8 replaced the expensive half of that, and it is the change the whole
project is built around. The Checker used to send the page to Claude Haiku on
**every tick** — ~$0.0057 a check, about $82/month for one target checked
every three minutes. Now the Planner compiles a **deterministic extraction
spec** once, and the Checker executes it with no model at all: $0.0000041 for
a plain fetch, $0.000186 through the headless browser. A model returns only
when an extractor actually breaks, once, to repair it. See
`docs/phase-8-cheap-checks.md`.

Phase 9 is adding **kinds of watch**, so that a market quote (one canonical
source, market-hours schedule), a vacancy (waiting for something to appear)
and a product price stop being special cases inside one prompt. See
`docs/phase-9-watch-kinds.md`.

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
gatekeeper/          Cognito pre-sign-up trigger: who may have an account
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
pip install -r requirements-dev.txt
python -m pytest -q
```

382 tests, all offline: `boto3`, `anthropic` and `playwright` are stubbed,
nothing touches AWS or an API key, and the whole suite runs in about two
seconds for nothing. They run on every push via GitHub Actions.

They cover the paths a manual Lambda invoke reaches worst — malformed
bodies, status conflicts, a confirm retried after a partial failure, budget
refusals, and the difference between "no value right now" and "this
extractor is broken". They have earned their keep twice: they caught a
cost-safety bug where an explicit `null` interval silently fell back to the
Planner's own, and a parser that read `512` out of "Steam Deck 512 GB OLED"
and would have reported it as a price.

Every Lambda now has tests. What is still missing is any test that runs the
**whole chain** — Planner → schedule → Checker → event → Notifier is still
verified only by invoking the real thing, and the first live end-to-end run
found two bugs that all 378 tests of the time could not see. A suite where
every test injects its collaborators cannot catch a wiring bug.

## Packaging

The Fetcher is different: Chromium and its system libraries are far past
Lambda's 250MB unzipped limit, so it ships as a container image. Its
`build.sh` builds and pushes to ECR, and Terraform stores only the image
URI. That means **pushing a new image to the same `:latest` tag will not
redeploy it** — `terraform apply` sees an unchanged URI string. Use
`aws lambda update-function-code --image-uri ...` after a rebuild.

## Status

Phases 0–3, 5, 6, 4a, 4b and 8 are complete and running in AWS. The loop is
closed end to end: a plain-English request becomes a watch that checks
itself on a schedule, emails you when the condition comes true, and then
turns its own schedules off. It works on pages that render their value in
JavaScript, via a headless-Chromium Lambda the Planner opts into per target
— though it now proves a plain GET cannot do the job first, because the
browser is 45x the cost. There is a live HTTP API behind a passcode, a
deployed React app, spend and error alarms, and a budget guardrail that
refuses to schedule a watch costing more than `MONTHLY_BUDGET_USD` a month.

A watch also repairs itself: when a site is redesigned under a compiled
extractor, one model call rewrites the spec, verifies it against the same
page, and carries on. Three failures in a row and the watch stops and says
so, because continuing to check something known to be broken bills every
tick to re-learn a settled fact.

**Phase 9 is in progress** — typed kinds of watch. Market quotes, vacancy
watches and product prices are being separated so that adding a new kind is
a module rather than another paragraph in a prompt that already carries
three request types.

See `CLAUDE.md` for the decision log, roadmap, and the known gaps found
while building.
