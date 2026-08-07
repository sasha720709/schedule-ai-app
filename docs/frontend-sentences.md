# The sentences this product can already say

Step 1 of the order of work in `docs/frontend-strategy.md`. That document
argues the interface should be designed **outward from the true sentences**
rather than inward from a component library. This is the list of sentences.

It is an inventory, not a proposal. Nothing here is new copy — every line is
already in the code, and the file:line is given so a design can be checked
against what the system can actually produce. Where a sentence exists because
of a specific failure, that is recorded too: **the reason is usually the
argument for how prominent it should be.**

Read this beside the strategy doc, not instead of it.

---

## 1. Why an inventory is the right first step

Three things fall out of having the list in one place, and none of them are
obvious from reading the components:

- **The product's voice already exists and is consistent.** Lower case, no
  jargon, no exclamation, and it consistently says what it does *not* know.
  That voice was not designed; it accumulated, one bug at a time. It should be
  preserved deliberately rather than rediscovered per screen.
- **Most sentences are conditional.** They appear only when something specific
  is true — a shop was rejected, a baseline came from a previous close, a feed
  stopped moving. A layout with fixed slots will either leave holes or force
  padding. **The plan card is a variable-length argument, not a form with
  optional fields.**
- **Several sentences are the entire reason a bug is not still shipping.** They
  cannot be shortened into labels without undoing the fix. They are marked
  **load-bearing** below.

---

## 2. The plan card — the screen to design first

`frontend/src/App.tsx`, the `status === "proposed"` branch. The strategy doc
calls this the heart of the product: the moment the system shows its work and
the user says yes. Roughly fifteen distinct sentences can appear here, and
almost all of them are conditional.

### What was read, and from where

| sentence | when | source |
|---|---|---|
| `read $306.49 just now — this is what will be watched` | a value was verified | `App.tsx:638` |
| `Apple Inc · NASDAQ · USD` | a quote target resolved | `App.tsx:629` |
| `47 items listed on this page today, 3 of which match` | a counting target | `App.tsx:648` |
| the target URL, and `· http` / `· browser` | always | `App.tsx:625` |

**Load-bearing:** the instrument line. Asking for a foreign company by bare
ticker returns the US depositary receipt — "SAP" is the NYSE ADR in USD, not
the Frankfurt listing. This line is the only thing that makes that visible
before confirming rather than never.

**Load-bearing:** the `N of which match` line. A count verified at zero is
honest and says nothing on its own; the unfiltered count is what lets someone
judge a filter before paying for a schedule.

### What is being watched, and what that means

| sentence | when | source |
|---|---|---|
| `trigger when price < 1160.10 ILS` | any condition watch | `App.tsx:508` |
| `(5% from the 1289 read live at planning)` | a relative threshold | `App.tsx:517` |
| `(any move below the 306.40 read at the previous close)` | baseline from a close | `App.tsx:527` |
| `measured across every shop below — the cheapest offer is what this watch calls the price` | multi-shop | `App.tsx:541` |
| `not watching amazon.com — prices in USD, and this watch is about ILS` | a shop was rejected | `App.tsx:553` |
| `any move at all triggers this — at the next open that is close to certain. Say a size ("5% down") if you meant one.` | `relative_change_pct` is 0 | `App.tsx:570` |

**Load-bearing, all six.** In order: the baseline source distinguishes two
promises that look identical on screen; the rejection line exists because
silence about a dropped shop is the same failure as the missing email of
2026-08-04 — the user asked about Amazon, Amazon is not in the list, and
nothing said why; and the "any move" warning exists because a stock never
reopens at its previous close, so `relative_change_pct: 0` fires in the first
seconds of the next session (measured: baseline 306.40, first check 306.49,
fired).

### What it will cost, before anything is scheduled

| sentence | when | source |
|---|---|---|
| `check every [30] min` | always | `App.tsx:700` |
| `≈ $2.32/month` | a target is verified | `App.tsx:719` |
| `· below the 51 min this budget allows` | interval under the floor | `App.tsx:729` |
| `· planner suggested 30` | the user changed it | `App.tsx:734` |
| `cost unknown until a target is verified` | nothing verified | `App.tsx:729` |

