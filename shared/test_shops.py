"""Tests for the shops registry.

What is protected here is mostly the same as `test_job_boards.py`: the registry
keeps producing the same verified targets, and an unknown country narrows the
search rather than refusing it.

The one thing that is different, and worth stating: **this registry cannot make
a price watch correct on its own.** Measured on real search pages, the cheapest
result for "xbox series x" was a headset at ILS 139, a game at ILS 29, and WWE
2K26 at $34.99. Picking between them is `questions.py`'s job. A test here that
asserted "the cheapest offer is the right answer" would be asserting a bug.
"""

import pytest

import shops


def test_a_search_always_produces_at_least_one_shop():
    targets = shops.targets_for("xbox series x", "IL")
    assert targets
    assert all(t["url"].startswith("https://") for t in targets)


def test_amazon_answers_for_every_country():
    for country in ("IL", "US", None, "", "ZZ"):
        names = {t["shop"] for t in shops.targets_for("xbox", country)}
        assert "amazon" in names


def test_israel_gets_its_local_shops_as_well():
    names = {t["shop"] for t in shops.targets_for("xbox", "IL")}
    assert names == {"amazon", "ivory", "bug"}


def test_an_unknown_country_narrows_rather_than_refuses():
    names = {t["shop"] for t in shops.targets_for("xbox", "ZZ")}
    assert names == {"amazon"}


def test_the_country_is_case_and_space_insensitive():
    assert len(shops.targets_for("xbox", " il ")) == 3


def test_a_search_with_nothing_to_search_for_is_refused():
    """A blank query returns a shop's whole front page, and the cheapest thing
    in a shop is never what anyone meant."""
    for blank in ("", "   ", None):
        with pytest.raises(ValueError, match="needs something"):
            shops.targets_for(blank, "IL")


def test_the_query_is_url_encoded():
    target = next(t for t in shops.targets_for('xbox" series', "IL")
                  if t["shop"] == "amazon")
    assert '"' not in target["url"]
    assert " " not in target["url"]


def test_every_shop_uses_the_offers_extractor():
    for target in shops.targets_for("xbox", "IL"):
        assert target["extractor"]["kind"] == "offers"
        assert target["extractor"]["parse"] == "float"


def test_a_shop_publishing_the_standard_carries_no_selector():
    """Ivory emits a JSON-LD ItemList with price, currency, availability and
    sku. A CSS selector cannot reach any of those, so adding one would only
    give the fallback a chance to shadow the better source."""
    ivory = next(t for t in shops.targets_for("xbox", "IL")
                 if t["shop"] == "ivory")
    assert "selector" not in ivory["extractor"]


def test_shops_without_the_standard_carry_a_fallback_selector():
    for name in ("bug", "amazon"):
        target = next(t for t in shops.targets_for("xbox", "IL")
                      if t["shop"] == name)
        assert target["extractor"]["selector"]


def test_only_amazon_needs_a_browser():
    """A browser check is 45x an HTTP one. Amazon earns it by having no other
    way in; a local shop that needed rendering would want a hard look first."""
    by_name = {t["shop"]: t for t in shops.targets_for("xbox", "IL")}
    assert by_name["amazon"]["fetch_method"] == "browser"
    assert by_name["ivory"]["fetch_method"] == "http"
    assert by_name["bug"]["fetch_method"] == "http"


def test_the_currency_a_shop_prices_in_is_recorded():
    """Comparing ILS to USD without saying so is how a watch reports a bargain
    that is not one."""
    by_name = {t["shop"]: t for t in shops.targets_for("xbox", "IL")}
    assert by_name["ivory"]["currency"] == "ILS"
    assert by_name["amazon"]["currency"] == "USD"


def test_the_hint_names_the_shop_and_the_search():
    target = next(t for t in shops.targets_for("xbox series x", "IL")
                  if t["shop"] == "ivory")
    assert "Ivory" in target["extract_hint"]
    assert "xbox series x" in target["extract_hint"]
