# Finishing the shares feature

Written 2026-08-04, at the owner's request, after the second live overnight
run. The brief was: *what is not okay, what is not done, what should be done —
and argue everything.*

So this document argues. Where the owner's position holds it says so and moves
on; where it does not, it says that instead. Three of the entries below are
recommendations **not** to build something.

---

## 0. What "finished" is supposed to mean

The product shape, as stated: three ways to watch — a **share price**, a **job
vacancy**, a **thing for sale** — plus **calendar reminders**, then auth and
the frontend, and that is the product. Nothing further afield.

**That framing is right, and it is smaller than it sounds.** Those are not
three features. They are one engine used at three levels of trust in the
source:

| | where the target comes from | who owns the extractor | built? |
|---|---|---|---|
| `quote` | registry (`shared/sources.py`) | us | yes, live twice |
| `value` | web search, compiled, verified | the model, verified against the page | yes, live |
| `presence` | web search, compiled as a counter | the model, verified | yes, live |

All three already run end to end against real AWS. So "finish the three" is
one hardening pass over shared machinery plus a short list of per-kind gaps —
not three projects. That is the good news and it should be said plainly before
the list of defects below makes it look worse than it is.

**One correction to the plan, though.** "Then we'll add authorization" is
listed with the polish. It is not polish. `user_id` is hardcoded to
`"default"` in every query, and `NOTIFY_EMAIL` is a single environment
variable — one recipient for the entire system, baked into the Notifier. A
second user changes every table read, the SES send, and the passcode model at
once. **Decide the auth shape before polishing three features on top of a
single-user assumption**, or the polish gets redone. It does not have to be
*built* early. It has to be *decided* early.

---

## 1. The baseline decision, and why it does not close the issue

**The owner's position:** the previous close is a fine baseline. If you ask on
Sunday what "current" means, Friday's close *is* current — there is no other
number.

**Agreed, and it is now a decision.** A frozen quote is the last real price,
not a wrong one. Taking it as the baseline is honest, needs no code, and the
alternative — refusing to plan a watch outside market hours — would be a worse
product. `shared/schedules.py` should stop describing this as the real
correctness bug.

**But accepting it moves the problem rather than removing it**, and the moved
version is sharper:

A stock does not open at the previous close. Ever, essentially. So a watch
whose condition is "different from current", created at 23:33 with Monday's
close as its baseline, **is guaranteed to fire in the first seconds of
Tuesday's open**. Not likely — guaranteed, to several decimal places.

This is not theory. It is what happened today, in-hours, when the same
condition was tested:

```
baseline    306.40   read at 16:33:11
first check 306.49   at 16:35:05   -> condition met, email sent
```

A 9-cent move, 0.03%, on the very first check. Overnight the gap is larger and
the outcome identical. The owner would have received, at 16:30 Israel time, an
email saying Apple had changed — carrying no information whatsoever, because
it could not have said anything else.

So the defect is not the baseline. It is that **"any change" is not a
condition on a continuous quantity; it is a guarantee.** And the product
currently accepts it without comment.

**What to do — and specifically what not to do.** `plan.py`'s prompt already
forbids inventing a percentage on the user's behalf ("do not decide the user
meant a meaningful one"), and that rule is correct: guessing 5% is how the
fabricated-threshold bug happened in the first place. Do not undo it.

Instead, **say the consequence at plan time**, where the user can act on it:

- store `baseline_at` and `baseline_source` (`live` or `previous_close`)
  alongside `baseline`, so the number can be checked against a session;
- have the plan card say, for `!=` with `relative_change_pct: 0`:
  *"baseline $306.40, Monday's close — any move triggers this, so it will
  almost certainly fire at the opening bell. Want a size instead?"*

That is one stored field and one sentence. It is the same fix as the one this
session already shipped for the silent window, applied to the other end of the
same watch: **the system knows something the user does not, and the fix is to
say it, not to guess for them.**

---

## 2. What is actually not okay

Ordered by how badly it bites, with the evidence for each.

### 2.1 Non-US symbols are silently wrong — worse than broken

Probed today from a real Lambda IP:

