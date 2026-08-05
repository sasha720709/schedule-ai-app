# Marketplaces: what is actually hard, and what Amazon costs

Written 2026-08-04, answering three things the owner asked: should Amazon be in
the list, what would an external API cost per request and per month, and what
personalisation a product watch needs beyond "the product and the platform".

Everything below that carries a number was measured today, from a real Lambda,
not recalled.

---

## 1. Amazon: no external API needed, and the official one is gone

### The official route is closed

Amazon's Product Advertising API **is deprecated as of 15 May 2026 and is no
longer accepting new customers.** Even before that it was unusable here: it
required an Amazon Associates account with **three qualifying *sales* in the
last 30 days** to keep access. A price-watching app makes no sales. It was
never an option, and now it is not an option for anyone.

### The paid route has a floor that dwarfs the whole budget

| | per Amazon request | monthly floor |
|---|---|---|
| ScraperAPI | **$0.00735** (Amazon = 5 credits + 10 for JS = 15; 100k credits for $49) | **$49** |
| ScrapingBee | **~$0.0147** (stealth proxy = up to 75 credits) | **$49** |

The per-request figures are bad enough — 40× to 80× what our own browser costs
— but **the $49/month floor is what settles it.** This project's entire budget
is $5 per watch per month. An external API costs ten times that before a single
request is made, for a single-user learning project.

### And it turns out we do not need one

Phase 6 recorded that "Amazon interrupted navigation, against a real Chromium"
and accepted Amazon as out of reach. **That is no longer true.** Six
consecutive renders of `amazon.com/s?k=xbox+series+x` from the Fetcher today:

    run 1: 1,116,895 chars, 22 product cards, no captcha
    run 2: 1,131,885 chars, 22 product cards, no captcha
    run 3: 1,168,138 chars, 22 product cards, no captcha
    run 4: 1,012,075 chars, 16 product cards, no captcha
    run 5: 1,125,348 chars, 16 product cards, no captcha
    run 6:   996,306 chars, 16 product cards, no captcha

With real prices parsed straight out: *Xbox Series X 1TB — $754.94*, *Xbox
Series X 1TB Digital (Renewed) — $649.99*.

Either Amazon relaxed, or the Fetcher's explicit page/context/browser teardown
from Phase 5 changed the fingerprint. Six for six is good evidence but not a
guarantee: Amazon's blocking is probabilistic and IP-dependent, and a Lambda's
address can change.

### What this costs

| | per check | at 6-hourly | hourly | every 15 min |
|---|---|---|---|---|
| **our Chromium** | $0.000186 | **$0.02/mo** | **$0.13/mo** | **$0.54/mo** |
| ScraperAPI | $0.00735 | $0.88/mo | $5.29/mo | $21.17/mo |
| *plus their floor* | — | +$49/mo | +$49/mo | +$49/mo |

**Recommendation: put Amazon in, on our own browser, and treat "Amazon blocked
us" as a first-class outcome rather than a mystery failure.** The extraction
engine already has three outcomes; a blocked render should be its own recorded
state so that the day it starts failing is visible in one place instead of
appearing as a broken extractor on every Amazon watch at once. Revisit a paid
API only if that state starts firing — and then only for Amazon, not for
everything.

---

## 2. What is actually hard here, and it is not the source

Quotes got a registry because the *source* was the problem. Jobs got a registry
because the *source* was the problem. **For products the source is the easy
part.** Two other things are hard, and the owner named both.

### 2.1 Particularization — *which* thing

"Tell me when the Xbox gets cheaper" does not identify a product. Series X or
Series S? 1TB disc or 512GB digital? Bundle or bare console? New or renewed?

This is not a hypothetical, and today's Amazon render proves it in one line:

    Xbox Series X 1TB                      $754.94
    Xbox Series X 1TB Digital (Renewed)    $649.99

A watch on "the cheapest Xbox Series X" fires on the refurbished one, at a
price the user cannot buy the thing they meant for. **A price watch on the
wrong variant is worse than no watch**: it is confidently wrong, on a schedule,
about money.

Today nothing pins this down. The Planner web-searches, picks a page, and
compiles a selector against whatever product that page happens to be about.

### 2.2 Localisation — where you can actually buy it

A price on amazon.com is not actionable from Be'er Sheva if the thing does not
ship there, and it says nothing about the shop two streets away.

The system currently has **no notion of where the user is.** `user_id` is
hardcoded to `"default"` and there is no profile of any kind. The jobs kind
gets a country by inferring it from the request; a product watch needs the same
thing, and more often, because "cheapest" is meaningless without a country.

---

## 3. The proposal, including the part the owner did not ask for

### 3.1 A product watch should watch a product, not a page

This is the structural change, and it is the one worth arguing about.

