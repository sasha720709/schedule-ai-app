"""Where a thing can be bought. A registry, like `sources.py` and `job_boards.py`.

## What is different here

The other two registries exist because *the source* was the problem. For
products it is not: a web search finds shops perfectly well. Two other things
are hard, and both were named by the owner.

**Which product.** "Tell me when the Xbox gets cheaper" does not identify
anything. Measured on real search pages today, the cheapest result for
`xbox series x` was:

    ivory.co.il   ILS   139   a HyperX gaming headset
    bug.co.il     ILS    29   a Suicide Squad game
    amazon.com    USD 34.99   WWE 2K26

Not one of them is a console. **A price watch on the wrong variant is worse
than no watch** -- it is confidently wrong, on a schedule, about money. This
registry finds the offers; `questions.py` is what picks between them, and
without that step a "cheapest" watch is meaningless.

**Where you can actually buy it.** A price on amazon.com says nothing about the
shop two streets away, and the system has no idea where the user is. Shops are
keyed by country here, exactly as job boards are.

## Prefer the standard, fall back to a selector

`schema.org/Product` is a published contract: shops emit it so Google can read
them, so they keep it working, and it survives the redesigns that break a CSS
class. It also carries what a price watch needs and a selector cannot reach --
the currency, whether the thing is in stock, the link to that specific offer,
and an `sku`, which is a stable identity of the kind deduplication has wanted
at every step of this project.

Verified 2026-08-04, over plain HTTP from a datacenter IP:

    ivory.co.il   JSON-LD ItemList, 16 priced Products, ILS, stock, sku
    bug.co.il     no product JSON-LD -- selector fallback, 25 offers
    amazon.com    no product JSON-LD -- selector fallback, browser only
    zap.co.il     no product JSON-LD, prices arrive after render (see below)

## On Amazon

Phase 6 recorded Amazon as out of reach after Chromium was interrupted. **Six
consecutive Fetcher renders today returned 16-22 product cards with real prices
and no captcha**, so it is in. It costs the browser price, $0.000186 a check --
against $0.00735 per request through ScraperAPI on top of a $49/month floor,
which is ten times the entire per-watch budget. Amazon's own Product
Advertising API is deprecated since 15 May 2026 and closed to new customers.

The blocking is probabilistic, so this may stop working. That is an argument
for noticing it in one place, not for paying $49 a month against the
possibility.

## Not here yet

`zap.co.il` aggregates every Israeli retailer, which makes it the single
highest-leverage target for "which shops near me have it and at what price" --
the same shape LinkedIn's guest endpoint gave for jobs. Its prices load after
render: one browser render captured 27 shop rows and one price. Promising,
unproven, and deliberately left out rather than half-added.
"""

import urllib.parse

WORLDWIDE = "*"


class Shop:
    """One place to look, with the extractor already written."""

    def __init__(self, name, *, countries, url, describe, currency=None,
                 selector=None, fetch_method="http"):
        self.name = name
        self.countries = countries
        self.url = url
        self.describe = describe
        self.currency = currency
        self.selector = selector
        self.fetch_method = fetch_method

    def expand(self, query: str) -> dict:
        extractor = {"kind": "offers", "parse": "float"}
        # Absent for a shop that publishes schema.org: the standard is tried
        # first regardless, and a selector is only ever the fallback.
        if self.selector:
            extractor["selector"] = self.selector

        return {
            "url": self.url.format(query=urllib.parse.quote(query)),
            "fetch_method": self.fetch_method,
            "extractor": extractor,
            "extract_hint": (
                f"offers for {query!r} on {self.describe}; the value is the "
                f"cheapest one listed"
            ),
        }


IVORY = Shop(
    "ivory",
    countries=("IL",),
    url="https://www.ivory.co.il/catalog.php?act=cat&q={query}",
    describe="Ivory (Israel)",
    currency="ILS",
    # No selector: it publishes a JSON-LD ItemList with price, currency,
    # availability and sku, which is strictly better than anything a CSS
    # selector could reach.
)

BUG = Shop(
    "bug",
    countries=("IL",),
    url="https://www.bug.co.il/search?q={query}",
    describe="Bug (Israel)",
    currency="ILS",
    selector=".bordered-product.product-cube-inner-1",
)

AMAZON = Shop(
    "amazon",
    countries=(WORLDWIDE,),
    url="https://www.amazon.com/s?k={query}",
    describe="Amazon",
    currency="USD",
    selector='[data-component-type="s-search-result"]',
    # The one shop here that needs rendering. 45x an HTTP check, and still
    # $0.13/month hourly -- see the module docstring on why no paid API.
    fetch_method="browser",
)

SHOPS = {s.name: s for s in (IVORY, BUG, AMAZON)}


def get(name):
    return SHOPS.get(name or "")


def for_country(country) -> list:
    """Shops that can answer for this country, general ones first.

    An unknown country is not an error: Amazon answers everywhere, so the worst
    case is one shop instead of three. Refusing to plan would be worse.
    """
    code = (country or "").strip().upper()
    general = [s for s in SHOPS.values() if WORLDWIDE in s.countries]
    local = [s for s in SHOPS.values() if code and code in s.countries]
    return general + local


def targets_for(query: str, country=None) -> list:
    """Every canned target for this product search. Never empty."""
    query = (query or "").strip()
    if not query:
        raise ValueError("a product search needs something to search for")
    return [
        {**shop.expand(query), "shop": shop.name, "currency": shop.currency}
        for shop in for_country(country)
    ]