| asked | CNBC returned |
|---|---|
| `WALMEX` (Bolsa Mexicana) | `last: null` — planning failed |
| `AMX` | **$25.01, NYSE, USD** — the American ADR |
| `SAP` | **$193.50, NYSE, USD** — the ADR, not Frankfurt |
| `TSLA` | $324.82, NASDAQ, USD — correct |

The `WALMEX` failure is visible: on 2026-08-03 it produced
`canned extractor gave failed: no key 'last'` and the watch was rejected. Ugly
message, right outcome.

**`AMX` and `SAP` are the real problem.** The owner asked about a Mexican
listing and got a confidently-returned number for a *different security* on a
*different exchange* in a *different currency*. Nothing in the plan card said
so — it showed a bare `25.01`. A watch built on that tracks the ADR forever
and the owner has no way to notice.

A wrong answer stated confidently is worse than a refusal, and this is the
clearest instance of it in the codebase.

**Fix.** CNBC returns `exchange`, `currencyCode` and `name` in the same
payload the extractor already reads. `sources.expand()` should pull them and
the plan card should show **"AMX — América Móvil SAB de CV · NYSE · USD"**. If
the request named an exchange (`BMV: WALMEX`) and the response disagrees,
refuse with that sentence rather than a jsonpath error. Small, and it converts
the whole class from silent to obvious.

### 2.2 One source, no fallback, and it is IP-sensitive — proven today

`shared/sources.py` has exactly one URL, and `QuoteKind.self_heals = False`,
so 8d will not even attempt a repair.

Today the same request was made from two places:

- from the Codespace: **HTTP 403, "Access Denied"**, an Akamai block page;
- from a Lambda in `us-east-1`: normal JSON.

CNBC's free keyless endpoint already blocks IP ranges. The registry exists
*because* Yahoo started returning 429 and stooq started 404ing from datacenter
IPs — this is the third instance of the same thing, and the first two ended
the same way.

**If AWS's range is blocked, every share watch in the product breaks at the
same moment**, and the failure will surface as a per-watch extraction error
that looks like a broken extractor. There is no signal that distinguishes
"CNBC blocked us" from "one watch's page changed".

**This is the largest structural risk in the shares feature** and the reason
it heads the list once 2.1 is done. Fix: a second entry in the registry with
the same output shape and failover between them, plus a distinct
`source_unavailable` outcome so the alarm says what actually happened. The
registry is already the right seam — this is one file.

### 2.3 The trading window is hardcoded to New York

`US_MARKET` is the only `Window`, and `QuoteKind.window` is a class constant
pointing at it. Every quote watch, for every symbol, gets
`cron(... 9-16 ? * MON-FRI *)` in `America/New_York`.

For a US symbol that is right. For anything else it ranges from harmless to
completely wrong:

- Mexico City (BMV) is close enough to New York's hours to be tolerable;
- **Tel Aviv (TASE) trades Sunday to Thursday.** A `MON-FRI` window misses
  Sunday's session entirely and polls all Friday while the exchange is shut.
  Given where the owner is, this is not a hypothetical.

**Fix, and it is nearly free**: the exchange is already in the payload 2.1
makes us read. Map exchange → window in `shared/schedules.py`, which is
already a registry of windows keyed by name. `QuoteKind.window` stops being a
constant and becomes a lookup on the resolved target.

### 2.4 Nothing notices a value that has stopped moving

`last_value` is overwritten on every check and there is no `last_changed_at`.

For quotes this has a specific failure: if CNBC freezes `last` for a symbol —
a delisting, a trading halt, a bad-but-plausible ticker that resolves to a
dormant listing — every check returns `ok` with the same number, forever. A
watch on `!=` never fires, no error is ever recorded, and the owner concludes
the price never moved.

That is the exact silent-rot shape `shared/condition.py` refuses to permit for
an unknown operator, and it is unguarded one layer up. Already listed as open
in `CLAUDE.md` since Phase 8b; shares is where it does real damage, because a
share price that has not moved in four hours of an open session is not a
stable price, it is a broken feed.

**Fix**: store `last_changed_at`; if a value has not changed across N checks
*inside a trading window*, mark the watch and say so. Cheap — one field and
one comparison.

