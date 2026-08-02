"""What a kind of watch is, and what compiled kinds share.

## Two levels, because step 2 proved one was not enough

Every kind answers exactly one question: **given a proposed target, produce a
verified way to read it.** That is `Kind.resolve`, and it is the only method
every kind implements.

Most kinds answer it by compiling an extractor, which is four steps and only
some of them vary:

1. read the page                                -- shared
2. choose an anchor from that reading           -- **varies**
3. compile a spec from markup around the anchor -- **varies** (prompt, framing)
4. run the spec and decide whether to trust it  -- shared loop,
   **varies** in what counts as proof and what to say when it fails

Those four live on `CompiledKind`, which implements `resolve` in terms of them.

`quote` does not compile anything -- the URL and the extractor come out of a
registry, and the only work left is proving the canned spec against the live
endpoint. So it extends `Kind` directly and never sees the four methods.

That split was forced. The first version of this file had one `Kind` with the
four compile methods on it, and adding `quote` would have meant stubbing all
four out. Three null objects in a row is the documented signal that an axis is
wrong (see `docs/phase-9-watch-kinds.md` §2), so the axis moved rather than the
kind being bent to fit. The rule stands: **no kind may leave a method empty.**

Deliberately *not* here: scheduling, notifying and cost. Those vary along
different axes and folding them in would rebuild the tangle this replaces.
"""

import json
import sys

from anthropic import Anthropic
from extract import SpecError, extract, validate_spec
from fetch import to_text, windows_around

import llm
from prompts import READ_PROMPT

# The page text handed to the reading model. Longer costs money for no gain --
# the anchor it returns only has to be findable in the markup afterwards.
MAX_READ_CHARS = 20000

# Two compile attempts. The second is fed the reason the first failed, which
# is the only thing that makes a retry worth more than a coin flip.
ATTEMPTS = 2


def tidy(spec: dict) -> dict:
    """Drop the keys the model filled with null, and anything unrecognised."""
    keep = ("scope", "kind", "selector", "pattern", "path", "attribute",
            "parse", "unavailable_if")
    return {k: v for k, v in spec.items() if k in keep and v not in (None, "")}


