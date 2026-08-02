# Phase 9 — Watch kinds, schedules and delivery

Status: **steps 1, 2a and the window half of step 3 are built, deployed and
tested (2026-08-02).** §8 is decided. Remaining: 2b (a classify step, so
routing leaves `SEARCH_PROMPT`), `once` schedules, `reminder`, and the
`Channel` seam. See §10 for exactly where the line is.

The one-line version: a watch is currently one shape with a growing pile of
exceptions, and the exceptions have started colliding with each other. This
phase gives the variation somewhere to live.

## 1. Why — the evidence, not the aesthetics

`planner/plan.py` is 634 lines, but the length is a symptom. Look at what is
in it: four model prompts totalling ~215 lines, and the biggest of them,
`SEARCH_PROMPT`, now carries the rules for **three different request types at
once** — a paragraph telling the model not to search for market quotes, a
paragraph distinguishing "waiting for a number" from "waiting for a thing to
appear", and a paragraph forbidding invented thresholds for relative
conditions.

Every one of those paragraphs was added after a real request failed. And two
of the failures were **rules in one prompt interfering with each other**:

- A presence watch could not be planned at all, because the rule demanding a
  literal value on the page overrode the rule that made `count` reachable.
- A relative condition produced `price < 313.93` against a page reading
  `$333.43`, because the threshold was written during the search step, before
  any page had been opened.

That is the cost curve of this design. Each new kind adds a paragraph, and
each paragraph slightly degrades the ones already there. The argument for
splitting is not that one file is long; it is that the failure rate grows with
the number of kinds and the failures are hard to attribute.

## 2. Three axes, currently tangled into one

The variation between requests is not one-dimensional. Three independent
things vary, and today they are all decided in the same place.

**Axis A — what makes it fire.**

- *Condition-triggered*: the schedule is a polling mechanism, the condition is
  the event. Everything built so far.
- *Time-triggered*: the schedule **is** the event. "Remind me at 9am." There
  is nothing to fetch, nothing to extract, nothing to compare.

This axis does not exist in the code at all, which is why a reminder cannot be
expressed today. It is also the axis that proves the abstraction: forcing a
reminder through the condition pipeline produces a watch with no target, no
extractor, and a condition that is always true — three null objects in a row,
which is the standard sign that the protocol describes the wrong thing.

**Axis B — where the target and extractor came from.** Only meaningful for
condition-triggered watches.

- *Searched*: web search → open the page → model reads it → model compiles an
  extractor → verify it before storing. Today's default.
- *Known*: a registry lookup. `shared/sources.py` already does this for market
  quotes; the model's only job is "Apple" → `AAPL`.

**Axis C — how the owner is told.** Today: hardcoded SES email.

- Email, calendar, chat, task tracker. Orthogonal to A and B: **any kind can
  use any channel**, and keeping that a product rather than a hierarchy is the
  whole point.

## 3. What a watch becomes

```
Watch
  kind          value | presence | quote | reminder
  trigger       condition | time          (derived from kind, not stored twice)
  schedule      interval | window | once
  channels      [email, calendar, ...]
  targets       0..n    (a reminder has none)
```

The important claim: **the machinery underneath barely changes.** Fetch →
extract → compare → notify stays exactly as it is for all three
condition-triggered kinds. `shared/extract.py`, `condition.py`, `cost.py` and
`repair.py` are pure, well-tested and untouched by this phase.
This is a split of one file plus two new seams, not a redesign.

## 4. The kinds

|                  | value | presence | quote | reminder |
|---|---|---|---|---|
| trigger          | condition | condition | condition | **time** |
| target from      | search | search | registry | — |
| extractor        | compiled + verified | compiled counter | canned | — |
| schedule         | interval | interval | **window** | **window or once** |
| self-heal (8d)   | yes | yes | no — the source is ours | n/a |
| cost/month @1min | $0.18 http / $8.05 browser | same | **$0.034** | ~$0 |

`value` and `presence` are close to the same kind and should stay one module
with a flag; they are listed separately because they already are.

`quote` differs from `value` in exactly two cells. That is the measure of how
little new machinery this needs.

## 5. Scheduling

Verified against the AWS CLI, not assumed. EventBridge Scheduler supports
`--schedule-expression-timezone`, `--start-date` / `--end-date`, and
`--action-after-completion`. All three matter here.

**Interval** — `rate(N minutes)`. What exists.

**Window** — `cron(...)` plus a timezone. Built: market hours are
`cron(*/N 9-16 ? * MON-FRI *)` in `America/New_York`, and **no code in the
Checker learns what a stock market is.** The schedule simply does not fire.

