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

# Re-exported. It moved to `shared/condition.py` on 2026-08-05 because `confirm`
# has to run the same arithmetic again once the answers pin down which offer the
# watch is actually about -- see the note in its docstring. Importers here are
# left pointing at `plan` so the move stayed a move rather than a rename.
from condition import resolve_relative_condition  # noqa: F401


def search(request: str, *, shape: str = "value", client=None) -> dict:
    """Step 1: what should be watched, where, and how often.

    `shape` is decided beforehand, by `classify`, and handed in rather than
    asked for. It used to be one of three request-type judgements crammed into
    this one prompt, and two shipped bugs came from those judgements
    interfering -- a presence watch that could not be planned at all, and a
    threshold invented from search results.
    """
    client = client or Anthropic()
    response = client.messages.create(
        model=llm.PLAN_MODEL,
        max_tokens=llm.PLAN_MAX_TOKENS,
        system=SEARCH_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        messages=[{"role": "user",
                   "content": f"watch_shape: {shape}\n\n{request}"}],
    )
    for block in response.content:
        if block.type == "server_tool_use":
            print(f"[searched] {block.input.get('query')}", file=sys.stderr)
    return llm.parse_json(llm.text_of(response))


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
