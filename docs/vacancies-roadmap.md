# Vacancies: what exists, what it costs, and what the owner is asking for

Written 2026-08-04, in answer to four questions: what do we have, how does it
differ from a *personalised* job watch, what does a check cost, and how could
the experience be better.

The short version: **the machinery works and is nearly free, the notification
is close to useless, and the personalisation idea is both affordable and a
genuine engineering improvement rather than a nicety.**

---

## 1. What a vacancy watch actually is today

The request `"tell me when a student cloud engineer vacancy appears in Beer
Sheva"` is the request the `presence` kind was built for — it failed outright
before Phase 8b, with `nothing to watch`.

What happens now:

1. `classify.py` sorts it to `presence` with one small Haiku call.
2. Sonnet, **with web search**, proposes 1–3 job-board URLs.
3. The page is fetched — plain GET first, Chromium only if that fails.
4. Haiku reads it and returns a **neighbouring listing** as the anchor. Not
   the job wanted, which does not exist: another listing of the same kind,
   because that is what reveals the list's markup.
5. Sonnet compiles a counter — a `scope` (the list container) plus a
   `selector` matching one item, filtered by visible text, e.g.
   `a.job-title:-soup-contains("Cloud Engineer")`.
6. It is **verified before the plan is offered**: the selector is re-run with
   its text filter stripped and must match something today. A wrong item class
   would otherwise count zero today, count zero forever, and never report a
   fault — alive, billed, and incapable of firing.
7. Every tick: plain GET, count the matches in Python, compare. **No model.**
8. Count crosses the condition → email → schedules deleted → done.

That is a real, working, verified pipeline. The problems below are not
"it doesn't work". They are "it works, and then tells you almost nothing".

---

## 2. What a check costs — the direct answer

**A vacancy check is a plain HTTP GET with no model call. It is the cheapest
thing this product does.**

| interval | checks/month | cost/month |
|---|---|---|
| every minute | 43,200 | **$0.18** |
| every 5 min | 8,640 | **$0.036** |
| every 15 min | 2,880 | **$0.012** |
| every hour | 720 | **$0.003** |

Per check: **$0.0000041**. The $5 monthly budget is nowhere near binding —
even at one-minute checks a vacancy watch uses 3.6% of it.

**Browser or not?** Decided per site, by trying, not by asking a model. The
Planner does a plain GET, compiles against the raw HTML, and escalates to
Chromium only when that fails. Evidence from the 8b pass 3 live run: **the
Israeli job boards stayed `http`.** So for the sites that matter here, no
browser.

It matters when it happens, though — Chromium is **45×**:

| | every 5 min | every hour |
|---|---|---|
| HTTP | $0.036/mo | $0.003/mo |
| browser | $1.61/mo | $0.134/mo |

Boards that render their results in JavaScript (LinkedIn is the obvious one)
will land on the browser path. Still affordable, but it is the difference
between "check every minute without thinking" and "check every 15 minutes".

**The conclusion that should drive the design: cost is not the constraint for
this feature.** Unlike shares, where interval choice was the whole game, here
you can afford to watch several boards at a sensible cadence and still spend
under a dime a month. Spend the budget on *quality of match*, not on frequency.

---

## 3. How it differs from what the owner is describing

Seven gaps, worst first.

### 3.1 The email tells you a number, not a job

This is the one to fix first, and it is slightly embarrassing.

A `count` extractor returns an integer, and Tier 0 sets `note` to `""`. So the
triggered email reads, verbatim:

```
What was found:
  1

Where:
  https://www.alljobs.co.il/...

Why this counts:
  (no explanation recorded)
```

The user is told that one matching thing exists somewhere on a page. No title,
no link to the posting, no description, no idea whether it is the job they
wanted. They have to go and find it themselves — which is most of the work
they asked to be spared.

**Fix:** the `count` kind must return the *matched items* — their text and
`href` — not merely how many there were. That is a change in
`shared/extract.py` and it is the highest value-per-line change in the whole
feature.

### 3.2 The filter is one word, on purpose

`COUNT_PROMPT` says it outright:

> Filter on ONE distinguishing term rather than a whole phrase.
> Do NOT try to encode every criterion the user mentioned. Being slightly
> loose is right: a watch that fires on a near-match wastes one email, while a
> watch too strict to ever fire wastes the whole point of the watch.

So `"student cloud engineer in Beer Sheva"` becomes roughly
`:-soup-contains("Cloud")`. **Student, Beer Sheva and the hours are silently
dropped.** That reasoning is correct as far as it goes — a CSS selector is a
bad place to encode "part-time, near my neighbourhood, suits a student" — but
it means today's watch answers a much weaker question than the one asked.

### 3.3 There is no notion of "best"

The condition is `count > 0`. Nothing ranks, scores or compares. "Find the
best vacancy it can" has no representation anywhere in the data model.

### 3.4 It fires once and stops

Like every watch: the Notifier emails, deletes the schedules, and `triggered`
is terminal.

