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
"""

import json
import re
import sys

from anthropic import Anthropic

from extract import SpecError, extract, validate_spec
from fetch import to_text, windows_around

PLAN_MODEL = "claude-sonnet-5"
READ_MODEL = "claude-haiku-4-5-20251001"

# Sonnet 5 runs *adaptive thinking by default* when the `thinking` parameter is
# omitted -- a change from Sonnet 4.6, which ran without it. And `max_tokens` is
# a hard cap on thinking *plus* response text, not just the reply.
#
# The first version of this file asked for a spec with max_tokens=1024. Thinking
# consumed the whole budget, the response came back carrying only a
# ThinkingBlock and no text at all, and the Planner failed with "No text in
# response" -- which reads like a malformed reply rather than a token ceiling.
# These budgets are deliberately generous: planning happens once per watch and
# is amortised over ~14,000 checks, so headroom here is free.
PLAN_MAX_TOKENS = 8192
COMPILE_MAX_TOKENS = 4096
READ_MAX_TOKENS = 1024

SEARCH_PROMPT = """You are the planning stage of a price/availability watcher.

Given a user's plain-English request, use web search to find 1-3 concrete
URLs that are the best sources to monitor, and judge how volatile the
target is (flash sale vs. slow-moving price) to suggest a sensible check
interval in minutes.

WHAT THE CHECKER CAN ACTUALLY DO -- plan within these limits:

- It fetches exactly ONE url per target, once per check. It cannot follow
  links, call a second endpoint, or chain requests. Never write a hint
  like "take the id from this response, then fetch .../item/{id}".
- fetch_method "http" is a plain GET. Choose it when the value is present
  in the raw HTML. It is cheap, so prefer it.
- fetch_method "browser" renders the page in headless Chromium. Choose it
  only when the value is drawn by JavaScript and absent from the raw HTML
  (Steam store pages, many modern storefronts). Since the checking model was
  removed, the browser is now the single most expensive part of a check --
  about eighty times a plain fetch -- so do not use it by default.
- A PLAIN JSON ENDPOINT IS THE BEST POSSIBLE TARGET. If the fact is
  available from an API in one request, use it: it is stable, tiny, and
  hundreds of times cheaper than rendering a page. Look for one first.
- NEITHER method defeats bot protection. amazon.com and bestbuy.com
  actively block automated traffic and will fail every time -- do not pick
  them. Prefer the manufacturer's own site, an official store page, or a
  price-tracking site that serves ordinary HTML.

CONDITIONS. `op` must be one of: <  <=  >  >=  ==  !=
Write the metric as a short snake_case name.

WATCH SHAPE. This is the most important judgement you make, so read it twice.

- "value"    -- the thing being watched is ON THE PAGE RIGHT NOW and the user
                is waiting for it to CHANGE. A price, a rating, a countdown.
- "presence" -- the thing being watched DOES NOT EXIST YET and the user is
                waiting for it to APPEAR. A job posting, a restock, an
                appointment slot, a new release, a ticket going on sale.

"Tell me when a cloud engineer vacancy appears" is `presence`, and its
condition is a count reaching one: {"metric": "matching_vacancies", "op": ">=",
"value": 1}. Do not turn a presence watch into a value watch by inventing a
number that is already on the page. If the user is waiting for something to
show up, it is `presence`, even if similar things are listed today.

RELATIVE CONDITIONS. You do not know the current value. You are writing this
before the page has been opened, so any number you have came from search
results and is stale, approximate, or about a different quote entirely.

**Never invent an absolute threshold for a request phrased relative to now.**
"goes down", "drops below current", "falls 5%", "cheaper than it is today" are
all relative. Put the change in `relative_change_pct` and leave
`condition.value` as null -- the threshold is computed later from the value
actually read off the page, which is the only baseline that is real.

  "tell me when it goes down"        -> relative_change_pct: 0
  "tell me when it drops 5%"         -> relative_change_pct: -5
  "tell me when it goes 10% above"   -> relative_change_pct: 10
  "tell me when it drops below $300" -> relative_change_pct: null, value: 300

