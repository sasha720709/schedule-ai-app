"""The `product` kind: a thing for sale, across the shops that sell it.

## Why this is a kind rather than a better `value` watch

A `value` watch watches **one page**, chosen by a web search. For a product
that is the wrong unit twice over: it depends on which page the search happened
to pick, and it cannot answer the question people actually ask, which is *where
is this cheapest*.

So the shops come from `shared/shops.py` and the watch gets one target per
shop. The condition then does what it looks like it does: each shop is checked,
and the watch fires when any of them crosses the threshold -- which is
"cheapest across shops" for a threshold, without the Checker needing to
evaluate anything across targets. That larger change is still needed for
"tell me the cheapest", and is deliberately not attempted here.

## The thing this kind cannot do on its own

Measured on real search pages the day this was written, the cheapest result
for `xbox series x` was a headset at ILS 139, a game at ILS 29, and WWE 2K26 at
$34.99. **Not one of them is a console.**

That is the whole particularization problem in three lines. This kind finds the
offers honestly; picking between them is `questions.py`'s job, and until that
is wired to products a "cheapest" watch is a watch on whatever accessory a shop
lists first. The plan card shows every offer it found precisely so this is
visible before anyone pays for a schedule.

## Prices in different currencies are not compared

Each target carries the currency its shop prices in. An Israeli shop quoting
ILS and Amazon quoting USD are two separate targets with two separate
thresholds -- not one number. Silently comparing them is how a watch reports a
bargain that is not one.
"""

import shops
from extract import extract
from fetch import fetch_raw

import llm
from kinds.base import Kind

PRODUCT_PROMPT = """You plan a watch on the price of something for sale. The
shops are already decided -- you are NOT choosing where to look and you have no
web search.

Turn the request into a shop search, and say when to tell them.

QUERY. What someone would type into a shop's search box. The product, in the
fewest words that still identify it, including the model or capacity when the
request gives one.

  "tell me when the Xbox Series X drops below 2000 shekels" -> "xbox series x"
  "watch the price of AirPods Pro 2"                        -> "airpods pro 2"
  "when do new Nike Air Max 90 appear"                      -> "nike air max 90"

Do NOT put the shop, the city or the price in the query -- they have their own
fields, and a shop's search box does not understand them.

COUNTRY. The ISO two-letter code for where they are buying: IL, US, DE, GB.
Null if there is no clue at all.

CONDITION. `op` is one of: <  <=  >  >=  ==  !=
The metric is "price". The value is what they said, as a number, in whatever
currency they said it in.

RELATIVE CONDITIONS. You do not know today's price and any figure you half
remember is stale. **Never invent an absolute threshold for a request phrased
relative to now.** "gets cheaper", "drops 10%", "goes below the current price"
are all relative: put the change in `relative_change_pct` and leave
`condition.value` null. The threshold is computed later from the price actually
read off a shop page.

  "tell me when it gets cheaper"      -> relative_change_pct: 0
  "when it drops 15%"                 -> relative_change_pct: -15
  "when it drops below 2000"          -> relative_change_pct: null, value: 2000

INTERVAL. Minutes. Shop prices move in hours, not seconds; 60-360 is sensible.
Use 60 if the request sounds urgent, 1440 for a long watch on an expensive
thing.

Respond with ONLY a JSON object:
{
  "query": string,
  "country": string | null,
  "relative_change_pct": number | null,
  "condition": {"metric": "price", "op": string, "value": number | null,
                "currency": string | null},
  "check_interval_min": integer
}"""


class ProductKind(Kind):
    name = "product"

    # The extractor is the registry's, so there is nothing for 8d to repair:
    # if a shop reshapes, the fix is one line in `shops.py` for every watch at
    # once. Same argument as `quote` and `jobs`.
    self_heals = False

    # A threshold crossed is an event, and answering it finishes the job --
    # unlike a job search, which is a stream. "Tell me when new listings
    # appear" is the `presence` shape, not this one.
    repeating = False

    def plan(self, request: str, symbol=None, *, hints=None, client=None) -> dict:
        result = llm.ask(
            client,
            model=llm.READ_MODEL,
            max_tokens=llm.READ_MAX_TOKENS,
            system=PRODUCT_PROMPT,
            content=request,
        )
        query = (result.get("query") or "").strip()
        if not query:
            raise ValueError(
                f"could not work out what product to watch in {request!r}"
            )

        interval = int(result.get("check_interval_min") or 180)
        return {
            "condition": result.get("condition") or {},
            "relative_change_pct": result.get("relative_change_pct"),
            "check_interval_min": max(1, min(interval, 1440)),
            "targets": shops.targets_for(query, result.get("country")),
        }

    def resolve(self, target: dict, condition: dict, *,
                fetch_http=None, fetch_browser=None, client=None) -> dict:
        """Prove the shop answers, before the plan is offered.

        A shop returning **no priced offer** is refused rather than stored.
        Unlike a `presence` watch, where zero is the normal state of something
        not yet posted, a shop with nothing for sale under this search means
        the query is wrong or the page has been reshaped -- and a watch built
        on it could never fire.
        """
        url = target["url"]
        raw = (fetch_browser() if target["fetch_method"] == "browser"
               else fetch_raw(url))
        outcome = extract(target["extractor"], raw)
        if not outcome.ok:
            raise ValueError(
                f"{target.get('shop', 'shop')} listed nothing for this search: "
                f"{outcome.error or outcome.status}"
            )

        return {
            "url": url,
            "extract_hint": target["extract_hint"],
            "fetch_method": target["fetch_method"],
            "window": self.window,
            "extractor": target["extractor"],
            "verified_value": outcome.value,
            "verified_raw": outcome.raw,
            # Every offer found, so the plan card can show that the cheapest
            # "xbox series x" is a headset before anyone pays for a schedule.
            "verified_items": outcome.items,
            "unfiltered_count": len(outcome.items),
            "currency": target.get("currency") or "",
            # Stored so the email can say "Ivory" rather than a hostname. The
            # host is a workable fallback and `across.py` still derives one for
            # rows written before this, but a registry that knows the shop's
            # name should not make the reader parse a URL.
            "shop": target.get("shop") or "",
            "literal": outcome.raw,
            "why": (f"{target.get('shop', 'known shop')}; {len(outcome.items)} "
                    f"offers, cheapest {outcome.value}"),
        }
