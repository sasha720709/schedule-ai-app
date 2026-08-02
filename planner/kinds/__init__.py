"""The registry of watch kinds.

Adding a kind should be: write a module, add one line here, write its tests.
Nothing in `plan.py` should need editing. If it does, the seam is in the wrong
place -- see `docs/phase-9-watch-kinds.md` §11.

`get()` falls back to `value` rather than raising, on purpose. An unrecognised
kind must degrade to today's behaviour, never to a refusal: the classifier is
the newest and least-proven part of this design, and a wrong guess should cost
a suboptimal plan, not a rejected request.
"""

from kinds.base import (Kind, CompiledKind, build_with_cheapest_fetch,
                        compile_and_verify, read_value, tidy)
from kinds.presence import PresenceKind
from kinds.quote import QuoteKind
from kinds.value import ValueKind

REGISTRY = {
    "value": ValueKind(),
    "presence": PresenceKind(),
    "quote": QuoteKind(),
}

DEFAULT = REGISTRY["value"]

__all__ = ["CompiledKind", "Kind", "REGISTRY", "build_with_cheapest_fetch",
           "compile_and_verify", "get", "names", "read_value", "tidy"]


def get(name: str | None) -> Kind:
    return REGISTRY.get(name or "", DEFAULT)


def names() -> tuple:
    return tuple(REGISTRY)
