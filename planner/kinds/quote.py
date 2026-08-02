"""The `quote` kind: a market price, from the one right place to look.

## Why this is deliberately stupid

Asked for the Apple share price four times, the searching Planner web-searched
four times and picked four different sites -- one of them CNN, where it
compiled an extractor reading "Last closed at", a figure that moves once a day,
for a watch checking every minute. Two of the others block datacenter IPs.

That is the wrong shape of intelligence. A sneaker restock genuinely needs
judgement about which shop and which region. A market quote does not: there is
one correct kind of source, it is the same for every ticker, and the only
per-request work is resolving "Apple" to AAPL.

So this kind compiles nothing and searches nothing. `shared/sources.py` owns
the URL and the extractor; the model's entire contribution is the symbol.

## What is still verified

Everything that can be. The canned spec is run against the live endpoint before
the plan is offered, exactly as a compiled one is. A registry entry that has
gone stale -- CNBC reshaping its payload -- fails at plan time, in front of the
user, rather than at 3am three weeks later.

## Why it never self-heals

8d's repair exists because a *site we do not control* can be redesigned under a
compiled extractor. Here the extractor is ours and the endpoint is a documented
JSON shape. If it breaks, the fix is one line in `sources.py` for every watch at
once -- paying Haiku to rediscover it per watch would be both slower and wrong.
"""

import sources
from extract import extract
from fetch import fetch_raw

from kinds.base import Kind


class QuoteKind(Kind):
    name = "quote"

    # The registry is the extractor, so there is nothing for 8d to repair.
    # Read by the Checker's degrade path; see the module docstring.
    self_heals = False

    # A market that is shut cannot produce a new price. Confining the schedule
    # is mostly about not making 35,010 pointless requests a month to a free
    # third-party endpoint we do not own and cannot afford to lose -- see the
    # honest accounting in shared/schedules.py.
    window = "us_market_hours"

    def resolve(self, target: dict, condition: dict, *,
                fetch_http=None, fetch_browser=None, client=None) -> dict:
        """Expand the symbol into a canned target and prove it against the wire.

        `fetch_http` and `fetch_browser` are accepted and ignored. A quote's URL
        is not known until the registry has expanded the symbol, so the caller
        cannot have bound a fetcher to it -- and there is no page to render,
        which is the whole point of the kind.
        """
        expanded = sources.expand(
            target.get("known_source", "stock_quote"), target.get("symbol", ""))

        url = expanded["url"]
        outcome = extract(expanded["extractor"], fetch_raw(url))
        if not outcome.ok:
            raise ValueError(
                f"canned extractor gave {outcome.status}"
                f"{': ' + outcome.error if outcome.error else ''}"
            )

        return {
            "url": url,
            "extract_hint": expanded["extract_hint"],
            "fetch_method": expanded["fetch_method"],
            "window": self.window,
            "extractor": expanded["extractor"],
            "verified_value": outcome.value,
            "verified_raw": outcome.raw,
            "literal": outcome.raw,
            "why": "known source; nothing searched, nothing compiled",
        }
