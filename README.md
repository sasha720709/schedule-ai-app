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
.devcontainer/       Codespaces dev environment
terraform/
  bootstrap/          one-time: creates the S3 + DynamoDB Terraform backend itself
  (more to come as the app is built out)
```

## Status

Phase 0: environment + Terraform backend bootstrap. No application code yet.