### 2.5 Currency is never set, and the baseline is never labelled

`condition.currency` comes back `null` on every quote watch planned so far,
while the payload carries `currencyCode`. Combined with 2.1 the plan card can
show `25.01` for a peso-denominated request and be wrong in two ways at once.

The `baseline_at` / `baseline_source` fields from §1 land here too. One pass
over `sources.expand()` and the plan card closes 2.1 and 2.5 together.

### 2.6 No history at all

`last_value` is overwritten, so there is no way to draw what the price did
today, and no way to answer "was it close?" after a watch fires.

**This is a product gap, not a defect**, and it is the one item on this list I
would put *after* the frontend rather than before it: history is worth
building when there is a chart to put it in. Recording it earlier is cheap
insurance, though — a `Checks` table written on every tick costs
approximately nothing at these volumes, and data not recorded cannot be
backfilled.

### 2.7 Pre- and post-market prices are invisible

Documented honestly in `sources.py`: the live extended-hours number sits in
`ExtendedMktQuote.last` and cannot be selected conditionally without a
jsonpath filter engine this project has deliberately refused to grow.

Leave the refusal in place. Once 2.3 makes windows per-exchange, "watch
extended hours too" becomes a *second window* plus a second registry entry
with a different path — a choice the user makes, not an engine feature. That
is the cheap version and it should wait until someone asks for it.

---

## 3. Three things I recommend not building

Padding a roadmap is easy. These are the ones to leave alone, with the reason.

**Market holidays.** The window fires 9-16 MON-FRI on Thanksgiving and July
4th. Cost of the omission: roughly 10 days a year × 8 hours × 12 checks/hour ≈
**960 wasted requests per watch per year**, against an endpoint that serves
tens of thousands. Cost of the fix: a holiday calendar per exchange, which
must be maintained forever and is wrong the first year nobody updates it.
**Not worth it.** Revisit only if the endpoint starts rate-limiting.

**Self-healing for quotes.** `self_heals = False` is correct and should stay.
The extractor is ours and the payload is a documented JSON shape; if it
breaks, the fix is one line in `sources.py` for every watch at once. Paying
Haiku to rediscover it per watch would be slower, more expensive, and produce
inconsistent extractors across watches. Do not "fix" this to match the other
kinds.

**A paid market-data API.** It resolves 2.2 completely and honestly. It also
introduces a bill, a key to store, and a dependency on someone's free tier
staying free — for a single-user learning project whose entire share-watching
volume is a few thousand requests a month. **A second free source with
failover buys most of the resilience for none of the cost.** Reconsider only
if the second source also gets blocked, at which point the pattern is real
rather than assumed.

---

## 4. Order of work

Grouped so each step ships something provable, cheapest-first within a tier.

**Tier 1 — stop being confidently wrong. ✅ DONE 2026-08-04.**

1. ✅ `sources.describe()` reads `exchange`, `currencyCode`, `name` out of the
   response already fetched — no second request. Stored on the target row and
   shown on the plan card. An uncovered symbol raises `NotCovered` with a
   sentence instead of a jsonpath error. (§2.1, §2.5)
2. ✅ `baseline_at` + `baseline_source` (`live` / `previous_close`), decided by
   `schedules.in_window()`. The plan card says which reading the threshold came
   from, and warns when a zero-percent condition will fire at the open. (§1)

**Tier 2 — stop being fragile. Half done; the other half withdrawn.**

3. ✅ Exchange → window lookup (`sources.EXCHANGE_WINDOWS`);
   `QuoteKind.window` is now a fallback, not the answer. Tel Aviv, XETRA and
   London added. **Proven live**: "Bank Leumi on the Tel Aviv Stock Exchange"
   → `LUMI-IL` → `cron(*/5 9-18 ? * SUN,MON,TUE,WED,THU *)` in
   `Asia/Jerusalem`. (§2.3)
4. ⛔ **Second source with failover — dropped by the owner, 2026-08-04.** The
   CNBC 403 from the Codespace was judged not a real risk. The argument in
   §2.2 stands and is left there deliberately: if the Lambda range is ever
   blocked, every share watch breaks at once and it will look like per-watch
   extractor failure. Revisit at the first `source_unavailable` in production.

