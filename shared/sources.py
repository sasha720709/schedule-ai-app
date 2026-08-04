"""Known sources: facts that should never be re-discovered by a web search.

## The problem this solves

Asked for the Apple share price four times, the Planner web-searched four
times and picked four different sites: CNN (where it compiled an extractor
reading "Last closed at" -- a figure that moves once a day -- for a watch
checking every minute), stockprices.dev (Cloudflare wall), stooq.com (404 from
a datacenter IP), and CNN again via a different page. Every one of those runs
paid Sonnet to re-answer a question whose answer never changes: *where do you
look up a stock quote?*

That is the wrong shape of intelligence. A sneaker restock or a local
electronics price genuinely needs judgement -- which shop, which region, which
page -- and the searching Planner is right for it. A market quote does not.
There is one correct kind of source (a quote API), it is the same for every
ticker, and the only per-request work is resolving "Apple" to AAPL.

So market quotes are **deliberately stupid**: the model routes the request
here (it still resolves company name to symbol -- that much is real language
work), and everything else is a lookup in this file. Same request, same
source, same extractor, every time.

## Why CNBC's quote API

Chosen by testing, not by preference. The phase-8 design doc's own example
(Yahoo's chart API) returns 429 from datacenter IPs now, and stooq.com --
which the Planner picked on its own once -- 404s the same way. CNBC's partner
quote endpoint answered from this environment without a key, covers stocks,
indices and futures, and returns JSON our deliberately-minimal jsonpath can
read (dotted keys and indices only, no filters).

The known caveat, stated rather than hidden: `last` is the regular-session
price. Outside market hours it holds the previous close while the live
pre/post-market number sits in `ExtendedMktQuote.last`, which cannot be
selected conditionally without a jsonpath filter engine this project refuses
to grow. A watch on a quote therefore moves during trading hours and rests
outside them -- which is also how most people talk about "the price".

## Non-US listings (added 2026-08-04, probed rather than assumed)

A bare ticker resolves to the **US** listing, and for a foreign company that
means the ADR -- a different security, on a different exchange, in a different
currency. The country suffix selects the home listing, and every one of these
was confirmed by asking a real Lambda:

    LUMI-IL   7,377.00  ILS  Tel Aviv Stock Exchange   Bank Leumi
    NICE-IL  30,720.00  ILS  Tel Aviv Stock Exchange   NICE Ltd
    SAP-DE      167.38  EUR  XETRA                     SAP SE
    VOD-GB      116.90  GBp  London Stock Exchange     Vodafone
    SAP         193.50  USD  NYSE                      SAP SE  <- the ADR

`.TA` works as an alias for `-IL`. **The Bolsa Mexicana is not covered at
all**: `WALMEX-MX` and `AMX-MX` both come back empty, which is what produced
the `no key 'last'` failure on 2026-08-03. `NotCovered` now says so in words.

**Minor units are a live trap, and deliberately not converted.** Tel Aviv
quotes in agorot and London in pence, so Bank Leumi reads 7,377 rather than
73.77 shekels. That is what TASE itself displays, so rewriting it would make
our number disagree with every Israeli source the user might check against.
A *relative* condition -- "5% down", which is the request people actually make
-- is unaffected, because a percentage has no units. An *absolute* one ("below
70 shekels") would compare against 7,377 and fire instantly. The currency is
therefore stored and shown; converting it is not attempted.
"""

import json
import re

# Uppercase tickers, plus the punctuation CNBC's own symbols use:
# @CL.1 (futures), .DJI (indices), BRK.A (share classes), EUR= (FX),
# LUMI-IL and SAP-DE (the country suffix for a non-US listing).
_SYMBOL = re.compile(r"^[A-Z0-9@.\-=]{1,12}$")