def read_value(url: str, hint: str, condition: dict, text: str, *, client=None) -> dict:
    """What does the page actually say, verbatim?

    Haiku, on text -- the same job the Checker used to do on every tick, now
    done once. Its answer is not the watch's reading; it is the anchor that
    makes compiling affordable, because a 1.5MB page is ~375,000 tokens and the
    fragments that matter are a few hundred characters.
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


class Kind:
    """A way of turning a proposed target into a verified way to read it."""

    name = "value"

    # Whether 8d should pay a model to rewrite this kind's extractor when it
    # breaks. True for anything compiled against a site we do not control.
    # False for a registry-supplied spec, where the fix is one edit to
    # `sources.py` for every watch at once.
    self_heals = True

    def resolve(self, target: dict, condition: dict, *,
                fetch_http, fetch_browser, client=None) -> dict:
        """Return everything the target row needs, or raise.

        Keys: url, extract_hint, fetch_method, extractor, verified_value,
        verified_raw, why. `why` is for the log and the plan card -- it is the
        one-line explanation of how this target came to be trusted.
        """
        raise NotImplementedError


class CompiledKind(Kind):
    """A kind whose extractor is written by a model and then proved."""

    compile_prompt = ""

    # -- step 2 ------------------------------------------------------------

    def anchor(self, reading: dict, url: str) -> str:
        """The literal string step 3 will be shown. Raise if there is none."""
        raise NotImplementedError

    def describe_anchor(self, anchor: str) -> str:
        """How that string is introduced to the compiling model."""
        raise NotImplementedError

    # -- step 4 ------------------------------------------------------------

    def prove(self, spec: dict, raw: str) -> str | None:
        """Extra proof beyond "it ran". Return a complaint, or None to accept."""
        return None

    def feedback(self, result, anchor: str) -> str:
        """What to tell the model when the spec ran but did not produce a value."""
        raise NotImplementedError

    # -- the pipeline those four drive --------------------------------------

    def build(self, url: str, hint: str, condition: dict, raw: str, *,
              client=None) -> dict:
        """Read, anchor, locate, compile, verify. Returns a verified spec."""
        text = to_text(raw)[:MAX_READ_CHARS]
        reading = read_value(url, hint, condition, text, client=client)
        anchor = self.anchor(reading, url)

        fragments = windows_around(raw, anchor)
        if not fragments:
            # The anchor is in the visible text but not verbatim in the markup
            # -- split across tags, or entity-escaped. Give the model the text
            # window instead of nothing; a regex over text may still compile.
            fragments = windows_around(text, anchor) or [text[:4000]]

        return compile_and_verify(
            self, url, hint, anchor, fragments, raw, client=client)

    def resolve(self, target: dict, condition: dict, *,
                fetch_http, fetch_browser, client=None) -> dict:
        url = target["url"]
        hint = target["extract_hint"]
        built, fetch_method, why = build_with_cheapest_fetch(
            self, url, hint, condition,
            fetch_http=fetch_http, fetch_browser=fetch_browser, client=client)
        return {"url": url, "extract_hint": hint, "fetch_method": fetch_method,
                "why": why, **built}


def build_with_cheapest_fetch(kind: CompiledKind, url: str, hint: str,
                              condition: dict, *, fetch_http, fetch_browser,
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
    `build` gives up before compiling when the value is not in the text at all,
    which is exactly the shape of a JS-rendered page. Roughly $0.0001, once,
    against $7.87/month saved every time the guess would have been wrong.

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
            built = kind.build(url, hint, condition, raw, client=client)
            return built, "http", "verified against raw HTML; no browser needed"
        except Exception as exc:  # noqa: BLE001
            cheap_error = f"could not verify against raw HTML: {exc}"

    print(f"[escalating to browser] {url}: {cheap_error}", file=sys.stderr)
    raw = fetch_browser()
    built = kind.build(url, hint, condition, raw, client=client)
    return built, "browser", f"needed rendering -- {cheap_error}"


def compile_and_verify(kind: CompiledKind, url: str, hint: str, anchor: str,
                       fragments: list, raw: str, *, client) -> dict:
    """Steps 3 and 4. Returns a verified spec, or raises.

    The verification is the point. A spec that does not reproduce what it was
    compiled from is not a plan, it is a guess -- and a guess that fails
    silently, months later, on a schedule.
    """
    joined = "\n\n--- fragment ---\n".join(fragments)
    feedback = ""

    for attempt in range(ATTEMPTS):
        problem = (f"\n\nA previous attempt failed: {feedback}\nFix it."
                   if feedback else "")
        spec = tidy(llm.ask(
            client,
            model=llm.PLAN_MODEL,
            max_tokens=llm.COMPILE_MAX_TOKENS,
            system=kind.compile_prompt,
            content=(
                f"URL: {url}\nWhat to watch: {hint}\n"
                f"{kind.describe_anchor(anchor)}\n"
                f"{problem}\n\nHTML fragments around it:\n{joined}"
            ),
        ))

        try:
            validate_spec(spec)
            result = extract(spec, raw)
        except SpecError as exc:
            feedback = f"the spec was malformed: {exc}"
            continue

        if result.ok:
            complaint = kind.prove(spec, raw)
            if complaint is None:
                print(f"[verified] {url} -> {result.value!r} via {spec}",
                      file=sys.stderr)
                return {"extractor": spec, "verified_value": result.value,
                        "verified_raw": result.raw, "literal": anchor}
            feedback = complaint
            print(f"[attempt {attempt + 1} unproven] {complaint}", file=sys.stderr)
            continue

        feedback = kind.feedback(result, anchor)
        print(f"[attempt {attempt + 1} failed] {feedback}", file=sys.stderr)

    raise ValueError(f"could not compile a working extractor for {url}: {feedback}")