**Tier 3 — stop being silent.**

5. `last_changed_at` and a stale-value signal. (§2.4)

**Tier 4 — after the frontend, or cheaply now if you want the data.**

6. A `Checks` history table. (§2.6)

Tiers 1 and 2 are what "shares is finished" should mean, and they are done
apart from the withdrawn item. 3 is shared with the other two kinds and is
better done once for all of them. 4 is a product decision, not a defect.

---

## 6. Two corrections from the owner, 2026-08-04

**"Change from current" was a test request, not a real one.** Real requests
are "5% down", "7% down", "goes up from current". Fair, and it lowers the
priority of §1's warning — but not to zero: **"goes up from current" is still
`relative_change_pct: 0`**, so the certainty argument applies to it unchanged.
The warning is one line of UI and it is built; it now only fires on the case
that genuinely warrants it.

**Logging each firing is not §2.6, and it needs something that does not
exist.** The owner described: a watch on "5% down" stamps the moment it first
drops 5%, then stamps again the next time it does, *while the watch is still
running*. That is a different feature from §2.6 in two ways.

- §2.6 is a log of **every check** — the price series, one row per tick,
  which is what a chart is drawn from. What the owner described is a log of
  **firings**, which is a much smaller and much more interesting table.
- More importantly: **today a watch that fires is finished.** The Notifier
  emails, deletes the schedules, and the status becomes `triggered`, which is
  terminal. There is no "while the watch is not stopped yet" — that state does
  not exist. "Fires more than once" is a **recurring watch**, and it is a new
  capability, not a log.

That capability is worth having and it is not free. A watch that keeps running
after firing needs: a re-arm rule (does the baseline reset to the new price,
or stay at the original?), a way not to email every five minutes while the
price sits 5.1% down, and an end condition, since a recurring watch bills
forever where a one-shot stops by itself. The re-arm question in particular is
a product decision with no obvious default — "tell me each time it drops 5%"
means something quite different depending on whether the 5% is measured from
the original baseline or from the last alert.

**Recommendation: build it, after Tier 3, and decide the re-arm rule first.**
`last_changed_at` from Tier 3 is a prerequisite anyway — you cannot sensibly
suppress repeat alerts without knowing when the value last moved.

---

## 7. On calendar reminders, since it came up

The owner's estimate: *"it's not so hard — write an `.ics` file and attach it
to the email."*

**Mostly right.** `.ics` is about fifteen lines of plain text, needs no
library, works with Gmail, Outlook and Apple Calendar, and needs no OAuth —
which is exactly why `CLAUDE.md` already records it as the recommended first
step over Google Calendar. The only real change is SES: attachments require
`send_raw_email` rather than `send_email`, which is a different API call and a
different IAM action. Half a day, honestly.

**Two things the estimate misses.**

**The `.ics` is the delivery. The reminder is the trigger, and it does not
exist yet.** "Remind me at 9am to learn English" needs a watch that fires on a
*time* rather than a condition — Phase 9 step 3b, the one-shot and recurring
schedule, plus a `reminder` kind that stores `targets: []` and whose schedule
invokes with `{"watch_id": ...}` so the Checker's entry point has to branch.
That is the actual work. The `.ics` is the easy half of a two-part job, and
building it first produces nothing usable.

**Attaching an `.ics` to a condition-watch email is close to pointless.**
"Apple dropped below $300" is an event that already happened; a calendar entry
in the past is clutter. Calendars are for future commitments, which is
precisely what the reminder kind produces and what a triggered watch does not.

So: build the reminder trigger first, and the `.ics` as its delivery. Do not
build the `.ics` as a feature of watch notifications — that is the ordering
the "it's easy" framing invites, and it would ship something nobody wants.

**One warning, given the week we have had.** An `.ics` with a wrong or missing
timezone puts the reminder at the wrong hour, and the user blames the app. Use
UTC with a trailing `Z` in `DTSTART`, or a full `VTIMEZONE` block — never a
naive local time. This session already lost a night to a timezone that was
right and unexplained; the same field is easy to get outright wrong.