**For job hunting that is the wrong shape.** A job search is a stream you
follow for weeks, not a single event. This is exactly the recurring-watch idea
shelved as optional in `shares-roadmap.md` §6 — and it matters far more here
than it ever did for shares. A share watch firing once is usually right; a job
watch firing once and going silent is a broken product.

### 3.5 Nothing remembers which postings have been seen

A prerequisite for 3.4. Without it a recurring watch re-notifies about the
same posting every tick. Needs a stable identity per listing — a hash of
title + `href` is enough.

### 3.6 No clarifying questions

One-shot: the request goes in, a plan comes out. The owner's idea — the chat
asking *which neighbourhood, what hours, are you a student* — is multi-turn
chat, deferred as sub-phase 4d. See §4: it is the cheap half.

### 3.7 The text filter is the one thing that cannot be verified

Structural, and worth stating plainly. `prove_the_item_selector()` proves the
container exists and the bare item selector matches real listings — but
**nobody can verify a text filter against a posting that does not exist yet.**
A watch can be silently too strict: it counts zero, which is legitimate, so
Tier 3's staleness signal will not flag it either.

This is the presence kind's worst failure mode, and §4 turns out to fix it.

---

## 4. The personalisation idea — affordable, and better engineering

The owner wants the chat to ask specifying questions and then find the *best*
match rather than any match.

**The instinct against this is that judging every listing needs a model on
every check — the $49/month that Phase 8 exists to have removed.** That
instinct is wrong, and the reason is the whole design:

> **Judging costs per *new posting*, not per check.**

A cheap deterministic counter detects that something appeared. Only then does
a model read it. That is precisely the Tier 0 / Tier 1 shape already in the
Checker.

| | cost |
|---|---|
| one deterministic check | $0.0000041 |
| one Haiku judgement of a new posting | $0.0057 |
| **niche query**, ~5 new matches/month | **$0.03/month** |
| **broad query**, ~200 new matches/month | **$1.14/month** |
| judging on *every* tick at 5 min, for comparison | **$49.36/month** |

So a personalised vacancy watch on a niche query costs about **three cents a
month**, and a deliberately broad one about a dollar. The clarifying questions
are one extra model call at creation time — a rounding error, paid once.

**And it fixes 3.7.** Today the CSS filter has to be simultaneously loose
enough to ever match and tight enough to be useful, which is a contradiction,
and nobody can verify it. Under a two-stage design the selector should be
*deliberately loose* — catch every job on the board — and the model decides
whether each new one is worth an email. A loose filter that cannot be verified
is fine; a tight one that cannot be verified is the bug.

This is the strongest argument for the owner's idea: it is not only a nicer
experience, it removes the failure mode the kind cannot currently detect.

**What it needs, in order:** matched items rather than a count (3.1), seen-IDs
(3.5), recurring watches (3.4), then the judgement step, then the clarifying
questions.

---

## 5. Ideas for the experience

Ordered by value per unit of work.

1. **Put the job in the email.** Title, link, one line of why it matched.
   Turns "1" into something actionable. (3.1)
2. **Show what it can see, at plan time.** The Planner already computes the
   unfiltered count as a health check and throws it away. Say it on the plan
   card: *"47 jobs listed on this page today, 3 of which mention Cloud."*
   That converts an unverifiable watch into a visible one, and it is nearly
   free — the number is already computed.
3. **A "what would this match right now?" preview** before confirming, listing
   the titles that pass the filter today. The single best defence against a
   filter that is too strict, and against 3.7.
4. **Digest, not spam.** When recurring watches exist: if five postings appear
   in an hour, send one email with five, not five emails.
5. **Score, do not just match.** *"8/10 — student position in Beer Sheva, but
   20 h/week where you asked for 12."* A near-miss the user can judge is worth
   more than silence, and this is what the judgement step produces anyway.
6. **Clarifying questions at creation.** (3.6, sub-phase 4d.) Ask two or
   three, never a form. The questions worth asking are the ones that change
   the *plan*, not the ones that merely sound thorough.
7. **Several boards in one watch.** Structurally supported already — the
   search step returns 1–3 targets — but in practice the live runs used one.
   At $0.012/month per board there is no reason to be shy.
8. **Say when it will look, and when it last saw a change.** Both already
   exist as of 2026-08-04 (`next_check_at`, `last_changed_at`) and cost
   nothing to surface here.

---

## 6. Recommended order

**Nothing here needs a cost decision**, which is unusual for this project and
should be enjoyed.

1. Matched items instead of a bare count (3.1) — plus the plan-time preview
   (5.2, 5.3), which falls out of the same change.
2. Seen-IDs (3.5) and recurring watches (3.4). These two are one piece of
   work; a recurring watch without dedup is worse than no recurring watch.
3. The judgement step (§4) — Tier 1 over *new* postings only.
4. Clarifying questions (3.6), last, because they are the only part that needs
   multi-turn chat and the only part that is pure gain rather than a
   correction.

Steps 1 and 2 are what makes the feature usable. Steps 3 and 4 are what makes
it the thing the owner actually described.
