"""Talking to a model: the parts that are the same whatever is being planned.

Split out of `plan.py` in Phase 9. Nothing here knows what a watch is -- it
knows which model to call, how big a token budget to ask for, and how to get
JSON back out of a reply that may not contain any text at all.

Keeping this separate is what lets a new kind of watch add a prompt without
also re-deriving how to parse a response, which is where two of this project's
production failures came from.
"""

import json

PLAN_MODEL = "claude-sonnet-5"
READ_MODEL = "claude-haiku-4-5-20251001"

# Sonnet 5 runs *adaptive thinking by default* when the `thinking` parameter is
# omitted -- a change from Sonnet 4.6, which ran without it. And `max_tokens` is
# a hard cap on thinking *plus* response text, not just the reply.
#
# The first version of this asked for a spec with max_tokens=1024. Thinking
# consumed the whole budget, the response came back carrying only a
# ThinkingBlock and no text at all, and the Planner failed with "No text in
# response" -- which reads like a malformed reply rather than a token ceiling.
# These budgets are deliberately generous: planning happens once per watch and
# is amortised over ~14,000 checks, so headroom here is free.
PLAN_MAX_TOKENS = 8192
COMPILE_MAX_TOKENS = 4096
READ_MAX_TOKENS = 1024


def parse_json(raw: str) -> dict:
    """Take the outermost {...} rather than trusting the whole string.

    `plan.py` used to assume the last text block was clean JSON and was
    observed failing in Lambda on an empty block. This is the sturdier parse
    the Checker already used.
    """
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No JSON object in model response: {raw[:200]!r}")
    return json.loads(raw[start:end + 1])


def text_of(response) -> str:
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


def ask(client, *, model, max_tokens, system, content, tools=None) -> dict:
    """One model call that must answer with a JSON object."""
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    if tools:
        kwargs["tools"] = tools
    return parse_json(text_of(client.messages.create(**kwargs)))