`9-16` brackets the 09:30–16:00 session rather than matching it, because
Scheduler takes one expression per schedule and the exact session needs two
(`30-59 9` plus `* 10-15`) — which would mean two schedules per target and two
of everything that creates, pauses and deletes them. The margin costs ~90
minutes a day of reading an unchanged value, which the table below shows is
harmless. Missing the opening bell would not be, so the margin goes outwards.

An interval of 60 minutes or more is **refused** on a windowed schedule:
`cron(*/60 ...)` steps within the hour, so it would silently fire hourly on
the hour and never raise anything.

Market holidays are not handled and will not be: cron cannot express
"except Thanksgiving", there are about nine a year, and a check on a holiday
reads the previous close and costs a fraction of a cent. Explicitly accepted.

**Once** — `at(2026-08-03T09:00:00)` with `--action-after-completion DELETE`,
so a one-shot reminder removes its own schedule instead of leaking one.

### An honest correction to the cost argument

When this was first proposed, part of the case for market-hours windows was
"the same $5 budget then buys 5.3× more frequent checking." **That argument is
close to worthless and should not be repeated.** Measured:

| | checks/month | $/month |
|---|---|---|
| stock, 1 min, 24/7 (today) | 43,200 | $0.178 |
| stock, 1 min, market hours (9:30–16:00) | 8,190 | **$0.034** |
| stock, 1 min, as built (9:00–17:00) | 10,080 | $0.042 |

The budget was never binding. $5 buys 1,214,574 HTTP checks a month, and a
month contains only 8,190 market minutes — the budget would permit checking
every 0.4 seconds. The saving is fourteen cents.

### And a second correction: the correctness argument was wrong too

The replacement argument was: outside trading hours CNBC's `last` holds the
previous close, so a relative watch could fire at market close against a
frozen number. **Checked before building, and it does not hold.**

A frozen quote is not a wrong quote; it is the last real price. Walk it
through with a baseline of $333.43 taken at 11:00:

| | `last` | fires? | right? |
|---|---|---|---|
| 11:00, trading | 331.00 | yes | yes — it did go down |
| closes at 335 | 335.00 | no | yes |
| 16:30, market shut | 335.00 | no | yes |
| all weekend | 335.00 | no | yes |

There are no false fires. The out-of-hours value is simply the last one that
was true.

So what the window actually buys, in order of how much it matters:

1. **It stops hammering a free third-party endpoint we do not own.** A
   one-minute quote watch makes 43,200 requests a month, 33,120 of which
   cannot return anything new. CNBC's keyless API is the one that answered
   when Yahoo returned 429 and stooq returned 404 from Lambda; losing it to
   rate limiting breaks every quote watch at once. **This is the real reason.**
2. The plan card stops overstating a windowed watch by ~4x.
3. It is the mechanism reminders need anyway (`cron`, `at`), so quotes are the
   cheap first user of a seam that has to exist regardless.

### The correctness bug that *is* real, and is not this one

A watch created **outside** trading hours takes its baseline from the previous
close. "Tell me when Apple goes down from the current," asked on a Sunday, is
measured against Friday — so if Monday opens higher, the watch is comparing
against a number the user never saw and never agreed to.

Windowing does not fix this. The fix is to take the baseline during a session,
or to say plainly on the plan card which close it came from. **Open.**

Where money *is* binding is browser targets: $8.05/month at one minute, above
the $5 budget. Windows would matter there — but stock quotes are HTTP, so the
two do not currently overlap.

## 6. Delivery channels

Requested in discussion: "add a reminder to my calendar app instead of just
sending mail" — Apple Calendar, Google Calendar, Monday.com.

This is a genuine second abstraction and a cleaner one than kinds: the
variation is obvious and bounded (*given a message, deliver it*), there is no
classification problem because the owner picks the channel, and the blast
radius is one Lambda plus one field on the watch row.

The trap is doing it in the wrong order, because the three named channels have
wildly different costs.

**Tier 1 — an `.ics` attachment on the existing email. Do this first.**
An `.ics` file is the calendar interchange format every calendar on earth
reads. Attach one and Apple Calendar, Google Calendar and Outlook all add the
event in one tap. No OAuth, no tokens, no per-vendor code, no new failure
mode. It is a formatting function and a test. **This delivers "put it in my
calendar" for every calendar at once**, and it is worth being clear that it is
not a lesser version of the real thing — for a one-way reminder it *is* the
real thing.

