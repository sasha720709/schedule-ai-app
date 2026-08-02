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

import llm
from kinds.base import Kind

QUOTE_PROMPT = """You plan a watch on a market quote. The source is already
decided -- you are NOT choosing where to look and you have no web search.

You are given a request and the ticker symbol it was resolved to. Produce the
condition and how often to check.

CONDITIONS. `op` must be one of: <  <=  >  >=  ==  !=
Write the metric as a short snake_case name, usually "price".

RELATIVE CONDITIONS. You do not know the current price, and any figure you
half-remember is stale. **Never invent an absolute threshold for a request
phrased relative to now.** "goes down", "drops below current", "falls 5%" are
all relative: put the change in `relative_change_pct` and leave
`condition.value` null. The threshold is computed later from the price
actually read off the wire, which is the only baseline that is real.

  "tell me when it goes down"        -> relative_change_pct: 0
  "tell me when it drops 5%"         -> relative_change_pct: -5
  "tell me when it goes 10% above"   -> relative_change_pct: 10
  "tell me when it drops below $300" -> relative_change_pct: null, value: 300

"Goes down" means ANY decrease. Do not decide on the user's behalf that they
meant a meaningful one and pick a percentage.

INTERVAL. Minutes, between 1 and 59. A quote watch only runs during market
hours, and the schedule is a cron step within the hour, so 60 or more is not
expressible. 1-5 minutes suits "tell me when it moves"; 15-30 suits a
threshold that is far away.

Respond with ONLY a JSON object:
{
  "relative_change_pct": number | null,
  "condition": {"metric": string, "op": string, "value": number | null,
                "currency": string | null},
  "check_interval_min": integer
}"""

# A windowed schedule is a cron step inside the hour, so anything at or above
# 60 would silently fire hourly. Clamped rather than refused: the model picked
# a cadence, not a contract, and refusing a plan over it would be absurd.
MAX_WINDOWED_INTERVAL_MIN = 59


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

    def plan(self, request: str, symbol=None, *, client=None) -> dict:
        """Condition and cadence only. No web search, because there is nothing
        to choose: `sources.py` owns the URL and the extractor, and the symbol
        was already resolved by the classifier.

        This is the saving that made classification worth doing on its own.
        A quote request used to pay for Sonnet *with web search* before anyone
        noticed the answer was a registry lookup.
        """
        from anthropic import Anthropic

        result = llm.ask(
            client or Anthropic(),
            model=llm.READ_MODEL,
            max_tokens=llm.READ_MAX_TOKENS,
            system=QUOTE_PROMPT,
            content=f"symbol: {symbol}\n\n{request}",
        )
        interval = int(result.get("check_interval_min") or 5)
        return {
            "condition": result.get("condition") or {},
            "relative_change_pct": result.get("relative_change_pct"),
            "check_interval_min": min(interval, MAX_WINDOWED_INTERVAL_MIN),
            "targets": [{"known_source": "stock_quote", "symbol": symbol}],
        }

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