Today a `value` watch watches **one URL**. For a product that is the wrong
unit. The right unit is *this exact thing, wherever it is sold*: a basket of
shop pages, with the condition evaluated over the **best offer across all of
them**.

That changes the product in ways that matter:

- "under ₪2,000" becomes true when **any** shop hits it, and the email says
  which shop and links to it.
- A shop that stops carrying the item is one target going quiet, not a broken
  watch.
- "Where can I buy this?" is answered by the watch itself, continuously.

**The honest cost of this: the Checker evaluates conditions per target, one
target per tick.** Each target fires independently today. "Cheapest of five
shops" needs a watch-level evaluation that does not exist — and that is the
real engineering in this phase, not the scraping.

### 3.2 Pin the product with the questions machinery already built

Step 4 of the vacancies work built exactly the tool this needs:
`shared/questions.py` asks about **what a search actually returned**, with real
options and real counts.

Point it at products and it becomes particularization:

    ? Which one did you mean?
        [3] Xbox Series X 1TB (disc)
        [2] Xbox Series X 1TB Digital
        [4] Xbox Series S 512GB
    ? Condition?
        [7] New          [2] Renewed / refurbished

Nobody writes those options. They are what the shops are actually selling
today. This is the same inversion that made the job questions worth asking, and
it is a far better answer to "which product" than a spec form or a guess.

### 3.3 A shops registry per country

`shared/shops.py`, on the pattern of `job_boards.py`. Probed today over plain
HTTP from the Codespace:

| | | |
|---|---|---|
| `zap.co.il` | Israeli price **aggregator** | reachable; prices load late — needs the browser and more work |
| `ivory.co.il` | Israeli electronics | 969KB of HTML, plain GET |
| `bug.co.il` | Israeli electronics | 1.4MB of HTML, plain GET |
| `amazon.com` | US | browser only, 6/6 today |

**`zap.co.il` is the one worth the effort.** It aggregates every Israeli
retailer, so a single target answers "which shops near me have it and at what
price" — the exact question the owner asked, and the same leverage LinkedIn's
guest endpoint gave for jobs. It is not free: one render showed 27 shop rows
but only one price, so the prices arrive after load. Promising, unproven.

### 3.4 Preferences worth having, beyond location and branch

The owner listed location and specific shops. Both are right. These are the
ones that also change what a watch should say, ordered by how often they would
bite:

1. **New / used / refurbished.** The $649 vs $754 trap above. This is the
   single most valuable one and it is nearly free — it comes straight out of
   the listing text the questions step already reads.
2. **Total landed price, not the sticker.** An item ₪50 cheaper with ₪60
   shipping is not cheaper. A watch that compares sticker prices across shops
   is comparing the wrong number.
3. **Variant** — capacity, size, colour. Part of particularization.
4. **In stock now, or any listing.** The engine already distinguishes these
   (`unavailable` vs `ok`); the watch does not yet let the user choose.
5. **First-party or marketplace seller.** On Amazon these are different
   products in practice — different price, different returns, different
   delivery.

Note that 1, 3 and 4 all fall out of the questions step. Only 2 needs new
extraction work, and it is the one most likely to make a watch *wrong* rather
than merely imprecise.

---

## 4. What this needs that does not exist

Stated plainly, because two of these are bigger than they look.

- **Watch-level conditions.** "Cheapest across shops" cannot be expressed. §3.1.
- **A home country.** There is no profile, no setting, no user record beyond a
  hardcoded `"default"`. Inferring it per request works and is what jobs does;
  a real setting belongs with auth, which the owner already plans.
- **A blocked-render outcome.** So the day Amazon starts refusing is one
  visible signal rather than every Amazon watch degrading separately.
- **Shipping cost as part of the price.** Extraction reads one number today.

---

## 5. Step 1, built 2026-08-04 — and it proves step 2 is not optional

`shared/shops.py` (Ivory, Bug, Amazon), a new `offers` extractor, and a
`product` kind. Deployed and planned live in about four seconds:

    status proposed | kind product | interval 360
    condition: price < 2000 ILS
      [http   ] ILS cheapest=139.0    ivory.co.il
            139  HyperX CloudX Stinger gaming headset
      [browser] USD cheapest=34.99    amazon.com
          34.99  WWE 2K26 - Xbox Series X
      [http   ] ILS cheapest=29.0     bug.co.il
             29  Suicide Squad: K.T.J.L
    cost/mo 0.0671

Three shops, two currencies, Amazon rendered, seven cents a month. **And not
one of the cheapest offers is a console.** The condition `price < 2000 ILS` is
already true at every shop, so this watch would have fired within minutes on a
headset. It was deliberately not confirmed.

That is the strongest argument available for step 2, and it came from running
the thing rather than reasoning about it. **Until `questions.py` is wired to
products, a "cheapest" product watch is a watch on whatever accessory a shop
lists first.** The plan card shows every offer precisely so this is visible
before anyone pays for a schedule.