Worth noticing: for a *reminder* delivered this way, the app does not need to
wake up at 9am at all. It writes one calendar event and deletes its schedule;
the calendar does the reminding. The time-triggered kind and the calendar
channel collapse into each other. A condition-triggered watch still polls —
only the delivery differs.

**Tier 2 — webhook channels.** Telegram (one bot token, one POST), Slack
(one incoming-webhook URL), Monday.com (an API token). Each is an adapter of
a few dozen lines with no auth dance. Cheap, and this is where the `Channel`
seam earns itself.

**Tier 3 — real OAuth, and it drags identity with it.** Google Calendar's API
needs OAuth 2.0: a consent screen, a refresh token *per user*, token storage
and refresh, and Google's verification review to go past test users. That is
not a bigger adapter; it is a different security model. This app is currently
one shared passcode with `user_id` hardcoded to `"default"` — a known gap —
and a per-user refresh token is exactly the thing that forces that gap to be
closed. Apple Calendar is worse: there is no public cloud API, only CalDAV
with app-specific passwords.

**Recommendation:** build the `Channel` seam now with two implementations,
email and `.ics`-by-email, so the shape is proven by more than one case. Defer
tiers 2 and 3 until a channel is actually wanted, and treat Google Calendar as
a project that starts with per-user auth, not as a channel.

**Do not fold channels into kinds.** They are orthogonal — any kind × any
channel — and collapsing two axes into one hierarchy is the mistake this
phase exists to undo.

## 7. Classification, the boring way

Agreed in discussion: go the classic route and do not let the model guess.

The risk with a "classify the request" step is that it becomes a new
single point of failure — the same wrong fork as today, moved one layer up.
The mitigations:

- ~~**Rules first, model second.**~~ **Revised on contact with the problem.**
  There is no honest lexical rule separating "how much is Apple" (a quote)
  from "how much is an Apple pencil at Best Buy" (a product): both name a
  company, and the distinguishing signal is meaning. A keyword heuristic
  would fight the model and lose. So the rules became **gates around the
  answer** rather than a substitute for it — the kind must be in the
  registry, and a `quote` must carry a symbol the registry will accept, or
  the whole thing degrades to `value`. A hallucinated ticker becomes a web
  search instead of a URL.
- **The default is the existing path.** Anything unmatched goes to `value` /
  the searching Planner, which is what happens today. An unknown request must
  degrade to current behaviour, never to a refusal.
- **Classification is verifiable.** The chosen kind is stored on the row and
  shown on the plan card, so a wrong fork is visible before confirmation
  rather than discovered a week later.
- **No new prompt paragraph.** Each kind's rules move *into its own module's*
  prompt. `SEARCH_PROMPT` shrinks; it stops being the dumping ground, which
  is the actual point of §1.

## 8. Who owns the schedule — decided: the watch

Today a schedule is created per *target*: named `schedule-ai-app-{target_id}`,
and the Checker is invoked with a `target_id`. A reminder has no target.

The two options were:

- **(a) Schedules move to the watch for time-triggered kinds.** Honest, and
  right long-term. Costs edits in the Checker's entry point, the Notifier's
  teardown, `api`'s confirm/patch/delete, and the tests for all of them.
- **(b) A reminder gets one synthetic target row.** Nothing else changes.
  Cheap — and it makes the table describe a target that does not exist, which
  is precisely the class of lie removed from the Notifier in Phase 5.

**Agreed 2026-08-02: (a).** A reminder stores `targets: []` and its schedule
invokes with `{"watch_id": ...}`; condition-triggered watches keep
`{"target_id": ...}` and are otherwise untouched. The dispatcher branches on
which key is present.

The reason (b) was tempting is that it is a two-hour job. The reason it was
refused is that this whole phase exists because small exceptions accumulated
into a file nobody wants to edit, and (b) is one more of them.

## 9. What not to touch

`shared/extract.py`, `shared/condition.py`, `shared/cost.py`,
`shared/repair.py`, `shared/sources.py`. Pure, tested, and correct. If this
phase starts editing them, it has gone wrong.

## 10. Order of work