# Exchange strings exactly as CNBC returns them, mapped to the trading window
# a watch on that exchange should run in (see `shared/schedules.py`).
#
# Verified from a Lambda on 2026-08-04 by asking for real symbols, not read
# from documentation: LUMI-IL and NICE-IL come back "Tel Aviv Stock Exchange"
# in ILS, SAP-DE "XETRA" in EUR, VOD-GB "London Stock Exchange" in GBp.
#
# An exchange missing from this table degrades to a continuous schedule rather
# than raising. Checking a shut market too often is a small cost problem;
# refusing to plan the watch is a broken product, and this table cannot
# possibly be complete.
EXCHANGE_WINDOWS = {
    "NYSE": "us_market_hours",
    "NASDAQ": "us_market_hours",
    "NYSE ARCA": "us_market_hours",
    "NYSE AMERICAN": "us_market_hours",
    "AMEX": "us_market_hours",
    "BATS": "us_market_hours",
    "TEL AVIV STOCK EXCHANGE": "tase_hours",
    "XETRA": "xetra_hours",
    "LONDON STOCK EXCHANGE": "lse_hours",
}

_CNBC_URL = (
    "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
    "?symbols={symbol}&requestMethod=itv&noform=1&partnerId=2&fund=1"
    "&exthrs=1&output=json"
)

KINDS = ("stock_quote",)


def expand(kind: str, symbol: str) -> dict:
    """Turn a routing decision into a complete, canned target.

    Raises ValueError on anything malformed -- the symbol arrives from a model
    and gets spliced into a URL, so the character set is a hard gate, not a
    formality.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown known_source kind {kind!r}")

    symbol = (symbol or "").strip().upper()
    if not _SYMBOL.match(symbol):
        raise ValueError(f"not a plausible market symbol: {symbol!r}")

    return {
        "url": _CNBC_URL.format(symbol=symbol),
        "fetch_method": "http",
        "extractor": {
            "kind": "jsonpath",
            "path": "$.FormattedQuoteResult.FormattedQuote[0].last",
            "parse": "float",
        },
        # The repair instruction, should CNBC ever reshape the payload.
        "extract_hint": (
            f"last traded price of {symbol} from CNBC's quote JSON; the value "
            f"lives in FormattedQuoteResult.FormattedQuote[0].last"
        ),
    }


class NotCovered(ValueError):
    """The source has no quote for this symbol. Not a bug -- a coverage limit.

    Kept distinct from a malformed symbol because the two need opposite
    answers: a bad ticker means the model guessed, and a covered exchange with
    no data means the user asked for a market this source does not carry.
    """


def describe(body: str) -> dict:
    """What instrument did that symbol actually resolve to?

    This is the fix for the worst thing the shares feature did. Asked for AMX
    on the Bolsa Mexicana, CNBC returns **$25.01, NYSE, USD** -- the American
    depositary receipt. Asked for SAP it returns the NYSE ADR too, not
    Frankfurt. Both are real numbers for a *different security* than the one
    requested, and the plan card showed a bare `25.01` with nothing to notice.
    A wrong answer stated confidently is worse than a refusal.

    So the name, exchange and currency come back alongside the price, out of
    the response that was already fetched -- no second request -- and the plan
    card shows them. The user then sees "América Móvil · NYSE · USD" before
    confirming and can say that is not what they meant.
    """
    try:
        quotes = json.loads(body)["FormattedQuoteResult"]["FormattedQuote"]
        quote = quotes[0]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise NotCovered(
            f"could not read a quote out of the response ({type(exc).__name__})"
        ) from None

    # An uncovered symbol comes back as a quote object with the fields simply
    # absent -- WALMEX-MX and AMX-MX both do this, because CNBC does not carry
    # the Bolsa Mexicana at all. Previously that surfaced as
    # "no key 'last' at '.FormattedQuoteResult.FormattedQuote[0].last'",
    # which describes our jsonpath rather than the user's problem.
    if quote.get("last") in (None, ""):
        raise NotCovered(
            f"this source has no quote for {quote.get('symbol') or 'that symbol'}"
            f" -- the exchange is probably not covered. Try the US listing, or "
            f"describe a page to watch instead."
        )

    return {
        "symbol": quote.get("symbol"),
        "name": quote.get("name"),
        "exchange": quote.get("exchange"),
        "currency": quote.get("currencyCode"),
    }


def window_for(exchange) -> str | None:
    """The trading window for an exchange, or None to run continuously."""
    if not isinstance(exchange, str):
        return None
    return EXCHANGE_WINDOWS.get(exchange.strip().upper())
