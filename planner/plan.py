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
every kind of watch. The kind-specific parts -- how to pick an anchor, which
prompt compiles it, what counts as proof -- now live in `kinds/`, one module
each. What is left here is the pipeline that does not vary: search, read,
locate, and the escalation from a plain GET to a browser.

The test of the split is `SEARCH_PROMPT`. It carries the rules for three
request types at once, and two shipped bugs came from those rules interfering.
It should shrink as kinds move out. If it grows, this refactor failed.
"""

import json
import sys

from anthropic import Anthropic

from fetch import to_text, windows_around

import kinds
import llm
from prompts import READ_PROMPT, SEARCH_PROMPT

# The page text handed to the reading model. Longer costs money for no gain --
# the anchor it returns only has to be findable in the markup afterwards.
MAX_READ_CHARS = 20000


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


def read_value(url: str, hint: str, condition: dict, text: str, *, client=None) -> dict:
    """Step 2: what does the page actually say, verbatim?

    Haiku, on text -- the same job the Checker used to do on every tick, now
    done once. Its answer is not the watch's reading; it is the anchor that
    makes step 3 affordable.
    """
    client = client or Anthropic()
    return llm.ask(
        client,
        model=llm.READ_MODEL,
        max_tokens=llm.READ_MAX_TOKENS,
        system=READ_PROMPT,
        content=(
            f"Condition: {json.dumps(condition)}\n"
            f"What to look for: {hint}\n"
            f"URL: {url}\n\nPage text:\n{text}"
        ),
    )


def build_extractor(url: str, hint: str, condition: dict, raw: str, *,
                    shape: str = "value", client=None) -> dict:
    """Steps 2-4 for one target. Returns a verified spec, or raises.

    `shape` selects the kind. The read is shared; everything after it -- which
    string to anchor on, which prompt compiles it, what counts as proof -- is
    the kind's decision, in `kinds/`.
    """
    kind = kinds.get(shape)

    text = to_text(raw)[:MAX_READ_CHARS]
    reading = read_value(url, hint, condition, text, client=client)
    anchor = kind.anchor(reading, url)

    fragments = windows_around(raw, anchor)
    if not fragments:
        # The anchor is in the visible text but not verbatim in the markup --
        # split across tags, or entity-escaped. Give the model the text window
        # instead of nothing; a regex over text may still be compilable.
        fragments = windows_around(text, anchor) or [text[:4000]]

    return kinds.compile_and_verify(
        kind, url, hint, anchor, fragments, raw, client=client)


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


def build_with_cheapest_fetch(url: str, hint: str, condition: dict, *,
                              fetch_http, fetch_browser, shape: str = "value",
                              client=None):
    """Verify against a plain GET first; render only if that cannot be done.

    Returns `(built, fetch_method, note)`.

    ## Why this is mechanical rather than asked for

    A browser check costs $0.000186 against $0.0000041 for a plain GET -- **45
    times more**, and at one-minute intervals that is $8.05/month against
    $0.18. Since the model left the hot path the browser is the single most
    expensive thing left in a check.

    The Planner's prompt already asks the model to prefer `http`. That is a
    request, not a guarantee, and the cost of it guessing wrong is 45x in the
    expensive direction and a rejected target in the cheap one. So the choice
    is settled by trying: if a spec compiles and verifies against the raw HTML,
    the page did not need JavaScript, whatever anyone believed.

    The wasted work when a page genuinely needs rendering is one Haiku read --
    `build_extractor` gives up before compiling when the value is not in the
    text at all, which is exactly the shape of a JS-rendered page. Roughly
    $0.0001, once, against $7.87/month saved every time the guess would have
    been wrong.

    Escalation also runs the other way. A target the model marked `http` that
    turns out to need rendering used to be rejected outright; now it is
    retried in the browser and kept.
    """
    try:
        raw = fetch_http()
    except Exception as exc:  # noqa: BLE001 -- blocked, 403, timeout, TLS
        cheap_error = f"plain GET failed: {type(exc).__name__}: {exc}"
    else:
        try:
            built = build_extractor(url, hint, condition, raw,
                                    shape=shape, client=client)
            return built, "http", "verified against raw HTML; no browser needed"
        except Exception as exc:  # noqa: BLE001
            cheap_error = f"could not verify against raw HTML: {exc}"

    print(f"[escalating to browser] {url}: {cheap_error}", file=sys.stderr)
    raw = fetch_browser()
    built = build_extractor(url, hint, condition, raw, shape=shape, client=client)
    return built, "browser", f"needed rendering -- {cheap_error}"


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
