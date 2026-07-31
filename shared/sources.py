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
"""

import re

# Uppercase tickers, plus the punctuation CNBC's own symbols use:
# @CL.1 (futures), .DJI (indices), BRK.A (share classes), EUR= (FX).
_SYMBOL = re.compile(r"^[A-Z0-9@.\-=]{1,12}$")

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
