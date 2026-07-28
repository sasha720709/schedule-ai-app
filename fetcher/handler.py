"""Browser Fetcher Lambda: renders one URL in headless Chromium and returns
its visible text.

Kept separate from the Checker on purpose. Chromium needs ~1.5GB of memory
and several seconds of cold start; folding it into the Checker would make
every plain-HTTP tick pay that cost too. The Checker calls this only when
the Planner marked a target fetch_method="browser"."""

import re

from playwright.sync_api import sync_playwright

MAX_PAGE_CHARS = 20000
NAV_TIMEOUT_MS = 25000
# Let client-side rendering settle after DOM load -- the whole reason this
# Lambda exists is content that isn't in the initial HTML.
SETTLE_MS = 3000

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_WHITESPACE = re.compile(r"\s+")


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

            text = _WHITESPACE.sub(" ", page.inner_text("body")).strip()
            status = response.status if response else None
        finally:
            browser.close()

    return {"url": url, "status": status, "text": text[:MAX_PAGE_CHARS]}