A user who says "goes down" means ANY decrease. Do not decide on their behalf
that they meant a meaningful one and pick a percentage.

After you finish searching, respond with ONLY a JSON object, no other
text before or after it, matching this shape:
{
  "watch_shape": "value" | "presence",
  "relative_change_pct": number | null,
  "condition": {"metric": string, "op": string, "value": number | boolean, "currency": string | null},
  "check_interval_min": integer,
  "targets": [
    {"url": string, "extract_hint": string, "fetch_method": "http" | "browser"}
  ]
}
"""

READ_PROMPT = """You read a web page so an extractor can be compiled against it.

YOU ARE READING, NOT JUDGING. You are shown the condition only so you know
WHICH value to find -- "price < 700" tells you to look for a price, not for a
rating. It is NOT a filter on what you may report.

**Never withhold a value because it fails the condition.** A price of $949
against a condition of "under $700" is exactly the situation a watch exists
for: the user is waiting for it to change. Reporting "the price does not meet
the condition" instead of "$949.00" makes the watch impossible to create at
all. Whether the condition holds is decided later, in Python, for free, on
every check. Your only job is to say what the page says right now.

You are given page text, a hint about what to watch, and the condition the
value will later be compared against. Report two things.

Respond with ONLY a JSON object:
{
  "literal": string | null,  // the watched value, EXACTLY as it appears, e.g. "$789.00"
  "sample":  string | null,  // ANY one comparable item on the page, verbatim
  "note": string             // one short sentence
}

`literal` is the thing being watched, if it is on the page right now.

`sample` matters most when `literal` is null, and you should almost always be
able to fill it in. Many watches are for something that has NOT happened yet --
a job that has not been posted, an item that is out of stock, a slot not yet
open. The page still shows *other* items of the same kind, and one of those is
what makes the page's structure readable. On a job board with no matching role,
`sample` is the title of any other job listing. On a sold-out product page, it
is any other product's name.

Both strings must occur in the text character for character -- they are going
to be searched for programmatically. Do not reformat, round, translate, or add
a currency symbol that is not there.

Use null for `sample` only if the page genuinely lists nothing at all."""

COMPILE_PROMPT = """You compile a deterministic extractor for a web page.

You are given fragments of real HTML surrounding a value, and the literal
value itself. Write a spec that will read that value again on every future
check, with no language model involved. It must keep working for months.

Respond with ONLY a JSON object:
{
  "scope": string | null,     // CSS selector narrowing to the region. STRONGLY PREFERRED.
  "kind": "css" | "regex" | "jsonpath" | "count",
  "selector": string,         // for css and count
  "pattern": string,          // for regex, at most ONE capture group
  "path": string,             // for jsonpath, e.g. "$.offers.price"
  "attribute": string | null, // read an attribute instead of the text
  "parse": "currency" | "float" | "int" | "text" | "bool",
  "unavailable_if": {...} | null   // same shape, no scope; matches when there is legitimately no value
}

RULES THAT MATTER:

- `scope` is not decoration. It narrows the search AND acts as a liveness
  test: if the scope stops matching, the page was redesigned and the watch
  reports a fault instead of silently reporting nothing. Without it, "the
  value is missing" and "the page was rebuilt" are indistinguishable.
  A real page contains many products, hidden templates and localised string
  tables -- an unscoped "Out of stock" pattern matches some other item's
  status and reports a healthy watch as unavailable forever.
- PREFER STABLE SELECTORS. Semantic class names, ids, data- attributes,
  itemprop. AVOID build-generated hashed classes like `_2mg-ayeqtfvSlVBeUNudsd`
  or `pk-LoKoNmmPK4GBiC9DR8` -- they change on every deploy of the site.
- Use `currency` for money. It requires a symbol or a two-digit minor unit,
  which is what stops "Steam Deck 512 GB" being read as a price of 512.
  Use `float` only when a bare number really is the value.
