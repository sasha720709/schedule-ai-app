"""Prompts that are not specific to one kind of watch.

Split out of `plan.py` in Phase 9. The texts are copied verbatim -- every
paragraph in them was added after a real request failed, so a refactor that
reworded one would be undoing evidence.

`SEARCH_PROMPT` is the one to watch. It currently carries the rules for three
request types at once, and two shipped bugs came from those rules interfering
with each other. Phase 9's job is to make it shrink as kinds move out; if it
grows instead, the phase has failed.
"""

SEARCH_PROMPT = """You are the planning stage of a price/availability watcher.

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
  (Steam store pages, many modern storefronts). Since the checking model was
  removed, the browser is now the single most expensive part of a check --
  about eighty times a plain fetch -- so do not use it by default.
- A PLAIN JSON ENDPOINT IS THE BEST POSSIBLE TARGET. If the fact is
  available from an API in one request, use it: it is stable, tiny, and
  hundreds of times cheaper than rendering a page. Look for one first.
- NEITHER method defeats bot protection. amazon.com and bestbuy.com
  actively block automated traffic and will fail every time -- do not pick
  them. Prefer the manufacturer's own site, an official store page, or a
  price-tracking site that serves ordinary HTML.

CONDITIONS. `op` must be one of: <  <=  >  >=  ==  !=
Write the metric as a short snake_case name.

THE SHAPE IS ALREADY DECIDED and is given to you as `watch_shape`. Honour it.

- "value"    -- the thing is on the page now; the user waits for it to CHANGE.
- "presence" -- the thing does not exist yet; the user waits for it to APPEAR,
                and the condition is a count reaching one, e.g.
                {"metric": "matching_vacancies", "op": ">=", "value": 1}.

For a `presence` request, do NOT invent a number that happens to be on the
page. Find pages that LIST things of the kind being waited for.

RELATIVE CONDITIONS. You do not know the current value. You are writing this
before the page has been opened, so any number you have came from search
results and is stale, approximate, or about a different quote entirely.

**Never invent an absolute threshold for a request phrased relative to now.**
"goes down", "drops below current", "falls 5%", "cheaper than it is today" are
all relative. Put the change in `relative_change_pct` and leave
`condition.value` as null -- the threshold is computed later from the value
actually read off the page, which is the only baseline that is real.

  "tell me when it goes down"        -> relative_change_pct: 0
  "tell me when it drops 5%"         -> relative_change_pct: -5
  "tell me when it goes 10% above"   -> relative_change_pct: 10
  "tell me when it drops below $300" -> relative_change_pct: null, value: 300

A user who says "goes down" means ANY decrease. Do not decide on their behalf
that they meant a meaningful one and pick a percentage.

After you finish searching, respond with ONLY a JSON object, no other
text before or after it, matching this shape:
{
  "relative_change_pct": number | null,
  "condition": {"metric": string, "op": string, "value": number | boolean, "currency": string | null},
  "check_interval_min": integer,
  "targets": [
    {"url": string, "extract_hint": string, "fetch_method": "http" | "browser"}
  ]
}
"""

READ_PROMPT = """You read a web page so an extractor can be compiled against it.

YOU ARE READING, NOT JUDGING. You are shown the condition only so you know
WHICH value to find -- "price < 700" tells you to look for a price, not for a
rating. It is NOT a filter on what you may report.

**Never withhold a value because it fails the condition.** A price of $949
against a condition of "under $700" is exactly the situation a watch exists
for: the user is waiting for it to change. Reporting "the price does not meet
the condition" instead of "$949.00" makes the watch impossible to create at
all. Whether the condition holds is decided later, in Python, for free, on
every check. Your only job is to say what the page says right now.

You are given page text, a hint about what to watch, and the condition the
value will later be compared against. Report two things.

Respond with ONLY a JSON object:
{
  "literal": string | null,  // the watched value, EXACTLY as it appears, e.g. "$789.00"
  "sample":  string | null,  // ANY one comparable item on the page, verbatim
  "note": string             // one short sentence
}

`literal` is the thing being watched, if it is on the page right now.

`sample` matters most when `literal` is null, and you should almost always be
able to fill it in. Many watches are for something that has NOT happened yet --
a job that has not been posted, an item that is out of stock, a slot not yet
open. The page still shows *other* items of the same kind, and one of those is
what makes the page's structure readable. On a job board with no matching role,
`sample` is the title of any other job listing. On a sold-out product page, it
is any other product's name.

Both strings must occur in the text character for character -- they are going
to be searched for programmatically. Do not reformat, round, translate, or add
a currency symbol that is not there.

Use null for `sample` only if the page genuinely lists nothing at all."""
