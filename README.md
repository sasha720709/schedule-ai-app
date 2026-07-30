# schedule-ai-app

An agentic worker that watches the web for a condition you name — a price, a
restock, a status change — and notifies you the moment it becomes true.
Built as a hands-on project for AWS serverless, agentic AI design, and
infrastructure as code.

## How it works

Two tiers, doing very different jobs:

- **Planner** (Claude, with web search) runs once per watch. It turns a
  plain-English request into concrete target URLs, an extraction strategy,
  and a check interval, then writes that plan to storage and schedules the
  recurring checks.
- **Checker** (Claude Haiku, no search) runs on every scheduled tick, one
  per target. It reads a fetched page and answers exactly one question: has
  the condition become true. Cheap and frequent, by design.

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
planner/              Planner: plan.py (Claude + web search), Lambda handler, build.sh
checker/              Checker: check.py (fetch + Haiku), Lambda handler, build.sh
notifier/             Notifier: SES email + schedule teardown, build.sh
fetcher/              Fetcher: Playwright/Chromium renderer, Dockerfile, build.sh
terraform/
  bootstrap/          one-time: creates the S3 + DynamoDB Terraform backend itself
  app/                tables, Lambdas, IAM, ECR, EventBridge bus/rule, SES (remote state)
```

Three of the four Lambdas are packaged by their own `build.sh`, which zips
the handler plus its pip dependencies into `dist/`. Terraform picks that zip
up directly. `boto3` is deliberately not bundled — the Lambda Python runtime
ships it.

## Tests

```bash
pip install -r api/requirements-dev.txt
pytest api/ -q
```

The `api` Lambda has an offline suite that stubs `boto3` and touches no
AWS — it runs in well under a second and costs nothing. It covers the
paths a manual Lambda invoke is worst at reaching: malformed bodies,
status conflicts, a confirm retried after a partial failure, intervals
just outside the allowed range, and an unexpected exception that must not
leak a stack trace. It found a real cost-safety bug on first run.

The other Lambdas have no tests yet; that is a Phase 5 item.

## Packaging

The Fetcher is different: Chromium and its system libraries are far past
Lambda's 250MB unzipped limit, so it ships as a container image. Its
`build.sh` builds and pushes to ECR, and Terraform stores only the image
URI. That means **pushing a new image to the same `:latest` tag will not
redeploy it** — `terraform apply` sees an unchanged URI string. Use
`aws lambda update-function-code --image-uri ...` after a rebuild.

## Status

Phases 0–3 and 6 are complete and running in AWS. The loop is closed end
to end: a plain-English request becomes a watch that checks itself on a
schedule, emails you when the condition comes true, and then turns its
own schedules off — and it now works on pages that render their value in
JavaScript, via a headless-Chromium Lambda the Planner opts into per
target. Phase 4 (API Gateway + a React chat UI) is next — until then,
watches are created by invoking the Planner Lambda directly.

See `CLAUDE.md` for the decision log, roadmap, and the known gaps found
while building.