- Use `count` when the watch is about something that does not exist yet --
  a vacancy, a restock, a new listing. Zero is a legitimate answer for it,
  where every other kind treats "nothing found" as a problem.
- `jsonpath` supports dotted keys and integer indices only. No wildcards,
  filters or recursive descent.
- A `script[type="application/ld+json"]` scope plus a jsonpath is often the
  most stable option on an HTML page. Look for it."""


COUNT_PROMPT = """You compile a deterministic counter for a list on a web page.

The user is waiting for something to APPEAR that is not there yet -- a job
posting, a restock, an open slot. So you are not shown the thing itself. You
are shown the markup around a DIFFERENT item of the same kind that happens to
be on the page today, because that is what reveals the list's structure.

Your job: write a spec that counts how many items in that list match what the
user is waiting for. Counting zero today is the correct and expected answer.

Respond with ONLY a JSON object:
{
  "scope": string,        // CSS selector for the LIST CONTAINER. Required.
  "kind": "count",
  "selector": string,     // CSS selector matching ONE item, filtered to what the user wants
  "parse": "int"
}

RULES THAT MATTER:

- `scope` must select the container that holds the list of items -- the results
  table, the `<ul>`, the grid. It is REQUIRED here, and it is doing real work:
  it is the liveness test. If the site is redesigned and the container stops
  matching, the watch reports a fault. Without it, a rebuilt page counts zero
  items forever and the user is told "not yet" until the end of time.
- `selector` matches an individual item AND filters it. Use soupsieve's
  `:-soup-contains("...")` to filter by visible text -- for example
  `a.job-title:-soup-contains("Cloud Engineer")`. Prefer `:-soup-contains(...)`
  over `:contains(...)`, which is a deprecated alias.
- Match the way the page is actually written. If the listings are in Hebrew,
  Japanese, or German, filter on the term as it appears on the page, not on
  the user's English phrasing.
- Filter on ONE distinguishing term rather than a whole phrase. A listing
  titled "Junior Cloud Engineer - Student Position" will not match a selector
  looking for the exact string "student job cloud engineer". Pick the term most
  likely to appear verbatim in a matching item's title.
- Do NOT try to encode every criterion the user mentioned. Being slightly loose
  is right: a watch that fires on a near-match wastes one email, while a watch
  too strict to ever fire wastes the whole point of the watch.