This group is the argument for plan-then-confirm existing at all, and it is
the one place in the product where a number is shown *before* it is spent.

### The questions, and what an answer means

| sentence | when | source |
|---|---|---|
| the question text, built from what the search returned | `questions` present | `App.tsx:668` |
| each option, with `[29]` — how many real items it covers | per option | `App.tsx:681` |
| `Answering narrows what is shown here, and tells the watch what to prefer later — it never hides a future posting, only ranks it lower.` | a repeating watch | `App.tsx:693` |
| `Answering narrows what is shown here, and pins what gets watched. Leave it blank and the watch follows the cheapest thing on the page, which is usually an accessory.` | a product watch | `App.tsx:694` |

**Load-bearing.** Those last two are the same machinery meaning opposite
things — a job that misses a preference ranks lower, a product that is not the
pinned one is simply not the product. Showing the wrong one is a lie about
what happens next. The counts on each option are what make the questions
grounded rather than generic.

### A reminder, which has no condition at all

| sentence | when | source |
|---|---|---|
| `reminds you at Thursday 21:00 (Asia/Jerusalem) — learn English` | a reminder | `App.tsx:487` |
| `every day at this time · stops 03/11/2026 · a calendar entry is attached to the email, so your own calendar does the reminding` | a reminder | `App.tsx:494` |
| `Once, or every day?` (as a question chip) | the model answered null | `planner/kinds/reminder.py` |

**Load-bearing:** the zone is named on purpose. `DEFAULT_TIMEZONE` is a
deployment setting until a user profile exists, so a wrong one has to cost a
glance now rather than a reminder at 6am.

---

## 3. A watch in the list, after it is running

| sentence | when | source |
|---|---|---|
| `next check 16:00, in 16 h` | active, has a schedule | `App.tsx:578` |
| `— last read 306.49` | any checked target | `App.tsx:752` |
| `last moved 3 h ago` | value has changed before | `App.tsx:777` |
| `unchanged for 78 checks — a whole trading session without a tick. Last moved 2 d ago; the feed may be frozen rather than the price.` | windowed target, stale | `App.tsx:771` |
| `keeps running — reports each new match once, never the same one twice · 3 reported so far · runs until 03/11/2026` | repeating | `App.tsx:584` |

**Load-bearing:** `next check 16:00, in 16 h` is the entire fix for the
missing-email bug. The schedule was correct — 23:33 Israel is three minutes
past the last New York slot, so the first check was 09:00 the next day — and
nothing said so. It is **computed at read time, never stored**, because a
stored copy is right for about one interval and then a lie.

**Load-bearing:** the staleness line reports and never acts. A still value is
normal for most watches; acting on it would re-create the false positive that
`unavailable` exists to prevent. Note it is *stated* for every target and
*flagged* only where a trading window makes "should have moved by now" a claim
anyone can check.

---

## 4. The eight statuses, and what each must not be collapsed into

`frontend/src/api.ts:14`. The strategy doc is explicit that these are not
decoration: **each exists because collapsing it into another caused a real
bug.** A design that renders them as one coloured pill in eight hues has
thrown away the distinction.

| status | what it means | must not read as |
|---|---|---|
| `planning` | the Planner is running, ~20 s | broken, or ready |
| `proposed` | a plan is ready, **nothing is scheduled and nothing is being spent** | already running |
| `active` | being checked on a schedule | — |
| `paused` | no schedule exists, so no next check can be shown | active |
| `triggered` | the thing happened; terminal, costs nothing | — |
| `failed` | planning failed, carries `plan_error` | a broken watch |
| `degraded` | the page changed and repair did not help; **checking stopped** | the user's fault |
| `expired` | ran its full term; **not a fault** | broken |

Two of these carry a full sentence rather than a word, and both are cases the
user did not ask for:

> `stopped: the site changed and automatic repair did not help — <reason>.
> Checking has stopped, so this costs nothing; delete it and describe it again
> to rebuild against the new page.` — `App.tsx:616`

