"""Being refused by a source, as a state of its own.

## Why this is not just another failure

Everything this system reads is read without permission. Amazon's blocking is
probabilistic and IP-dependent: six consecutive renders worked on 2026-08-04
and fifteen more on 2026-08-05, and none of that is a guarantee for tomorrow.
The day it starts refusing, what happens today is:

    every Amazon watch's extractor "stops matching"
      -> each pays Haiku for a repair that cannot possibly work
      -> each repair fails against a captcha page
      -> three ticks later each watch degrades, separately
      -> the owner gets N emails saying N watches broke

Every part of that is wrong. Nothing is broken, no repair can help, and the
one fact worth knowing -- *Amazon is refusing us* -- is the one thing never
said. It is the `unavailable` vs `failed` distinction one layer down: the
engine already refuses to conflate "there is legitimately nothing today" with
"the extractor is broken", and this is the third case, "we were not allowed to
look".

So a refusal is detected at the fetch boundary, never repaired, counted
separately from failures, and published as **one metric** so that "the day
Amazon starts blocking" is a single alarm rather than a wave of unrelated-
looking breakages.

## The trap, which is the same one as last time

A block page is recognised by its words, and **a phrase found on a page is not
a fact about the page** -- the lesson from `unavailable_if` matching a string
table, and from "FREE delivery" appearing in fourteen product cards that had
no free delivery. "Access Denied" is a plausible substring of a real shop's
help centre; "are you a robot" is a plausible substring of a forum thread.

Three defences, in order of how much they buy:

1. **Only the top of the document is searched.** A refusal page says so
   immediately, in its `<title>` or its first paragraph. A real page that
   happens to contain the words carries them thousands of characters in.
2. **Strong markers stand alone; weak ones need corroboration.** "Enter the
   characters you see below" is Amazon's captcha and nothing else. "Access
   denied" is a phrase. The second only counts on a page small enough to be a
   block page -- a real Amazon search is ~1MB and a captcha is a few KB, which
   is a two-order-of-magnitude gap and the cheapest discriminator available.
3. **A refusal never silences a watch by itself.** It is recorded and reported
   and it stops repairs; it takes many consecutive refusals to stop a watch,
   because guessing wrong in this direction kills a working watch over one bad
   render.
"""

BLOCKED = "blocked"

# Status codes that mean "not you, and not now". 401 is deliberately absent:
# it means the resource needs credentials, which is a planning mistake rather
# than a refusal, and it will never clear by waiting.
BLOCKING_STATUS = (403, 429, 503)

# How much of the document to search. A refusal announces itself at the top;
# anything further in is a page that merely mentions the words.
HEAD_CHARS = 4000

# Above this, the response is a real page. A block page is a form and a
# sentence; the Amazon search it replaces is around a million characters.
BLOCK_PAGE_MAX_CHARS = 120000

# Unambiguous. Each of these belongs to one bot-defence product and appears
# nowhere else.
STRONG = (
    # Amazon
    "enter the characters you see below",
    "type the characters you see in this image",
    "/errors/validatecaptcha",
    "to discuss automated access to amazon data",
    # Cloudflare
    "checking your browser before accessing",
    "attention required! | cloudflare",
    "cf_chl_opt",
    # Imperva / Distil
    "request unsuccessful. incapsula incident id",
    "pardon our interruption",
    # DataDome
    "captcha-delivery.com",
    # PerimeterX
    "px-captcha",
)

# Real phrases that are also ordinary English. Only believed on a document
# small enough to be a block page.
WEAK = (
    "access denied",
    "unusual traffic",
    "are you a robot",
    "verify you are a human",
    "you don't have permission to access",
    "automated access",
    "rate limit exceeded",
    "too many requests",
)


class Blocked(Exception):
    """The source refused. Not a broken extractor, and not an empty result.

    Carries the reason as its message so the row, the log and the email can
    all say the same sentence.
    """

    def __init__(self, reason: str, *, url: str = "", status=None):
        super().__init__(reason)
        self.reason = reason
        self.url = url
        self.status = status


def host_of(url) -> str:
    """The host, for grouping a metric. "amazon.com", not a whole URL."""
    if not isinstance(url, str) or "//" not in url:
        return "unknown"
    host = url.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]
    return host[4:] if host.startswith("www.") else host


def reason(*, status=None, body: str = "", url: str = ""):
    """Why this response is a refusal, or None if it is a page.

    Returns a sentence rather than a boolean, because the whole point is that
    someone reading an email learns *which* wall they hit.
    """
    if status in BLOCKING_STATUS:
        return (f"{host_of(url)} answered HTTP {status} — it is refusing "
                f"automated requests right now")

    if not isinstance(body, str) or not body:
        return None

    head = body[:HEAD_CHARS].lower()

    for marker in STRONG:
        if marker in head:
            return (f"{host_of(url)} served a bot check "
                    f"(matched {marker!r}) instead of the page")

    # Weak markers need the page to be block-page shaped. On a real page these
    # words are a help article or a forum post, and treating them as a refusal
    # would stop a working watch.
    if len(body) <= BLOCK_PAGE_MAX_CHARS:
        for marker in WEAK:
            if marker in head:
                return (f"{host_of(url)} served a short refusal page "
                        f"(matched {marker!r}, {len(body)} characters)")

    return None


def check(*, status=None, body: str = "", url: str = "") -> None:
    """Raise `Blocked` if this response is a refusal. Otherwise do nothing."""
    why = reason(status=status, body=body, url=url)
    if why is not None:
        raise Blocked(why, url=url, status=status)
