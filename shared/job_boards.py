"""Where to look for a job. A registry, for the same reason `sources.py` is one.

## The problem this solves

Asked "tell me when a student cloud engineer vacancy appears in Beer Sheva",
the searching Planner chose `careers.wix.com` -- a cookie wall that defeats
Chromium as well as a plain GET -- and LinkedIn's ordinary search page, which
it could not compile against. **The request the `presence` kind was built for
could not be planned at all.**

That is the identical failure `shared/sources.py` was written to end. Asked
four times for the Apple share price, the Planner web-searched four times and
picked four different sites, two of which block datacenter IPs. The answer was
not a better prompt; it was to stop asking a question whose answer never
changes.

*Where do you look up jobs?* is that kind of question. There is a small set of
right answers per country, they are the same for every request, and the only
genuinely per-request work is turning "student cloud engineer in Beer Sheva"
into keywords and a location.

## Why this is better than a blacklist

The obvious fix for LinkedIn was to blacklist it -- teach the search prompt to
avoid the sites that do not render. That treats the symptom. The searching
Planner would still pick a different unusable board next week, and every
avoided site has to be discovered by a user first.

A registry inverts it: instead of an ever-growing list of what does not work,
a short list of what does, each one verified against the live endpoint before
it was written down.

## LinkedIn, without an API key

`/jobs-guest/jobs/api/seeMoreJobPostings/search` is the endpoint LinkedIn's own
public job pages call to fill their result list. No account, no key, no OAuth,
no Chromium -- it returns ten server-rendered `<li>` cards as plain HTML.
Verified on 2026-08-04 from both a Codespace and a real Lambda:

    cloud engineer @ Beer Sheva, Israel     -> 10 cards, all Be'er Sheva
    student        @ Beer Sheva, Israel     ->  5 cards
    python developer @ New York, US         -> 10 cards, all New York, NY
    devops         @ United States          -> 10 cards

So it costs exactly what any other HTTP check costs -- $0.0000041, about
$0.012/month at fifteen-minute intervals -- and needs no paid scraping API.
**The price does not go up. It goes down**, because a jobs request no longer
pays for Sonnet-with-web-search at plan time.

### Two things about it that are not obvious

**The links are not stable.** Every response carries a fresh `refId` and
`trackingId`, so an identity built from the raw URL changes on every fetch --
which would have made a repeating watch re-report every job, every tick,
forever. `extract.py` keys on `data-entity-urn` instead. Nothing offline could
have caught this; it only appears when the same page is fetched twice.

**The result set alternates.** Five consecutive fetches of one query returned
two different pages of ten -- nineteen distinct jobs in total, flipping back
and forth, almost certainly two load-balanced shards with different index
states. This is harmless *because* of deduplication: each posting is reported
once and then remembered, so the flapping settles after the first few checks
instead of producing an email every other tick.

## Adding a board

Verify it first, from a Lambda if the site is at all likely to care about
IP ranges. It must return listings in the HTML of a plain GET -- no rendering
-- and it must have something per item that survives a second fetch.
"""

import urllib.parse

# Where a request is coming from decides which boards can answer it. The
# country is resolved by the classifier from what the user wrote, not from
# their IP -- someone in Israel may well be looking for work in Berlin.
WORLDWIDE = "*"


class Board:
    """One place to look, with the extractor already written."""

    def __init__(self, name, *, countries, url, selector, describe,
                 params=None):
        self.name = name
        self.countries = countries
        self.url = url
        self.selector = selector
        self.describe = describe
        self.params = params or {}

    def expand(self, keywords: str, location: str) -> dict:
        """A complete, canned target for this search."""
        query = {k: v.format(keywords=keywords, location=location)
                 for k, v in self.params.items()}
        url = self.url.format(
            keywords=urllib.parse.quote(keywords),
            location=urllib.parse.quote(location),
        )
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return {
            "url": url,
            "fetch_method": "http",
            "extractor": {
                "kind": "count",
                "selector": self.selector,
                "parse": "int",
            },
            "extract_hint": (
                f"job listings on {self.describe} for {keywords!r}"
                + (f" in {location}" if location else "")
                + f"; each result is an element matching {self.selector!r}"
            ),
        }


LINKEDIN = Board(
    "linkedin",
    countries=(WORLDWIDE,),
    # The endpoint LinkedIn's own public job pages call to fill their list.
    url="https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
    params={"keywords": "{keywords}", "location": "{location}", "start": "0"},
    # `data-entity-urn` lives on this div, which is what makes deduplication
    # survive LinkedIn rewriting every link on every request.
    selector="li div.job-search-card",
    describe="LinkedIn",
)

DRUSHIM = Board(
    "drushim",
    countries=("IL",),
    # Hebrew-language listings LinkedIn does not always carry. Server-rendered:
    # 25 vacancies in the HTML of a plain GET, verified 2026-08-04.
    url="https://www.drushim.co.il/jobs/search/{keywords}/",
    selector=".jobList_vacancy",
    describe="Drushim (Hebrew listings)",
)

BOARDS = {b.name: b for b in (LINKEDIN, DRUSHIM)}


def get(name):
    return BOARDS.get(name or "")


def for_country(country) -> list:
    """Boards that can answer for this country, general ones first.

    An unknown country is not an error -- LinkedIn answers worldwide, so the
    worst case is one board instead of two. Refusing to plan would be far
    worse than planning narrowly.
    """
    code = (country or "").strip().upper()
    general = [b for b in BOARDS.values() if WORLDWIDE in b.countries]
    local = [b for b in BOARDS.values()
             if code and code in b.countries]
    return general + local


def targets_for(keywords: str, location: str, country=None) -> list:
    """Every canned target for this search. Never empty."""
    keywords = (keywords or "").strip()
    if not keywords:
        raise ValueError("a job search needs something to search for")
    return [
        {**board.expand(keywords, (location or "").strip()), "board": board.name}
        for board in for_country(country)
    ]
