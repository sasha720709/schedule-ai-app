"""Phase 1 prototype: turn a plain-English watch request into a structured
plan, using Claude with the server-side web_search tool. No AWS yet."""

import json
import sys

from anthropic import Anthropic

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You are the planning stage of a price/availability watcher.

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
  (Steam store pages, many modern storefronts). It costs far more time and
  memory per check, so do not use it by default.
- NEITHER method defeats bot protection. amazon.com and bestbuy.com
  actively block automated traffic and will fail every time -- do not pick
  them. Prefer the manufacturer's own site, an official store page, or a
  price-tracking site that serves ordinary HTML.
- If a value is available from a plain JSON endpoint in a single request,
  that is an excellent target: use fetch_method "http" and say in the hint
  which field to read.

After you finish searching, respond with ONLY a JSON object, no other
text before or after it, matching this shape:
{
  "condition": {"metric": string, "op": string, "value": number, "currency": string | null},
  "check_interval_min": integer,
  "targets": [
    {"url": string, "extract_hint": string, "fetch_method": "http" | "browser"}
  ]
}
"""


def plan(request: str) -> dict:
    client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        messages=[{"role": "user", "content": request}],
    )

    for block in response.content:
        if block.type == "server_tool_use":
            print(f"[searched] {block.input.get('query')}", file=sys.stderr)

    text_blocks = [block.text for block in response.content if block.type == "text"]
    if not text_blocks:
        raise RuntimeError(f"No text in response: {response.content}")

    raw = text_blocks[-1].strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


if __name__ == "__main__":
    request = " ".join(sys.argv[1:]) or "tell me when the PS5 Slim drops under $400"
    result = plan(request)
    print(json.dumps(result, indent=2))
