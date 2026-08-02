"""Decide which kind of watch a request is, before anything expensive happens.

## Why this exists at all

Until now the answer lived inside `SEARCH_PROMPT`, as paragraphs: one telling
the model not to web-search market quotes, one distinguishing "waiting for a
number" from "waiting for a thing to appear". Every kind added a paragraph, and
two shipped bugs were those paragraphs interfering with each other. Splitting
them out is the whole point of Phase 9; this file is where the decision goes
instead.

It also saves the expensive call outright. A quote request used to pay for
Sonnet **with web search** before anyone noticed the answer was a registry
lookup. Classified first, it never searches at all.

## An honest note on "rules first, model second"

The plan doc says classification should be rules-first. In practice there is no
honest lexical rule that separates "how much is Apple" (a quote) from "how much
is an Apple pencil at Best Buy" (a product): both name a company, and the
distinguishing signal is meaning. A keyword heuristic here would fight the
model and lose.

So the rules are **gates around the answer**, not a substitute for it:

- The kind must exist in the registry, or it is ignored.
- A `quote` must come with a symbol the registry will actually accept, or it is
  ignored -- a hallucinated ticker must not become a URL.
- Anything ignored falls back to `value`, the searching path, which is what
  every request did before this file existed.

That last one is the important one. **A misclassification must cost a
suboptimal plan, never a rejected request.** The failure mode of a confident
classifier is worse than the failure mode of a vague one.

The chosen kind is stored on the watch and shown on the plan card, so a wrong
fork is visible before the user confirms rather than discovered weeks later.
"""

import sys

import sources

import llm

CLASSIFY_PROMPT = """You sort a monitoring request into exactly one kind.

  quote     -- an exchange-traded market price: a share, an index, a future,
               an ETF. "Tell me when Apple drops", "watch the S&P".
  presence  -- something that DOES NOT EXIST YET and the user is waiting for
               it to APPEAR. A job posting, a restock, an appointment slot, a
               ticket going on sale.
  value     -- anything else. Something already on a page that the user is
               waiting to CHANGE: a product price, a rating, a countdown.

Rules:

- `quote` is ONLY for instruments traded on an exchange. A product price is
  `value` even when the product is made by a listed company. If the request
  names a shop, a site, a country, or any other "where", it is NOT a quote --
  a quote has one right source and needs no choosing.
- For `quote`, resolve the name to its ticker symbol yourself: "Apple" ->
  "AAPL", "the S&P 500" -> ".SPX". That resolution is the only thing you are
  asked to know. If you are not confident of the symbol, answer "value"
  instead and let the normal search handle it.
- If the request is waiting for something to show up, it is `presence`, even
  if similar things are listed today.
- When genuinely unsure, answer "value". It is the general path and it works
  for everything; a wrong guess here is more expensive than a vague one.

Respond with ONLY a JSON object:
{"kind": "quote" | "presence" | "value", "symbol": string | null}"""


def _acceptable(decision, known: tuple) -> str:
    """Apply the gates. Returns the kind to actually use.

    Every field here came out of a model, so nothing about its type is
    guaranteed -- `{"kind": 7}` is a perfectly possible reply, and this
    function must survive it rather than take planning down. Coerce, do not
    assume.
    """
    if not isinstance(decision, dict):
        return "value"

    kind = decision.get("kind")
    kind = kind.strip().lower() if isinstance(kind, str) else ""
    if kind not in known:
        return "value"

    if kind == "quote":
        # A symbol from a model is about to be spliced into a URL. The registry
        # validates the character set; asking it here means a hallucinated
        # ticker degrades to a web search instead of becoming a request.
        try:
            sources.expand("stock_quote", decision.get("symbol", ""))
        except ValueError as exc:
            print(f"[classify] rejecting quote: {exc}", file=sys.stderr)
            return "value"

    return kind


def classify(request: str, known: tuple, *, client=None) -> dict:
    """Return `{"kind": ..., "symbol": ...}`, never raising.

    `known` is the registry's kind names, passed in rather than imported so
    this module does not depend on the package it is choosing from.

    A classifier that fails -- a malformed reply, a timeout, an outage -- must
    not take planning down with it. The whole request degrades to the path it
    would have taken before this step existed.
    """
    from anthropic import Anthropic

    try:
        decision = llm.ask(
            client or Anthropic(),
            model=llm.READ_MODEL,
            max_tokens=llm.CLASSIFY_MAX_TOKENS,
            system=CLASSIFY_PROMPT,
            content=request,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[classify] failed, falling back to value: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return {"kind": "value", "symbol": None}

    kind = _acceptable(decision, known)
    symbol = decision.get("symbol") if kind == "quote" else None
    symbol = symbol if isinstance(symbol, str) else None
    print(f"[classify] {request[:60]!r} -> {kind}"
          f"{f' ({symbol})' if symbol else ''}", file=sys.stderr)
    return {"kind": kind, "symbol": symbol}