1. ~~**The seam, with no new kinds.**~~ **Done 2026-08-02.** `plan.py` went
   from 634 lines to 219; the kind-specific half is `planner/kinds/`
   (`base.py`, `value.py`, `presence.py`) behind a registry, model plumbing is
   `planner/llm.py`, and the shared prompts are `planner/prompts.py`. Prompt
   texts were moved by script rather than retyped — every paragraph in them
   was added after a real failure, so a reworded prompt would be undoing
   evidence. The suite passed (305 tests at that point), the built zip imports
   in its vendored layout, and
   the deployed Lambda answers `KeyError: 'watch_id'` rather than
   `Runtime.ImportModuleError` on an empty payload, which is a free proof that
   every module loads in the real runtime.

   **Naming trap for step 2:** the Lambda zip is flat, so a future
   `shared/kinds.py` would land beside the `kinds/` package directory and
   collide. If declarative kind facts need to be shared with `api`/`checker`,
   call that module something else — `watch_kinds.py`.
2. ~~**`quote` as a kind.**~~ **Done 2026-08-02, both halves.** 2a split
   `Kind` / `CompiledKind` and removed the `known_source` if-statement. 2b
   added `planner/classify.py`, so routing no longer lives in a prompt
   paragraph: a small Haiku call picks the kind before anything expensive
   runs, and `Kind.plan()` is how each kind turns a request into targets.

   **The §1 success criterion is met.** `SEARCH_PROMPT` went from 4,686 to
   3,545 characters — it lost both the KNOWN SOURCES paragraph and the WATCH
   SHAPE paragraph, and is now handed `watch_shape` rather than deciding it.

   A quote also stops paying for the expensive call entirely: it never runs
   Sonnet-with-web-search, because there is nothing to choose. `QuoteKind.plan`
   is one small Haiku call for the condition and the cadence.
3. **Schedule shapes.** **3a (window) done** — `shared/schedules.py` builds
   `rate(...)` or `cron(...)`+timezone, `cost.py` prices from real
   checks-per-month rather than assuming 43,200, and a quote target carries
   `schedule_window` so the api schedules it inside market hours. Proven live:
   `cron(*/5 9-16 ? * MON-FRI *)` / `America/New_York` on a real schedule.

   **3b — `once` — is the next task.** `at(2026-08-03T09:00:00)` plus
   `--action-after-completion DELETE`, so a one-shot removes its own schedule
   instead of leaking one. Both flags verified present on the CLI. This is
   also where §8 lands: a one-shot belongs to a watch, not to a target, so
   `_upsert_schedule` and the Checker's entry point both have to learn the
   `{"watch_id": ...}` shape. Do this **before** step 4 — the reminder is
   unbuildable without it, and doing them together would mix a plumbing
   change with a product change in one diff.
4. **`reminder`** — the kind that proves axis A. No target, no extractor, no
   condition; the schedule firing *is* the event. Expect `Kind` to need a
   `trigger` distinction at this point, the way step 2 forced `CompiledKind`
   out of `Kind`. If it does not, be suspicious.
5. **The `Channel` seam**, with email and `.ics`. See §6 for why `.ics` first
   and why Google Calendar is a different project.

Steps 1 and 3 have no user-visible output, which makes them the ones most
likely to get skipped. They are also the ones the rest depends on.

## 10a. What the first live run taught (2026-08-02)

Phase 9 was written, tested and deployed without ever being run end to end.
The first real run found two bugs in minutes, and both are worth remembering
as *classes* rather than as incidents.

**A suite where every test injects its collaborators cannot see a wiring
bug.** Moving the compile step out of `plan.py` dropped a
`client or Anthropic()`, so every non-quote plan died on `None.messages`.
All 378 tests passed, because every one of them hands in a scripted client —
the single argument that is never None in a test and always None in the
Lambda. Two tests now take the production path deliberately: they pass
nothing. Look for this shape elsewhere.

**A per-action IAM policy fails on the action you added last.** The Phase 5
`schedule_arn` cleanup needed a `dynamodb:UpdateItem` the Notifier did not
have. The email had already been sent, so the failure landed *after* the side
effect that matters, and EventBridge retried — three duplicate emails to a
real person. Fixed on both sides on purpose: the permission is granted, *and*
the tidy-up is swallowed, because a guarantee as important as "a human is not
notified twice" must not rest on an IAM statement being right. **Anything
after the notification is best-effort by construction, not by permission.**

## 11. Risks

- **Four kinds is thin evidence for an abstraction.** `value` and `presence`
  are really one example, so the design rests on `reminder` and `quote`. This
  is why the order above builds the seam against those two rather than
  against "kinds in general".
- **The registry could become the new dumping ground.** Watch for a kind
  module that grows conditionals about *other* kinds; that means the axis is
  wrong.
- **Channels look easy until OAuth.** Tiers 1 and 2 are genuinely small. Tier
  3 is a different project wearing the same word.
