"""Text safety helpers for Telegram HTML output.

Telegram's `parse_mode=HTML` accepts a small tag allowlist and counts the
*visible* text (after entity parsing) against the 4096/1024 limits. Tags are
free. `visible_length()` therefore measures what Telegram will measure, which
lets us truncate accurately instead of guessing.
"""

from __future__ import annotations

import html
import re

ELLIPSIS = "…"

_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")

#: A `t.co` short link, with or without its scheme. X wraps every URL it puts in
#: a tweet with this shortener, including the one it appends for an attached
#: photo or video. The trailing punctuation is excluded so a link ending a
#: sentence does not swallow the full stop. The lookbehind is what keeps a
#: `t.co` that appears *inside* another URL's path (`example.com/t.co/x`) from
#: being mistaken for one of X's own short links and then blanked out of it.
_TCO_LINK_RE = re.compile(
    r"(?<![\w./-])(?:https?://)?t\.co/[A-Za-z0-9]+", re.IGNORECASE
)


def escape_html(text: str | None) -> str:
    """Escape user-generated text for Telegram HTML.

    Only `&`, `<` and `>` are special to Telegram; quotes are left alone because
    escaping them would show up literally inside text nodes.
    """
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_markdown_v2(text: str | None) -> str:
    """Escape text for Telegram MarkdownV2.

    Provided for templates/integrations that prefer MarkdownV2; the shipped
    templates use HTML, which has a smaller escaping surface.
    """
    if not text:
        return ""
    specials = r"_*[]()~`>#+-=|{}.!\\"
    return "".join(f"\\{ch}" if ch in specials else ch for ch in text)


def visible_length(markup: str) -> int:
    """Length of `markup` as Telegram will count it: tags stripped, entities decoded."""
    if not markup:
        return 0
    return len(html.unescape(_TAG_RE.sub("", markup)))


def strip_tags(markup: str) -> str:
    return html.unescape(_TAG_RE.sub("", markup or ""))


def collapse_blank_lines(text: str) -> str:
    """Normalise whitespace produced by conditional template blocks."""
    text = _TRAILING_WS_RE.sub("\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def smart_truncate(text: str, limit: int, *, ellipsis: str = ELLIPSIS) -> str:
    """Shorten `text` to at most `limit` characters, preferring a word boundary.

    Never returns more than `limit` characters, including the ellipsis.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(ellipsis):
        return text[:limit]

    budget = limit - len(ellipsis)
    window = text[:budget]
    # Prefer breaking at a paragraph, then a sentence, then a word.
    for separator in ("\n\n", "\n", ". ", " "):
        cut = window.rfind(separator)
        if cut >= budget * 0.6:
            return window[:cut].rstrip() + ellipsis
    return window.rstrip() + ellipsis


def expand_short_links(text: str, mapping: dict[str, str]) -> str:
    """Replace t.co URLs with their expanded targets.

    `mapping` comes from the provider's URL entities. Longest keys are replaced
    first so that `t.co/abc` cannot clobber `t.co/abcdef`.
    """
    if not text or not mapping:
        return text
    for short in sorted(mapping, key=len, reverse=True):
        expanded = mapping[short]
        if expanded:
            text = text.replace(short, expanded)
    return text


def remove_urls(text: str, urls: set[str]) -> str:
    """Drop specific URLs from the text (used for redundant media t.co links)."""
    if not text or not urls:
        return text
    for url in sorted(urls, key=len, reverse=True):
        if url:
            text = text.replace(url, "")
    # Tidy up the whitespace the removal left behind.
    return _TRAILING_WS_RE.sub("\n", text).strip()


def find_tco_links(text: str) -> set[str]:
    """Every `t.co` short link appearing in `text`, exactly as it is written.

    Returned verbatim (scheme included when present) so the result can be fed
    straight to `remove_urls`, which replaces substrings rather than patterns.
    """
    if not text:
        return set()
    return set(_TCO_LINK_RE.findall(text))


def normalise_tweet_text(text: str | None) -> str:
    """Decode provider HTML entities and normalise newlines.

    X returns `&amp;`, `&lt;`, `&gt;` pre-escaped in `text`. We unescape here and
    re-escape at render time so the escaping happens exactly once.
    """
    if not text:
        return ""
    decoded = html.unescape(text)
    return decoded.replace("\r\n", "\n").replace("\r", "\n").strip()


def first_line(text: str, limit: int = 80) -> str:
    """Short single-line excerpt for log lines and list views."""
    flat = " ".join((text or "").split())
    return smart_truncate(flat, limit)
