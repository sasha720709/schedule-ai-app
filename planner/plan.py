"""The Planner: turn a plain-English request into a plan that has been tried.

## What changed in Phase 8b

The Planner used to describe a task in English -- `"find the 512GB OLED
price"` -- and a language model re-read that English and re-solved the same
problem on every tick, roughly 14,400 times a month. Now it **compiles a
checker**: a typed extraction spec that a dozen lines of Python execute for
about four millionths of a dollar, forever.

Two consequences that are easy to miss:

- Planning gets deliberately more expensive, and that is fine. It is amortised
  over ~14,000 checks; going from $0.05 to $0.20 is invisible next to what it
  buys downstream.
- **The Planner now opens the pages it proposes.** It never did before. It web
  searched and handed over URLs sight-unseen, which is how Amazon and Best Buy
  became production failures instead of plan-time ones. A plan that cannot be
  verified against the real page is now never offered at all.

## Why three model calls and not one

Compiling a selector needs markup, and the rendered Steam Deck page is 1.49MB
-- roughly 375,000 tokens. Sending that is neither affordable nor necessary.
So the work is split the way a person would do it:

1. **Search.** Sonnet, with web search, proposes targets, a condition and an
   interval. Text only, no markup.
2. **Read.** The page is fetched, and Haiku reads the value out of the *text* --
   cheap, and exactly the job the old Checker did on every tick. This is the
   only step that needs to understand the page's meaning.
3. **Compile.** The literal value from step 2 is located in the markup with
   `str.find`, and only the few hundred characters around it are shown to
   Sonnet, which names a selector. Locating a string is not a job for a model.

Then the spec is *run*. If it does not reproduce the value, it is not stored.

## What changed in Phase 9

This file used to be 634 lines and hold all of the above plus the rules for
every kind of watch. Steps 2-4 now belong to whichever kind is resolving the
target (`kinds/`), and a kind that does not compile anything -- `quote`, whose
URL and extractor come from a registry -- skips them entirely.

What is left here is the request-level pipeline that no kind varies: the web
search, and turning a relative condition into a real threshold once something
has actually been read.

The test of the split is `SEARCH_PROMPT`. It still carries the rules for three
request types at once, and two shipped bugs came from those rules interfering.
It should shrink as kinds move out; step 2b is what shrinks it. If it grows,
this refactor failed.
"""

import json
import sys

from anthropic import Anthropic

import llm
from prompts import SEARCH_PROMPT


def search(request: str, *, client=None) -> dict:
    """Step 1: what should be watched, where, and how often."""
    client = client or Anthropic()
    response = client.messages.create(
        model=llm.PLAN_MODEL,
        max_tokens=llm.PLAN_MAX_TOKENS,
        system=SEARCH_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        messages=[{"role": "user", "content": request}],
    )
    for block in response.content:
        if block.type == "server_tool_use":
            print(f"[searched] {block.input.get('query')}", file=sys.stderr)
    return llm.parse_json(llm.text_of(response))


def resolve_relative_condition(condition: dict, pct, baseline) -> dict:
    """Turn "goes down from current" into a real threshold, using a real reading.

    The condition is written during the search step, before any page has been
    opened. At that moment the model has no current value -- only whatever a
    search result claimed -- so asking it for an absolute threshold on a
    relative request guarantees a fabricated one.

    Observed exactly that: asked "tell me when Apple shares go down from the
    current", it produced `price < 313.93`. That is 5% below $330.45, a figure
    from search results, while the page itself said $333.43. Two inventions in
    one number -- a baseline that was never read, and a 5% drop the user never
    asked for. "Goes down" means any decrease.

    So the threshold is computed here instead, from the value the extractor was
    actually verified against, and the baseline is stored alongside it so the
    plan card can say "5% below the $333.43 read just now" rather than showing
    a bare number nobody can check.
    """
    if pct is None or not isinstance(baseline, (int, float)) or isinstance(baseline, bool):
        return condition

    resolved = dict(condition)
    threshold = round(float(baseline) * (1 + float(pct) / 100), 4)
    resolved["value"] = threshold
    resolved["baseline"] = float(baseline)
    resolved["relative_change_pct"] = float(pct)
    # "goes down" is any decrease, so the threshold IS the baseline and the
    # comparison has to be strict -- `<=` would fire on an unchanged price.
    if not resolved.get("op"):
        resolved["op"] = "<" if float(pct) <= 0 else ">"
    return resolved


def plan(request: str) -> dict:
    """Backwards-compatible entry point: search only, no page access.

    Kept so `python plan.py "..."` still works offline and so the api Lambda's
    tests have something cheap to call. The Lambda handler drives the full
    fetch-and-verify flow itself, because only it can reach the browser Fetcher.
    """
    return search(request)


if __name__ == "__main__":
    request = " ".join(sys.argv[1:]) or "tell me when the PS5 Slim drops under $400"
    print(json.dumps(plan(request), indent=2))