> `finished: this watch ran its full term and stopped, after telling you about
> 3 things. Nothing went wrong — a watch that keeps running rather than
> stopping at its first result is given an end date so a forgotten one cannot
> check for years. Describe it again to restart it.` — `App.tsx:597`

**A ninth state exists on the target, not the watch:** `blocked`
(`shared/blocked.py:52`). A shop refusing to be read is not a broken watch,
and the difference is actionable — the owner can wait or watch elsewhere. It
reaches the user today only through the degraded email; **the interface does
not surface it yet, and should.**

---

## 5. The emails — the same voice, and the only thing a user sees when away

`notifier/handler.py`. Worth designing against even though they are not
screens: they are the product's output, and the plan card is a promise about
what they will contain.

**Fired** (`:232`) — `Your watch just came true.` Then: what you asked for /
what was found / where / across every shop being watched / why this counts /
checked at / whether it keeps running.

**Every shop, best first** (`:153`) — the answer to *"is that actually the
cheapest?"*:

```
  1899 ILS  ivory  (free delivery, read just now)
  2400 ILS  bug    (before delivery, read 45 min ago)  -- not confirmed recently
```

**Load-bearing:** a quiet shop is shown and flagged, never dropped. Delivery
is one of `free delivery` / `includes 29 delivery` / `before delivery`, and
nothing may turn `unknown` into a number — a plausible shipping figure is the
same class of bug as the fabricated 5%.

**Blocked** (`:296`) — `A shop stopped letting us look`. Says the block is
often temporary and often specific to where the request came from, and says
`Nothing was spent trying to repair it: there was nothing to repair.`

**Expired** (`:271`) — `It told you about 3 things while it ran.` and
`Nothing is broken`.

**Degraded** (`:322`) — names the repair cost: `An automatic repair was
attempted and did not work, which cost about $0.008.`

**Reminder** (`:358`) — the exception that proves the rule. The vocabulary of
every other email is about an outcome, and a reminder has none, so it shares
no wording with them at all.

---

## 6. What the list implies for the design

Observations, offered as input to step 2 rather than as decisions:

1. **The plan card carries 6–15 sentences depending on the watch.** A
   fixed-height card cannot hold it. Length is information: a watch with a
   rejected shop, a previous-close baseline and an open question genuinely has
   more to say than a reminder.
2. **Almost every sentence pairs a measured number with its provenance** —
   *read just now*, *at the previous close*, *45 min ago*, *3 of which match*.
   The strategy doc's "monospace for every measured number" is not a style
   preference here; the numbers and their timestamps are the content.
3. **The product's most distinctive habit is admitting what it does not know.**
   `unknown` delivery, `cost unknown until a target is verified`, `(no
   explanation recorded)`, `the feed may be frozen rather than the price`.
   These should not be styled as errors or hidden as secondary — they are the
   thing that makes the tool trustworthy, and greying them all out would bury
   the product's best feature.
4. **Four sentences are warnings the user should act on before confirming**
   (any-move, below-the-budget-floor, rejected shop, wrong instrument). They
   currently render identically to everything else, as `.muted`. This is the
   clearest existing gap between what the backend knows and what the interface
   shows.
5. **Six kinds are not one object.** A reminder has no condition, no target and
   no cost worth showing; a multi-shop product watch has a table of readings; a
   jobs watch has ranked items with scores and reasons. The strategy doc names
   "every watch rendered as an identical card" as a slop marker, and this list
   is the evidence that they genuinely differ.

---

## 7. What is still missing, and is a content question not a design one

Found while collecting the above. Each is a sentence the system could say and
does not:

- **`blocked` never reaches the interface.** Only the email says it. A target
  refused right now shows as an ordinary error.
- **A paused watch says nothing about what pausing costs or preserves.** It
  drops the schedule, so it bills nothing — worth saying, since the user is
  choosing between pause and delete.
- **`planning` has no sentence at all**, and it lasts ~20 s. The one screen
  where the product is silent is the first one a new user sees.
- **Nothing states the sender address or that a calendar entry is attached**
  until after a reminder fires. The plan card mentions the attachment; nothing
  says *where* mail will arrive.
