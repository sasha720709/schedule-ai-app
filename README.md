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
terraform/
  bootstrap/          one-time: creates the S3 + DynamoDB Terraform backend itself
  app/                tables, Lambdas, IAM, EventBridge bus/rule, SES (remote state)
```

Each Lambda is packaged by its own `build.sh`, which zips the handler plus
its pip dependencies into `dist/`. Terraform picks that zip up directly.
`boto3` is deliberately not bundled — the Lambda Python runtime ships it.

## Status

Phases 0–3 are complete and running in AWS. The loop is closed end to
end: a plain-English request becomes a watch that checks itself on a
schedule, emails you when the condition comes true, and then turns its
own schedules off. Phase 4 (API Gateway + a React chat UI) is next —
until then, watches are created by invoking the Planner Lambda directly.

See `CLAUDE.md` for the decision log, roadmap, and the known gaps found
while building.
