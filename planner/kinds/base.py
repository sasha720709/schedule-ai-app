"""What every compiled kind of watch has in common, and where they differ.

## The shape of the seam

Compiling an extractor is four steps, and only some of them vary by kind:

1. read the page                     -- shared, `plan.read_value`
2. choose an anchor from that reading -- **varies**
3. compile a spec from markup around the anchor -- **varies** (prompt, framing)
4. run the spec and decide whether to trust it  -- shared loop,
   **varies** in what counts as proof and what to say when it fails

So a Kind is four small methods, not a god object. Each one has a real
implementation in every kind: no `pass`, no `return None` placeholder. That is
the test of whether this abstraction is the right one -- the moment a kind has
to stub a method out, the axis is wrong and this file should change, not the
kind.

Deliberately *not* here: anything about fetching, scheduling, notifying or
cost. Those vary along different axes (see `docs/phase-9-watch-kinds.md`) and
folding them in would rebuild the tangle this replaces.
"""

import sys

from extract import SpecError, extract, validate_spec

import llm

# Two compile attempts. The second is fed the reason the first failed, which
# is the only thing that makes a retry worth more than a coin flip.
ATTEMPTS = 2


def tidy(spec: dict) -> dict:
    """Drop the keys the model filled with null, and anything unrecognised."""
    keep = ("scope", "kind", "selector", "pattern", "path", "attribute",
            "parse", "unavailable_if")
    return {k: v for k, v in spec.items() if k in keep and v not in (None, "")}


class Kind:
    """Base class, and the documentation of what a kind must decide.

    Subclasses override the four varying steps. The default implementations
    here are the `value` behaviour, because that is the one every other kind is
    a deviation from -- and because an unrecognised kind must degrade to
    today's behaviour rather than to a refusal.
    """

    name = "value"
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


def compile_and_verify(kind: Kind, url: str, hint: str, anchor: str,
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
