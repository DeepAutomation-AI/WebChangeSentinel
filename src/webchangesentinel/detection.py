"""Stable HTML extraction and explainable textual / perceptual comparison."""

from __future__ import annotations

import difflib
import hashlib
import html as html_module
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup, Comment, Tag
from lxml import etree
from lxml import html as lxml_html

if TYPE_CHECKING:
    from .config import FilterConfig, MonitorConfig


@dataclass(frozen=True)
class ExtractedContent:
    clean_html: str
    text: str
    content_hash: str


@dataclass(frozen=True)
class ChangeResult:
    changed: bool
    qualifying: bool
    difference_percent: float
    changed_chars: int
    diff: str
    reason: str
    visual_difference_percent: float = 0.0


class ExtractionError(ValueError):
    """A selector was invalid or selected no content."""


def _normalize_text(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_content(html: str, monitor: MonitorConfig) -> ExtractedContent:
    """Strip scripts, styles, comments and ignored CSS nodes before selection.

    CSS selects elements; XPath supports elements, attributes and text nodes.
    Whitespace and attribute ordering are normalized for stable snapshots.
    Regex/case filters apply to comparison, leaving readable snapshot text intact.
    """
    soup = BeautifulSoup(html, "lxml")
    for node in soup.select("script, style, noscript"):
        node.decompose()
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    try:
        for selector in monitor.filters.ignore_selectors:
            for node in soup.select(selector):
                if node.parent is not None:
                    node.decompose()
    except Exception as exc:
        raise ExtractionError("Invalid ignored CSS selector") from exc
    for node in soup.find_all(True):
        node.attrs = dict(sorted(node.attrs.items()))

    selected: list[Tag | str] = []
    if monitor.selector_type == "full":
        selected = [soup.body or soup]
    elif not monitor.selector:
        raise ExtractionError("A nonempty selector is required")
    elif monitor.selector_type == "css":
        try:
            selected = list(soup.select(monitor.selector))
        except Exception as exc:
            raise ExtractionError("Invalid CSS selector") from exc
    elif monitor.selector_type == "xpath":
        try:
            document = lxml_html.fromstring(str(soup))
            matches = document.xpath(monitor.selector)
        except (etree.XPathError, etree.ParserError, ValueError) as exc:
            raise ExtractionError("Invalid XPath selector") from exc
        if not isinstance(matches, list):
            raise ExtractionError("XPath must select elements, text or attributes")
        for match in matches:
            if isinstance(match, etree._Element):
                selected.append(
                    BeautifulSoup(
                        etree.tostring(match, encoding="unicode", method="html", with_tail=False),
                        "html.parser",
                    )
                )
            elif isinstance(match, str):
                selected.append(match)
            else:
                raise ExtractionError("XPath must select elements, text or attributes")
    else:
        raise ExtractionError("Unknown selector type")
    if not selected:
        raise ExtractionError("Selector matched no content")

    html_parts = []
    text_parts = []
    for node in selected:
        if isinstance(node, str):
            html_parts.append(html_module.escape(node))
            text_parts.append(node)
        else:
            # Preserve text spaces in the snapshot while making serialization stable.
            for text_node in list(node.find_all(string=True)):
                normalized = re.sub(r"\s+", " ", str(text_node))
                text_node.replace_with(normalized)
            html_parts.append(str(node))
            text_parts.append(node.get_text(separator="\n", strip=True))
    clean_html = "\n".join(html_parts).strip()
    text = _normalize_text("\n".join(text_parts))
    return ExtractedContent(
        clean_html=clean_html,
        text=text,
        content_hash=hashlib.sha256(clean_html.encode("utf-8")).hexdigest(),
    )


def _filtered_text(text: str, filters: FilterConfig) -> str:
    for pattern in filters.ignore_patterns:
        text = re.sub(pattern, "", text, flags=re.MULTILINE)
    text = _normalize_text(text)
    return text.casefold() if filters.ignore_case else text


def _visual_difference(old_hash: str | None, new_hash: str | None) -> float:
    if not old_hash or not new_hash:
        return 0.0
    if len(old_hash) != len(new_hash):
        raise ValueError("Perceptual hashes must have equal lengths")
    if not re.fullmatch(r"[0-9a-fA-F]+", old_hash + new_hash):
        raise ValueError("Perceptual hashes must contain hexadecimal digits")
    bits = len(old_hash) * 4
    return 100 * (int(old_hash, 16) ^ int(new_hash, 16)).bit_count() / bits


def compare(
    old_text: str,
    new_text: str,
    filters: FilterConfig,
    old_image_hash: str | None = None,
    new_image_hash: str | None = None,
) -> ChangeResult:
    """Compare normalized content and apply alert filters.

    Ignore patterns are Python regexes removed sequentially, with MULTILINE
    enabled; case-insensitive patterns can use ``(?i)``. ``ignore_case`` applies
    afterwards. Difference percent is ``100 * (1 - SequenceMatcher.ratio())``.
    Changed characters count the larger side of each inserted/deleted/replaced
    span. A case-insensitive keyword occurrence must overlap a modified span;
    an unchanged keyword elsewhere on an edited line cannot trigger an alert.

    Perceptual difference is the percentage of differing hash bits. The larger
    textual/visual percentage must meet the threshold. Visual changes can pass
    the character minimum independently, but keyword filters still require a
    matching textual change. Missing either image hash means no visual comparison.
    """
    previous = _filtered_text(old_text, filters)
    current = _filtered_text(new_text, filters)
    matcher = difflib.SequenceMatcher(None, previous, current, autojunk=False)
    textual_percent = 100 * (1 - matcher.ratio())
    visual_percent = _visual_difference(old_image_hash, new_image_hash)
    changed_chars = 0
    previous_changes = []
    current_changes = []
    for operation, i1, i2, j1, j2 in matcher.get_opcodes():
        if operation != "equal":
            changed_chars += max(i2 - i1, j2 - j1)
            previous_changes.append((i1, i2))
            current_changes.append((j1, j2))
    diff = "\n".join(
        difflib.unified_diff(
            previous.splitlines(),
            current.splitlines(),
            fromfile="previous",
            tofile="current",
            lineterm="",
        )
    )
    changed = previous != current or visual_percent > 0
    reason = "threshold_met"
    qualifying = True
    if not changed:
        qualifying, reason = False, "unchanged"
    elif max(textual_percent, visual_percent) < filters.threshold_percent:
        qualifying, reason = False, "below_threshold"
    elif changed_chars < filters.min_changed_chars and visual_percent == 0:
        qualifying, reason = False, "below_min_changed_chars"
    elif filters.keywords and not any(
        match.start() < end and match.end() > start
        for keyword in filters.keywords
        for text, changes in ((previous, previous_changes), (current, current_changes))
        for match in re.finditer(re.escape(keyword), text, flags=re.IGNORECASE)
        for start, end in changes
    ):
        qualifying, reason = False, "keywords_not_matched"
    return ChangeResult(
        changed=changed,
        qualifying=qualifying,
        difference_percent=textual_percent,
        changed_chars=changed_chars,
        diff=diff,
        reason=reason,
        visual_difference_percent=visual_percent,
    )
