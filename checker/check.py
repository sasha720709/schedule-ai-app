"""The Checker's actual work: fetch one page, ask Haiku whether the
condition is met. No web search, no planning -- this runs on every tick,
so it stays as small and cheap as possible."""

import json

from anthropic import Anthropic

# Fetching moved to shared/fetch.py when the Planner needed it too -- it now
# opens the pages it proposes, rather than recommending them sight-unseen.
# Re-exported here so callers of check.fetch_text keep working.
from fetch import MAX_TEXT_CHARS as MAX_PAGE_CHARS  # noqa: F401
from fetch import fetch_raw, fetch_text, to_text  # noqa: F401

MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You check whether a condition is currently true on a web page.

You are given the text of a page, a hint about what to look for, and a
condition. Find the value the hint describes, then judge the condition.

Respond with ONLY a JSON object, no other text:
{
  "last_value": string | null,   // what you found, verbatim, e.g. "$429.99"
  "condition_met": boolean,      // is the condition true right now
  "note": string                 // one short sentence; say so if unsure
}

If the value genuinely isn't on the page (blocked, changed layout, sold
out), use null for last_value, false for condition_met, and explain in note.
Never guess a value that isn't there."""


def _parse_json(raw: str) -> dict:
    """Claude sometimes wraps JSON in prose or a code fence, so take the
    outermost {...} rather than trusting the whole string to be clean."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No JSON object in model response: {raw[:200]!r}")
    return json.loads(raw[start:end + 1])


def judge(url: str, extract_hint: str, condition: dict, page_text: str) -> dict:
    """Decide the condition against already-fetched text. Kept separate from
    fetching so the caller can source that text however it likes -- plain
    GET here, or the browser Fetcher Lambda for JS-rendered pages."""
    client = Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Condition: {json.dumps(condition)}\n"
                f"What to look for: {extract_hint}\n"
                f"URL: {url}\n\n"
                f"Page text:\n{page_text}"
            ),
        }],
    )

    text_blocks = [block.text for block in response.content if block.type == "text"]
    if not text_blocks:
        raise RuntimeError(f"No text in response: {response.content}")

    return _parse_json(text_blocks[-1])


def check(url: str, extract_hint: str, condition: dict) -> dict:
    """Plain-HTTP convenience path, used for local testing."""
    return judge(url, extract_hint, condition, fetch_text(url))
