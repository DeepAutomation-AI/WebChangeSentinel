"""Extraction and filtering tests assert meaningful user-facing outcomes."""

from types import SimpleNamespace

import pytest

from webchangesentinel.detection import ExtractionError, compare, extract_content


def filters(**updates):
    values = dict(
        ignore_selectors=[],
        ignore_patterns=[],
        ignore_case=False,
        threshold_percent=0,
        min_changed_chars=0,
        keywords=[],
    )
    values.update(updates)
    return SimpleNamespace(**values)


def monitor(**updates):
    values = dict(selector=None, selector_type="full", filters=filters())
    values.update(updates)
    return SimpleNamespace(**values)


def test_css_extraction_removes_dynamic_nodes_and_scripts():
    html = """<main><h1>Price</h1><p id="price">$10 <span class="clock">12:00</span></p>
    <script>alert('noise')</script><style>.noise{}</style><!--noise--></main>"""
    result = extract_content(
        html, monitor(selector="#price", selector_type="css", filters=filters(ignore_selectors=[".clock"]))
    )
    assert result.text == "$10"
    assert "clock" not in result.clean_html
    assert "script" not in result.clean_html
    assert len(result.content_hash) == 64


def test_full_text_cleans_executable_and_hidden_fallback_content():
    result = extract_content(
        "<body><h1> Hello   world </h1><script>noise</script><style>noise</style>"
        "<noscript>noise</noscript><!--noise--><p>Next\tline</p></body>",
        monitor(),
    )
    assert result.text == "Hello world\nNext line"
    assert "noise" not in result.clean_html


@pytest.mark.parametrize("selector", ["//div[@id='target']", "//div[@id='target']/text()"])
def test_xpath_supports_elements_and_text(selector):
    result = extract_content(
        '<div id="other">Other</div><div id="target">Selected text</div>',
        monitor(selector_type="xpath", selector=selector),
    )
    assert result.text == "Selected text"


def test_xpath_attribute_selection():
    result = extract_content(
        '<a href="https://example.test?a=1&amp;b=2">link</a>',
        monitor(selector_type="xpath", selector="//a/@href"),
    )
    assert result.text == "https://example.test?a=1&b=2"
    assert "&amp;" in result.clean_html


@pytest.mark.parametrize(
    "selector_type,selector",
    [("css", ".missing"), ("css", "[broken"), ("css", ""), ("xpath", "//*["), ("xpath", "//missing"), ("xpath", "count(//p)")],
)
def test_invalid_or_empty_selectors_fail_without_erasing_baseline(selector_type, selector):
    with pytest.raises(ExtractionError):
        extract_content("<p>Content</p>", monitor(selector_type=selector_type, selector=selector))


def test_hash_is_stable_for_attribute_order_and_whitespace():
    config = monitor(selector_type="css", selector="p")
    first = extract_content('<p id="x" class="price">Hello   world</p>', config)
    second = extract_content('<p class="price" id="x">Hello world</p>', config)
    assert first.content_hash == second.content_hash
    assert first.clean_html == second.clean_html


def test_text_change_returns_percent_count_and_unified_diff():
    result = compare("abcd", "abXd", filters())
    assert result.changed and result.qualifying
    assert result.difference_percent == pytest.approx(25)
    assert result.changed_chars == 1
    assert "-abcd" in result.diff and "+abXd" in result.diff
    assert result.reason == "threshold_met"


def test_regex_and_case_filters_suppress_only_ignored_changes():
    config = filters(ignore_patterns=[r"\d{2}:\d{2}"], ignore_case=True)
    result = compare("PRICE 10\nUpdated 12:00", "price 10\nUpdated 13:55", config)
    assert not result.changed and not result.qualifying
    assert result.reason == "unchanged"
    meaningful = compare("PRICE 10\nUpdated 12:00", "price 20\nUpdated 13:55", config)
    assert meaningful.changed and meaningful.qualifying


def test_multiline_ignore_pattern_removes_timestamp_lines():
    result = compare(
        "Product\nGenerated at 12:00\nPrice 10",
        "Product\nGenerated at 13:00\nPrice 10",
        filters(ignore_patterns=[r"^Generated at .*$"]),
    )
    assert not result.changed


def test_threshold_and_character_minimum_filter_minor_changes():
    threshold = compare("abcd", "abXd", filters(threshold_percent=26))
    assert threshold.changed and not threshold.qualifying
    assert threshold.reason == "below_threshold"
    minimum = compare("abcd", "abXd", filters(min_changed_chars=2))
    assert minimum.changed and not minimum.qualifying
    assert minimum.reason == "below_min_changed_chars"


def test_unchanged_keywords_in_context_do_not_trigger():
    result = compare("urgent: price 10", "urgent: price 20", filters(keywords=["urgent"]))
    assert result.changed and not result.qualifying
    assert result.reason == "keywords_not_matched"


@pytest.mark.parametrize("old,new", [("Sold out", "Available"), ("AVAILABLE", "Sold out")])
def test_added_or_removed_keywords_match_complete_changed_words(old, new):
    result = compare(old, new, filters(keywords=["available"]))
    assert result.qualifying


def test_visual_difference_can_qualify_with_identical_text():
    result = compare(
        "Unchanged", "Unchanged", filters(threshold_percent=50, min_changed_chars=100), "0000", "ffff"
    )
    assert result.changed and result.qualifying
    assert result.visual_difference_percent == 100
    assert result.difference_percent == 0
    assert result.changed_chars == 0
    assert result.diff == ""


def test_visual_only_change_does_not_match_an_unchanged_keyword():
    result = compare("Available", "Available", filters(keywords=["available"]), "0000", "ffff")
    assert result.changed and not result.qualifying
    assert result.reason == "keywords_not_matched"


def test_missing_visual_baseline_does_not_invent_a_change():
    result = compare("Same", "Same", filters(), None, "ffff")
    assert not result.changed
    assert result.visual_difference_percent == 0


@pytest.mark.parametrize("old,new", [("00", "000"), ("zzzz", "ffff")])
def test_invalid_image_hashes_fail_explicitly(old, new):
    with pytest.raises(ValueError):
        compare("Same", "Same", filters(), old, new)
