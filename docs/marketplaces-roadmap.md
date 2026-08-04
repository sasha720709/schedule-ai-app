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

## 6. Recommended order

1. ✅ **Shops registry + Amazon on our own browser.** Done — §5.
2. **Particularization via the questions step.** Reuses what exists; turns
   "the Xbox" into a specific thing before any money is spent. **No longer
   optional**: §5 shows a watch that would fire on a headset.
3. **Watch-level conditions** — the real engineering, and what makes "cheapest
   of five shops" expressible.
4. Landed price, and the blocked-render outcome.

1 and 2 together would make a product watch honest. 3 is what makes it good.