### The one architectural idea worth keeping

Prefer a **published standard** over a selector. `schema.org/Product` is a
contract shops maintain because Google reads it, so it survives the redesigns
that break a CSS class — and it carries what a selector cannot reach: the
currency, whether the thing is in stock, the offer's own link, and an `sku`,
which is the stable identity deduplication has wanted at every step of this
project.

Only Ivory publishes it, of the four probed. So `offers` tries JSON-LD first
and falls back to a selector, and Bug and Amazon get the worse path — no
currency, no stock, and a price scraped out of card text. Stated rather than
hidden, because the fallback is the one that will break first.

### Currencies are not compared

Each target carries the currency its shop prices in. Ivory quoting ILS and
Amazon quoting USD are two targets with two thresholds, never one number.
Silently comparing them is how a watch reports a bargain that is not one.

## 6. Step 2, built 2026-08-04 — the watch now follows the product

`questions.py` was already being called for products: the Planner builds
questions from whatever the targets verified, and a product's verified items
are its offers. What was missing was the answers *doing* anything.

For jobs an answer is a **preference** handed to the ranker, because tomorrow's
posting does not exist yet. For a product it is a **pin**, because the thing
being watched does exist and the question is which of today's offers it is.
Same machinery, opposite meaning, and saying the wrong one on screen would be
a lie about what happens next — so the plan card's wording branches on
`repeating`.

    ? Looking for the console itself?     [4] Console  [26] Games/accessories
    ? New or refurbished?                 [2] New      [ 2] Refurbished

`questions.chosen_ids()` intersects the answers into an exact set of item ids —
the same arithmetic the plan card does while the user clicks, so what they see
narrow is what gets watched. `confirm` writes the surviving ids onto each
target as `watched_ids`, and the Checker reads the cheapest **pinned** offer
instead of the cheapest thing on the page.

### Two bugs that only running it could find

**Absent and empty `watched_ids` are different.** Empty means the answers were
given and *this shop* had nothing matching; absent means no preference was
stated. Conflating them made bug.co.il and ivory.co.il fall back to the
cheapest thing on their pages — a ILS 29 game — for a console watch. Empty is
now `unavailable`: out of stock, delisted, or not carried here. All legitimate,
and the one thing 8d must never pay a model to repair.

**Amazon's identity is `data-asin`, not its link.** Its result links are
sponsored-click redirects carrying a base64 blob that is fresh every request,
and its plain links embed the result's *position* in the path (`/ref=sr_1_3`).
So the same console at a different position was a different item, and every
pinned product vanished on the very next check — all three targets read
`unavailable` on a watch that had just been confirmed. `data-asin` is now
preferred; `data-uuid` and `data-index`, which Amazon sets on the same
elements, are deliberately excluded because they are per-render.

### Proven live

    ? Looking for the console itself?  -> "console"
    amazon   pinned=4
        218.29  Xbox One S, 1TB Console (Renewed)
        619.99  Xbox Series X - 1TB Digital Edition (Renewed)
        859.99  X-box Series-X-1TB Black (Renewed)
       1289     Xbox Series X - Halo Infinite Limited Edition

    checker: value=218.29 met=True   (condition: price < 900)

The pin survived a **fresh Amazon render**, which is exactly what failed before
the identity fix.

### What is still imprecise, honestly

The model's "console" grouping put an Xbox **One** S in with the Series X, and
every pinned offer above is Renewed. The second question — new or refurbished —
exists to catch that and was simply not answered in this run. So the mechanism
is right and the grouping is approximate: a user who answers both questions
gets what they asked for, and one who answers neither gets today's behaviour,
which is the cheapest thing on the page.

Sharpening the grouping is a prompt problem, not a structural one, and the plan
card showing every pinned offer before confirming is what makes it correctable.

## 7. Step 3, built 2026-08-05 — the watch has one reading

The step was written down as "watch-level conditions". Building it found that
the interesting half of that phrase is *reading*, not *condition*, and the
correction is worth more than the feature.

### The condition is still evaluated per target, on purpose

The obvious implementation — evaluate the condition against the aggregate —
was tried on paper and rejected for two reasons.

**It changes nothing for a threshold.** `min(prices) < X` is true exactly when
some price is under X, and every shop is checked on the same interval. The
watch fires at the same instant either way. That is not an approximation, it is
the same statement, and §3.1's own note conceded as much.

**Where it would differ, it would be worse.** A sibling's price is from *its*
last check, up to an interval old. Firing on a neighbour's older number means
emailing "cheapest ILS 1,890 at Bug" about a price that may already be gone,
when Bug's own tick would confirm or correct it within minutes. A watch about
money should fire on a number it just read.

