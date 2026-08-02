# Phase 9 — Watch kinds, schedules and delivery

Status: **design, agreed in discussion 2026-08-02. One decision still open
(§8).** Nothing here is built yet.

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
`repair.py` are pure, well-tested (147 tests) and untouched by this phase.
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

**Window** — `cron(...)` plus a timezone. Market hours become
`cron(* 9-15 ? * MON-FRI *)` in `America/New_York`, and **no code in the
Checker learns what a stock market is.** The schedule simply does not fire.

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
| stock, 1 min, market hours | 8,190 | **$0.034** |

The budget was never binding. $5 buys 1,214,574 HTTP checks a month, and a
month contains only 8,190 market minutes — the budget would permit checking
every 0.4 seconds. The saving is fourteen cents.

**So windows are a correctness feature that happens to save pennies.** The
real defect they fix: outside trading hours CNBC's `last` holds the previous
close, so a relative watch ("tell me when Apple goes down") can fire at market
close against a frozen number rather than on a real move — the same family as
the CNN "Last closed at" bug, a correct reading of the wrong thing. A watch
that does not run after 16:00 cannot make that mistake.

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

- **Rules first, model second.** A `quote` is recognised by a rule (a company
  or ticker, no "where", no price-of-a-product phrasing). A `reminder` is
  recognised by a rule (an explicit time, no thing-to-observe).
- **The default is the existing path.** Anything unmatched goes to `value` /
  the searching Planner, which is what happens today. An unknown request must
  degrade to current behaviour, never to a refusal.
- **Classification is verifiable.** The chosen kind is stored on the row and
  shown on the plan card, so a wrong fork is visible before confirmation
  rather than discovered a week later.
- **No new prompt paragraph.** Each kind's rules move *into its own module's*
  prompt. `SEARCH_PROMPT` shrinks; it stops being the dumping ground, which
  is the actual point of §1.

## 8. The open decision — who owns the schedule

**Not yet agreed. This is the one thing blocking implementation.**

Today a schedule is created per *target*: named `schedule-ai-app-{target_id}`,
and the Checker is invoked with a `target_id`. A reminder has no target.

- **(a) Schedules move to the watch for time-triggered kinds.** Honest, and
  right long-term. Costs edits in the Checker's entry point, the Notifier's
  teardown, `api`'s confirm/patch/delete, and the tests for all of them.
- **(b) A reminder gets one synthetic target row.** Nothing else changes.
  Cheap — and it makes the table describe a target that does not exist, which
  is precisely the class of lie removed from the Notifier in Phase 5.

Recommendation: **(a)**. The reason (b) is tempting is that it is a two-hour
job, and the reason to refuse it is that this phase exists because small
exceptions accumulated into a file nobody wants to edit.

## 9. What not to touch

`shared/extract.py`, `shared/condition.py`, `shared/cost.py`,
`shared/repair.py`, `shared/sources.py`. Pure, tested, and correct. If this
phase starts editing them, it has gone wrong.

## 10. Order of work

1. **The seam, with no new kinds.** Extract `value` and `presence` out of
   `plan.py` into modules behind a registry; prove the suite still passes.
   Nothing user-visible changes. This is the risky step and it is first, alone.
2. **`quote` as a kind**, moving `sources.py` routing out of `SEARCH_PROMPT`
   and adding the market-hours window. First user-visible win, and it fixes
   the frozen-close defect.
3. **Schedule shapes**: window and once, plus the §8 decision.
4. **`reminder`** — the kind that proves axis A.
5. **The `Channel` seam**, with email and `.ics`.

Steps 1 and 3 have no user-visible output, which makes them the ones most
likely to get skipped. They are also the ones the rest depends on.

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