- PREFER STABLE SELECTORS. Semantic class names, ids, data- attributes. AVOID
  build-generated hashed classes -- they change on every deploy of the site."""


def _parse_json(raw: str) -> dict:
    """Take the outermost {...} rather than trusting the whole string.

    `plan.py` used to assume the last text block was clean JSON and was
    observed failing in Lambda on an empty block. This is the sturdier parse
    the Checker already used.
    """
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No JSON object in model response: {raw[:200]!r}")
    return json.loads(raw[start:end + 1])


def _text_of(response) -> str:
    blocks = [block.text for block in response.content if block.type == "text"]
    if blocks:
        return blocks[-1]

    # Name the likely cause rather than dumping the block list. A response
    # carrying only thinking blocks almost always means max_tokens was spent
    # before any text was written -- and the raw error for that reads like a
    # malformed reply, which sends you looking in the wrong place.
    kinds = [block.type for block in response.content]
    if response.stop_reason == "max_tokens" or kinds == ["thinking"]:
        raise RuntimeError(
            f"no text in response (blocks={kinds}, stop_reason="
            f"{response.stop_reason!r}): the token budget was exhausted before "
            f"any text was produced. On Sonnet 5 thinking is on by default and "
            f"max_tokens caps thinking plus text together -- raise max_tokens."
        )
    raise RuntimeError(f"no text in response: blocks={kinds}")


def search(request: str, *, client=None) -> dict:
    """Step 1: what should be watched, where, and how often."""
    client = client or Anthropic()
    response = client.messages.create(
        model=PLAN_MODEL,
        max_tokens=PLAN_MAX_TOKENS,
        system=SEARCH_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        messages=[{"role": "user", "content": request}],
    )
    for block in response.content:
        if block.type == "server_tool_use":
            print(f"[searched] {block.input.get('query')}", file=sys.stderr)
    return _parse_json(_text_of(response))


def read_value(url: str, hint: str, condition: dict, text: str, *, client=None) -> dict:
    """Step 2: what does the page actually say, verbatim?

    Haiku, on text -- the same job the Checker used to do on every tick, now
    done once. Its answer is not the watch's reading; it is the anchor that
    makes step 3 affordable.
    """
    client = client or Anthropic()
    response = client.messages.create(
        model=READ_MODEL,
        max_tokens=READ_MAX_TOKENS,
        system=READ_PROMPT,
        messages=[{"role": "user", "content": (
            f"Condition: {json.dumps(condition)}\n"
            f"What to look for: {hint}\n"
            f"URL: {url}\n\nPage text:\n{text}"
        )}],
    )
    return _parse_json(_text_of(response))


def compile_extractor(url: str, hint: str, anchor: str, fragments: list, *,
                      shape: str = "value", feedback: str = "",
                      client=None) -> dict:
    """Step 3: name a selector, shown only the markup around the anchor.

    `shape` decides what the anchor *is*. For a value watch it is the reading
    itself. For a presence watch the thing being watched does not exist yet, so
    the anchor is a neighbour -- another listing of the same kind -- and what
    gets compiled is a counter over the list it sits in.
    """
    client = client or Anthropic()
    joined = "\n\n--- fragment ---\n".join(fragments)
    problem = f"\n\nA previous attempt failed: {feedback}\nFix it." if feedback else ""

    if shape == "presence":
        system, anchor_line = COUNT_PROMPT, (
            f"An example item currently in the list: {anchor!r}\n"
            f"(This is NOT what the user wants -- it is a neighbour, shown to "
            f"reveal the list's structure.)"
        )
    else:
        system, anchor_line = COMPILE_PROMPT, (
            f"The literal value on the page right now: {anchor!r}"
        )

    response = client.messages.create(
        model=PLAN_MODEL,
        max_tokens=COMPILE_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": (
            f"URL: {url}\nWhat to watch: {hint}\n{anchor_line}\n"
            f"{problem}\n\nHTML fragments around it:\n{joined}"
        )}],
    )
    return _parse_json(_text_of(response))


def _tidy(spec: dict) -> dict:
    """Drop the keys the model filled with null, and anything unrecognised."""
    keep = ("scope", "kind", "selector", "pattern", "path", "attribute",
            "parse", "unavailable_if")
    return {k: v for k, v in spec.items() if k in keep and v not in (None, "")}


ATTEMPTS = 2

# `:-soup-contains("...")` and its deprecated alias `:contains("...")`.
_TEXT_FILTER = re.compile(r':(?:-soup-)?contains\((["\'])(?:(?!\1).)*\1\)')


def unfiltered(selector: str) -> str:
    """The same selector with its text filters removed."""
    return _TEXT_FILTER.sub("", selector).strip()


def prove_the_item_selector(spec: dict, raw: str) -> str | None:
    """Check that a count spec could ever match anything. Returns a complaint.

    A count verified at zero is much weaker evidence than a price verified at
    $789: it shows the container exists and the selector parses, but not that
    the selector would match the thing being waited for when it finally
    appears. A wrong item class counts zero today, counts zero forever, and
    never reports a fault -- the watch is alive, billed, and incapable of
    firing.

    So the filter is stripped and the bare item selector re-run. It must match
    something now: the page lists *other* jobs today, and if the selector
    cannot see those it will not see the one the user is waiting for either.
    Only the text filter is left unproven, which is the irreducible part --
    nobody can verify a match against a posting that does not exist yet.
    """
    selector = spec.get("selector", "")
    bare = unfiltered(selector)
    if not bare or bare == selector:
        return None  # nothing was filtered; the count itself is the proof

    probe = {**spec, "selector": bare}
    probe.pop("unavailable_if", None)
    result = extract(probe, raw)
    if result.ok and result.value:
        return None

    return (
        f"the item selector {bare!r} (your selector with its text filter "
        f"removed) matches nothing on this page, so the filtered version can "
        f"never match either. The page does list items today -- find the "
        f"selector that matches one of those, then filter it."
    )


def build_extractor(url: str, hint: str, condition: dict, raw: str, *,
                    shape: str = "value", client=None) -> dict:
    """Steps 2-4 for one target. Returns a verified spec, or raises.

    The verification is the point. A spec that does not reproduce what it was
    compiled from is not a plan, it is a guess -- and a guess that fails
    silently, months later, on a schedule.

    ## Why `shape` exists

    The first version of this function required a literal value to be present
    on the page before it would compile anything. That is right for a price and
    exactly backwards for the largest class of watch there is: a job posting, a
    restock, an open appointment. Those are *absent by definition* until the
    moment the watch is supposed to fire, so demanding the value up front meant
    a vacancy watch could never be created at all -- the `count` kind was
    unreachable through the Planner in precisely the case it was added for.

    A presence watch anchors on a *neighbour* instead: any other listing on the
    page reveals the list's markup, and what gets compiled is a counter over
    that list. Verifying it means the container matched and the count ran.
    **Zero is a passing verification**, not a failure.
    """
    text = to_text(raw)[:20000]
    reading = read_value(url, hint, condition, text, client=client)
    literal = (reading.get("literal") or "").strip()
    sample = (reading.get("sample") or "").strip()
    note = reading.get("note", "")

    if shape == "presence":
        # Either works as an anchor: if a matching item happens to be listed
        # today, its markup is just as good a guide to the list's shape.
        anchor = literal or sample
        if not anchor:
            raise ValueError(
                f"{url} lists nothing that could be counted: {note}"
            )
    else:
        anchor = literal
        if not anchor:
            raise ValueError(
                f"nothing to watch on {url}: {note}. If this is something you "
                f"are waiting to APPEAR rather than to change, it needs to be "
                f"planned as a presence watch."
            )

    fragments = windows_around(raw, anchor)
    if not fragments:
        # The anchor is in the visible text but not verbatim in the markup --
        # split across tags, or entity-escaped. Give the model the text window
        # instead of nothing; a regex over text may still be compilable.
        fragments = windows_around(text, anchor) or [text[:4000]]

    feedback = ""
    for attempt in range(ATTEMPTS):
        spec = _tidy(compile_extractor(
            url, hint, anchor, fragments, shape=shape,
            feedback=feedback, client=client))
        try:
            validate_spec(spec)
            result = extract(spec, raw)
        except SpecError as exc:
            feedback = f"the spec was malformed: {exc}"
            continue

        if result.ok:
            # A count of zero is a legitimate reading, but on its own it does
            # not show the selector could ever match. Prove the item selector
            # separately before trusting it for months.
            complaint = (prove_the_item_selector(spec, raw)
                         if shape == "presence" else None)
            if complaint is None:
                print(f"[verified] {url} -> {result.value!r} via {spec}",
                      file=sys.stderr)
                return {"extractor": spec, "verified_value": result.value,
                        "verified_raw": result.raw, "literal": anchor}
            feedback = complaint
            print(f"[attempt {attempt + 1} unproven] {complaint}", file=sys.stderr)
            continue

        if shape == "presence":
            # A count that ran and returned nothing is a working extractor
            # describing an empty list -- which is the normal state of a watch
            # for something that has not happened yet.
            feedback = (
                f"running it gave {result.status}"
                f"{': ' + result.error if result.error else ''}. The scope must "
                f"select the list container that holds items like {anchor!r}, "
                f"and it must match the page as it is written today."
            )
        else:
            feedback = (
                f"running it gave {result.status}"
                f"{': ' + result.error if result.error else ''}, "
                f"but the value {anchor!r} is definitely on the page"
            )
        print(f"[attempt {attempt + 1} failed] {feedback}", file=sys.stderr)

    raise ValueError(f"could not compile a working extractor for {url}: {feedback}")


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