So the rule is **fire on your own fresh reading, report the whole picture**,
and `shared/across.py` says so at the top so nobody reads the split as an
omission. What the aggregate is actually for turns out to be three things, and
all three were broken:

### 1. Currencies were being compared, despite the doc saying otherwise

§5 states that ILS and USD are separate targets with separate thresholds. They
were not. A watch has **one** condition, `condition["currency"]` was a label
copied off the first shop that answered, and the threshold was applied to every
target regardless — so `price < 2000` (shekels) was true of Amazon's **$34.99**
and the watch would have fired on it.

Fixed at the Planner: a shop pricing in another currency is **refused**, with
the reason recorded on the watch and shown on the plan card. No exchange rate
is introduced — a rate is a second thing to be wrong about, silently, in an
email about money. The owner's ruling on 2026-08-05: the real answer is the
user's own country and currency, which arrives with auth; until then, compare
within the currency and say what was dropped.

### 2. The baseline came from whichever shop loaded first

"10% cheaper than now" was measured against `baselines[0]`. With three shops
that is arbitrary, and the other two were then judged against a threshold
derived from a shop they have nothing to do with.

It is now the **best** verified reading: the cheapest for a `<` watch, the
dearest for a `>` watch. Measuring at the same end of the range the condition
is judged at is the whole rule, and it is conservative in both directions — the
threshold moves further away, so the watch fires later, never sooner.

### 3. The baseline described the wrong object entirely

The one that would have cost real money. The Planner takes the baseline before
the questions are answered, so for "xbox series x" it is the cheapest thing any
shop lists — **a ILS 139 headset** — and the product is pinned down afterwards,
at confirm. A watch for "10% cheaper" was therefore carrying `price < 125.1`
while following a ILS 1,899 console: a threshold that can never be crossed, on
a watch that looks perfectly healthy.

`confirm` now re-derives it from the pinned offers.
`resolve_relative_condition` moved from `planner/plan.py` to
`shared/condition.py` for this: the arithmetic has to be repeatable from a
different starting price, which means it cannot live inside the Planner's
one-way flow.

### What the email finally says

    Across every shop being watched:
      1899 ILS  ivory  (read just now)
        https://www.ivory.co.il/...
      2400 ILS  bug    (read 45 min ago)
       900 ILS  zap    (read 10 h ago)  -- not confirmed recently

Three rules in that block. A shop that has gone quiet is **shown and flagged,
never dropped** — showing two shops to someone who asked about three is the
silent omission this project keeps removing — and it can never be the best
reading, because a cheap price nobody has confirmed for hours is a less certain
answer, not a better one. **When each price was read is part of the price.**
And the summary is built **only when the watch speaks**, not on every tick:
same rule as ranking, paid per notification.

### One event, however many shops cross

Every target of a watch runs on the same interval and its schedules are created
in the same second, so EventBridge fires them together. Two shops crossing the
same threshold both flipped the watch to `triggered` and both published — two
emails about one event. The transition is now a conditional write and the loser
of the race stays quiet. Latent since Phase 3 and unreachable until a watch had
more than one target that could fire.

### Also in this step

- **`planner/handler.py` had no tests at all.** Everything under `planner/` was
  tested through `plan.py` and the kinds, each with its collaborators injected
  — the exact shape that hid both live bugs of 2026-08-02. The currency gate
  and the baseline choice are in the handler and nowhere else, so
  `planner/test_planner_handler.py` drives the Lambda itself.
- **The Checker's IAM role could not `Query`.** It had `GetItem` and
  `UpdateItem` only, and a GSI query needs the index ARN as well as the
  table's. Granted before the code that needs it shipped, and the read is
  wrapped so a missing permission degrades to an email without the summary —
  the Phase 5 lesson, where an `AccessDenied` after the email had been sent
  cost a real person three duplicate copies.

## 8. Recommended order

1. ✅ **Shops registry + Amazon on our own browser.** Done — §5.
2. ✅ **Particularization via the questions step.** Done — §6.
3. ✅ **Watch-level readings.** Done — §7.
4. **Landed price, and the blocked-render outcome.** Next, and the one
   remaining thing that makes a product watch *wrong* rather than imprecise:
   an item ILS 50 cheaper with ILS 60 shipping is not cheaper, and the
   cross-shop summary §7 just built compares sticker prices.

1 and 2 together made a product watch honest. 3 made it explain itself.

### Still not expressible, and deliberately so

**"Just tell me the cheapest, every morning."** There is no threshold in that
sentence, so it is not a condition watch at all — it is a scheduled report, and
the trigger it needs is Phase 9 step 3b, the same one calendar reminders need.
`readings` is the payload it will use when that exists. Building a second
firing mechanism here to serve one phrasing would have been the expensive way
to get there.
