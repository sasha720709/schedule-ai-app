"""Reading a page, in one place.

The Planner needs this as of Phase 8b. Until now it had no way to fetch
anything: it web-searched, recommended URLs sight-unseen, and handed them over.
**It had never once opened a page it proposed** -- which is why every Phase 2
failure (Amazon, Best Buy) was discovered by the Checker, in production, on a
schedule, instead of at plan time by a single GET.

Two representations, the same split the browser Fetcher makes:

    raw     markup, for compiling and running extractors
    text    visible text, for the model, where length is billed
"""

import re
import urllib.request

# Enough of a page for a price or status to appear, without paying to send a
# whole megabyte of markup to a model.
MAX_TEXT_CHARS = 20000
# Markup is not a token budget -- the only limits are memory and parse time.
# The real Steam Deck page is 1.49MB.
MAX_RAW_CHARS = 2000000
FETCH_TIMEOUT_SEC = 20

# Some sites reject the default urllib agent outright.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def fetch_raw(url: str) -> str:
    """Plain HTTP GET, markup intact.

    A CSS selector has nothing to select in a string that has had its tags
    stripped -- the same reason the browser Fetcher now returns `html`.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SEC) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return raw[:MAX_RAW_CHARS]


def to_text(raw: str) -> str:
    """Crudely reduce markup to visible text, for the model path only."""
    text = _SCRIPT_STYLE.sub(" ", raw)
    text = _TAG.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def fetch_text(url: str) -> str:
    return to_text(fetch_raw(url))[:MAX_TEXT_CHARS]


def windows_around(haystack: str, needle: str, *, radius: int = 1200,
                   limit: int = 3) -> list:
    """Slices of markup surrounding a literal value.

    This is what makes compiling a selector affordable. The rendered Steam Deck
    page is 1.49MB -- roughly 375,000 tokens, past any context window worth
    paying for. But once the value's literal text is known ("$789.00"), the
    markup that matters is the few hundred characters wrapped around it, and
    that is what a model actually needs in order to name a selector.

    Deterministic on purpose: locating a string is not a job for a model.
    """
    if not needle:
        return []

    found, start = [], 0
    while len(found) < limit:
        at = haystack.find(needle, start)
        if at == -1:
            break
        found.append(haystack[max(0, at - radius):at + len(needle) + radius])
        start = at + len(needle)
    return found
