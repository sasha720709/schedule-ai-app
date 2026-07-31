"""Browser Fetcher Lambda: renders one URL in headless Chromium and returns
both the rendered HTML and its visible text.

Kept separate from the Checker on purpose. Chromium needs ~1.5GB of memory
and several seconds of cold start; folding it into the Checker would make
every plain-HTTP tick pay that cost too. The Checker calls this only when
the Planner marked a target fetch_method="browser".

## Why two representations

Phase 8 made this a blocker. The Checker used to send text to a model and ask
it to read a value; text was all it needed. Now the Checker runs a compiled
extractor, and a CSS selector has nothing to select in a string with no
markup -- so returning only `inner_text` meant **CSS extraction could not work
on browser-rendered pages at all**, which is precisely the class of page this
Lambda exists to serve.

So both come back:

    html   the serialized DOM *after* JavaScript has run. What Tier 0
           extraction (css / regex / jsonpath) reads.
    text   visible text, whitespace collapsed. What a Tier 1 repair prompt
           sends to a model, where every token is billed and markup is noise.

`<script>` and `<style>` are deliberately **not** stripped from `html`, for a
reason that was measured rather than assumed. The reflex is to strip them as
noise; the counter-argument was that a JSON-LD block or a hydration payload is
often the cleanest price on the page. Both turn out to be beside the point:
on the Steam Deck page, 1.41MB of HTML, every script tag together came to
4,835 bytes and every style tag to 29,864 -- **2.5% of the document**.
Stripping would cost the best extraction targets to save nothing. The bloat in
a modern storefront is ordinary markup.
"""

import json
import re

from playwright.sync_api import sync_playwright

# Text is bounded because it may be sent to a model, where length is money.
MAX_TEXT_CHARS = 20000

# HTML is bounded only by what can cross the invoke boundary, and the bound is
# expressed in *encoded* bytes rather than characters on purpose. A synchronous
# Lambda response caps at 6MB; JSON escaping cost 1.14x on the page measured
# here, but escaping non-ASCII as \uXXXX costs up to 6x on a CJK-heavy document,
# and a storefront page can be either. Budgeting in characters means the margin
# silently depends on what language the page is written in.
#
# 4MB of the 6MB leaves room for the rest of the response and for a page half
# again as large as the 1.41MB one that motivated this.
MAX_HTML_JSON_BYTES = 4000000

NAV_TIMEOUT_MS = 25000
# Let client-side rendering settle after DOM load -- the whole reason this
# Lambda exists is content that isn't in the initial HTML.
SETTLE_MS = 3000

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_WHITESPACE = re.compile(r"\s+")


def fit_to_budget(html: str, budget: int = MAX_HTML_JSON_BYTES):
    """Trim `html` until its JSON encoding fits `budget`. Returns (html, cut).

    Measures this specific document's inflation rather than assuming one, then
    converges. Pure and dependency-free so it can be tested without a browser.
    """
    encoded = len(json.dumps(html))
    if encoded <= budget:
        return html, False

    keep = int(len(html) * budget / encoded)
    while keep > 0 and len(json.dumps(html[:keep])) > budget:
        keep = int(keep * 0.9)
    return html[:keep], True


def lambda_handler(event, context):
    url = event["url"]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            # Lambda's execution environment has no usable /dev/shm and
            # doesn't permit Chromium's sandbox.
            args=["--no-sandbox", "--disable-dev-shm-usage", "--single-process"],
        )
        try:
            context_ = browser.new_context(
                user_agent=UA,
                locale="en-US",
                viewport={"width": 1280, "height": 900},
            )
            page = context_.new_page()
            response = page.goto(url, timeout=NAV_TIMEOUT_MS,
                                 wait_until="domcontentloaded")
            page.wait_for_timeout(SETTLE_MS)

            html = page.content()
            text = _WHITESPACE.sub(" ", page.inner_text("body")).strip()
            status = response.status if response else None
        finally:
            browser.close()

    # Report truncation and true size rather than hiding them. An extractor
    # that misses because its element sat past the cut looks identical to an
    # extractor that is simply broken, and Phase 8d escalates the second but
    # must not waste a repair call on the first. This is not hypothetical: the
    # first version of this handler capped HTML at 500k, and the Steam Deck
    # price sits at roughly 1.1MB into a 1.41MB document.
    fitted, cut = fit_to_budget(html)
    return {
        "url": url,
        "status": status,
        "html": fitted,
        "html_chars": len(html),
        "html_truncated": cut,
        "text": text[:MAX_TEXT_CHARS],
        "text_truncated": len(text) > MAX_TEXT_CHARS,
    }
