"""Offline tests for the Fetcher's payload budgeting.

`lambda_handler` itself needs a real browser and is verified by invoking the
deployed Lambda. `fit_to_budget` is pure, and it is the part that fails
expensively: too high and the invoke returns an opaque Lambda error, too low
and extraction silently misses a value that was on the page. The first attempt
capped at 500,000 characters, and the Steam Deck price sits at roughly 1.1MB
into a 1.41MB document -- these tests exist so that does not recur.

The module is loaded by explicit path rather than `import handler`, because
`api/handler.py` exists too and a bare import would resolve to whichever
directory reached sys.path first.
"""

import importlib.util
import json
import os
import sys
import types

# handler.py imports playwright at module scope, which is right for a Lambda
# but means the module cannot be imported on a machine with no browser. Stub
# the one symbol it touches rather than restructure production code for a test.
if "playwright" not in sys.modules:
    _playwright = types.ModuleType("playwright")
    _sync_api = types.ModuleType("playwright.sync_api")
    _sync_api.sync_playwright = None
    _playwright.sync_api = _sync_api
    sys.modules["playwright"] = _playwright
    sys.modules["playwright.sync_api"] = _sync_api

_spec = importlib.util.spec_from_file_location(
    "fetcher_handler",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "handler.py"),
)
fetcher_handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetcher_handler)

fit_to_budget = fetcher_handler.fit_to_budget


def test_small_document_is_untouched():
    html = "<html><body>hello</body></html>"
    fitted, cut = fit_to_budget(html, budget=10000)
    assert fitted == html
    assert cut is False


def test_document_exactly_at_the_budget_is_not_cut():
    html = "a" * 100
    fitted, cut = fit_to_budget(html, budget=len(json.dumps(html)))
    assert fitted == html
    assert cut is False


def test_oversized_ascii_is_cut_to_fit():
    html = "<div>" + "x" * 50000 + "</div>"
    fitted, cut = fit_to_budget(html, budget=1000)
    assert cut is True
    assert len(json.dumps(fitted)) <= 1000
    assert html.startswith(fitted)


def test_cjk_inflation_is_measured_not_assumed():
    r"""The reason the budget is in encoded bytes and not characters.

    Every character here escapes to \uXXXX -- six bytes for one character. A
    character-count cap would admit this document at roughly six times the
    size it was meant to allow.
    """
    html = "測" * 20000
    fitted, cut = fit_to_budget(html, budget=6000)
    assert cut is True
    assert len(json.dumps(fitted)) <= 6000
    # Well under the ~6000 characters a naive character cap would have kept.
    assert len(fitted) < 1200


def test_a_page_the_size_of_the_real_steam_deck_one_survives_whole():
    """1.41MB of Latin markup is the case that motivated the rewrite.

    It must come back untruncated under the shipped budget, or CSS extraction
    on browser-rendered pages stays broken in exactly the way this fixes.
    """
    html = "<div class='x'>price $789.00</div>" * 42000
    assert len(html) > 1_400_000
    fitted, cut = fit_to_budget(html, budget=fetcher_handler.MAX_HTML_JSON_BYTES)
    assert cut is False
    assert fitted == html


def test_budget_too_small_for_anything_degrades_rather_than_hangs():
    fitted, cut = fit_to_budget("<html>" * 100, budget=2)
    assert cut is True
    assert len(fitted) == 0
