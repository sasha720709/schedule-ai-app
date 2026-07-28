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
terraform/
  bootstrap/          one-time: creates the S3 + DynamoDB Terraform backend itself
  app/                DynamoDB tables, Lambdas, IAM roles (remote state)
```

Each Lambda is packaged by its own `build.sh`, which zips the handler plus
its pip dependencies into `dist/`. Terraform picks that zip up directly.
`boto3` is deliberately not bundled — the Lambda Python runtime ships it.

## Status

Phases 0–2 are complete and running in AWS: the Planner and Checker are
deployed Lambdas, and EventBridge Scheduler drives the check loop without
anything else running. Phase 3 (notifications via SES) is next — until
then, a met condition only flips the watch's status in DynamoDB.

See `CLAUDE.md` for the decision log, roadmap, and the known gaps found
while building Phase 2.
