"""Post rendering: one tweet in, one Telegram message plan out.

## Why truncation happens on plain text

The obvious implementation — render the markup, then cut it to 1024 characters —
is broken in a way that only shows up in production: cutting at an arbitrary
offset can slice through `<a href="...">`, and Telegram then rejects the entire
message with "can't parse entities". Admins see a publish fail for a reason that
has nothing to do with the tweet.

So the fixed blocks (header, author name, channel footer) are rendered
first, their *visible* length is measured with `visible_length()`, and whatever
budget is left is handed to `smart_truncate()` applied to the **plain** text.
Escaping happens after truncation. A cut can therefore never land inside a tag,
because there is no tag in the text being cut.

## Why the caption limit is 1024 and not 4096

Telegram allows 4096 characters in a text message but only 1024 in a media
caption. A post with photos therefore has a third of the budget, and a long post
cannot simply be sent as a caption. `LONG_CAPTION_MODE` decides what gives:
truncate the caption, or send the text as its own message alongside the media.

## What is deliberately absent

The author's @handle, the link back to the original post, and the tweet's
publication time are never rendered. A relay post carries the text, the media and
the channel's own footer — nothing that points a reader back at the source.

Metrics (likes/reposts) are fetched and stored but not rendered by the shipped
templates — they go stale within minutes of publishing, and a channel post
advertising a like count that no longer matches the source looks worse than one
that omits it. They remain in `tweets.payload` for a template that wants them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from app.config import LongCaptionMode, Settings, TemplateName
from app.constants import MAX_CAPTION_LENGTH, MAX_MESSAGE_LENGTH
from app.logging_config import get_logger
from app.providers.base import TweetData
from app.utils.text import (
    collapse_blank_lines,
    escape_html,
    expand_short_links,
    find_tco_links,
    normalise_tweet_text,
    remove_urls,
    smart_truncate,
    visible_length,
)

log = get_logger(__name__)

#: Below this many visible characters of body text, a post is not worth
#: publishing at all. Used to detect "the fixed blocks ate the whole budget",
#: which means the template is misconfigured rather than the tweet being short.
_MIN_BODY_BUDGET = 20

Order = Literal["single", "text_first", "media_first"]


@dataclass(frozen=True, slots=True)
class RenderedPost:
    """The message plan. `caption` is always what accompanies media (or the
    whole post when there is none); `text_message` is the optional companion."""

    caption: str
    order: Order
    text_message: str | None = None
    photo_count: int = 0
    video_count: int = 0
    skipped_media_count: int = 0
    truncated: bool = False

    @property
    def media_count(self) -> int:
        return self.photo_count + self.video_count

    @property
    def is_text_only(self) -> bool:
        return self.media_count == 0

    @property
    def caption_length(self) -> int:
        return visible_length(self.caption)


# --------------------------------------------------------------------- helpers
def _author_line(tweet: TweetData, settings: Settings) -> str:
    """The bolded byline, e.g. `🐦 **Alice**`.

    Only the display name is rendered — the @handle is deliberately left out. A
    provider that omits the display name (a third of real ones do) yields no
    byline at all, rather than falling back to the handle or to "Unknown author":
    both would put an identity on the post the operator asked not to have.
    """
    name = tweet.author_name
    if not name:
        return ""

    # Values are escaped before substitution; the format string itself is
    # trusted operator markup and is not.
    values = {
        "name": escape_html(name),
        "handle": "",
        "username": "",
        "id": escape_html(tweet.author_id or ""),
    }

    try:
        formatted = settings.TEMPLATE_AUTHOR_FORMAT.format(**values)
    except (KeyError, IndexError, ValueError):
        # An operator typo in TEMPLATE_AUTHOR_FORMAT must not fail a publish.
        log.warning(
            "TEMPLATE_AUTHOR_FORMAT could not be applied; using '{name}'",
            extra={"template": settings.TEMPLATE_AUTHOR_FORMAT[:80]},
        )
        formatted = values["name"]

    formatted = collapse_blank_lines(formatted).strip()
    if not formatted:
        return ""
    emoji = settings.TEMPLATE_EMOJI_AUTHOR
    return f"{emoji} <b>{formatted}</b>" if emoji else f"<b>{formatted}</b>"


def _channel_footer(settings: Settings) -> str:
    """Our channel's identity, appended to the bottom of every published post.

    Both lines are optional and neither is invented: a private channel with no
    configured name renders nothing here. The name and handle are escaped like
    any other value; only the configured emoji and the `<b>` wrapper are markup.
    """
    lines: list[str] = []

    name = settings.TELEGRAM_CHANNEL_NAME.strip()
    if name:
        rendered = f"<b>{escape_html(name)}</b>"
        emoji = settings.TEMPLATE_EMOJI_CHANNEL
        lines.append(f"{emoji} {rendered}" if emoji else rendered)

    handle = settings.channel_handle
    if handle:
        lines.append(f"@{escape_html(handle)}")

    return "\n".join(lines)


def _required_post(settings: Settings, footer: bool) -> list[str]:
    """The trailing block that must never be dropped: our channel identity."""
    if not footer:
        return []
    block = _channel_footer(settings)
    return [block] if block else []


def _is_self_link(url: str, tweet_id: str) -> bool:
    """True when `url` points back at this same post.

    X's media entities carry the tweet's own permalink (`…/status/<id>/photo/1`)
    as their `expanded_url`. Rendering that would put the source link — which the
    operator deliberately removed — back into the post under a shortened link.
    """
    if not tweet_id:
        return False
    path = urlsplit(url).path
    return bool(re.search(rf"/status(?:es)?/{re.escape(tweet_id)}(?:/|$)", path))


def _media_short_links(tweet: TweetData, text: str) -> set[str]:
    """The `t.co` links in `text` that stand for the tweet's own attachments.

    A tweet with media appends one short link per attachment to its own text,
    and that link is *not* part of what the author wrote. It is also absent from
    `entities.urls`, so `expand_short_links` leaves it untouched and the post
    otherwise ends with a bare `t.co` URL. A text URL is always present in
    `entities.urls`, so "in the text but not in `url_map`" is what distinguishes
    the redundant media link from a legitimate one.

    The second case is a provider that *does* list the media link as a URL
    entity, with the tweet's own permalink as its expansion. That link is
    dropped for the same reason: it points at the source, not at anything the
    author wrote.
    """
    mapping = {
        key.split("://", 1)[-1].lower(): value for key, value in tweet.url_map.items()
    }
    dropped: set[str] = set()
    for short in find_tco_links(text):
        target = mapping.get(short.split("://", 1)[-1].lower())
        if target is None or _is_self_link(target, tweet.tweet_id):
            dropped.add(short)
    return dropped


def _body_text(tweet: TweetData) -> str:
    """The tweet's own text, expanded and normalised, still unescaped."""
    text = normalise_tweet_text(tweet.text)

    # Only a tweet that carries media can carry a redundant media link, and the
    # removal is scoped to `t.co` links for exactly that reason: an ordinary URL
    # in the text is never a candidate.
    if tweet.media:
        redundant = _media_short_links(tweet, text)
        if redundant:
            text = remove_urls(text, redundant)

    if tweet.url_map:
        text = expand_short_links(text, dict(tweet.url_map))
    return collapse_blank_lines(text)


def _optional_blocks(settings: Settings) -> tuple[list[str], list[str]]:
    """`(header, footer)` blocks, either of which may be empty.

    Both come from configuration, so they are treated as trusted markup and are
    *not* escaped — that is what lets an operator put `<i>` or a link in the
    footer. The values substituted into them are not.
    """
    header = settings.TEMPLATE_HEADER.strip()
    footer_parts = [settings.TEMPLATE_FOOTER.strip()]
    if settings.INCLUDE_SIGNATURE and settings.TEMPLATE_SIGNATURE.strip():
        footer_parts.append(settings.TEMPLATE_SIGNATURE.strip())
    footer = collapse_blank_lines("\n".join(p for p in footer_parts if p))
    return ([header] if header else []), ([footer] if footer else [])


def _budget(pre: list[str], post: list[str], limit: int, has_body: bool) -> int:
    """Visible characters left for the body once the fixed blocks are placed.

    Each adjacent pair of blocks is joined by a blank line, which costs two
    visible characters that Telegram counts. Omitting that term makes long posts
    overflow by exactly the number of blocks, which is the kind of off-by-a-little
    that only fails on the longest input.
    """
    blocks = [b for b in (*pre, *post) if b]
    overhead = sum(visible_length(b) for b in blocks)
    count = len(blocks) + (1 if has_body else 0)
    separators = 2 * max(0, count - 1)
    return limit - overhead - separators


def _compose(
    *,
    optional_pre: list[str],
    required_pre: list[str],
    body_plain: str,
    required_post: list[str],
    optional_post: list[str],
    limit: int,
) -> tuple[str, bool]:
    """Assemble the message, shedding optional blocks only if it will not fit.

    Order of sacrifice is header, then footer. The byline and the channel
    identity footer are never dropped: a post that does not say where it came
    from is worse than one that is merely truncated, and our own footer is the
    one piece of the post that is not the tweet's.
    """
    variants = [
        (optional_pre, optional_post),
        ([], optional_post),  # drop the header
        ([], []),  # drop the footer too
    ]

    has_body = bool(body_plain)
    last = ""
    truncated = False

    for pre_extra, post_extra in variants:
        pre = [*pre_extra, *required_pre]
        post = [*required_post, *post_extra]
        budget = _budget(pre, post, limit, has_body)

        if has_body and budget < _MIN_BODY_BUDGET and (pre_extra or post_extra):
            # This variant leaves no room for the text; try a leaner one.
            continue

        body = smart_truncate(body_plain, budget) if has_body else ""
        blocks = [b for b in [*pre, escape_html(body) if body else "", *post] if b]
        last = collapse_blank_lines("\n\n".join(blocks))
        truncated = len(body) < len(body_plain)

        if visible_length(last) <= limit:
            return last, truncated

    # Every variant overflowed. Returning the last one is better than raising:
    # the message is over-length by a few characters at most, and Telegram's
    # rejection is a clearer signal than a renderer crash mid-publish.
    log.warning(
        "rendered post exceeds the limit even after dropping optional blocks",
        extra={"limit": limit, "actual": visible_length(last)},
    )
    return last, truncated


# -------------------------------------------------------------------- builders
def _rich(
    tweet: TweetData, settings: Settings, body_plain: str, limit: int,
    *, footer: bool = True,
) -> tuple[str, bool]:
    optional_pre, optional_post = _optional_blocks(settings)
    return _compose(
        optional_pre=optional_pre,
        required_pre=[_author_line(tweet, settings)],
        body_plain=body_plain,
        required_post=_required_post(settings, footer),
        optional_post=optional_post,
        limit=limit,
    )


def _minimal_byline(tweet: TweetData) -> str:
    """Bold display name only — the same removals as `_author_line`, plainer."""
    name = tweet.author_name
    return f"<b>{escape_html(name)}</b>" if name else ""


def _minimal(
    tweet: TweetData, settings: Settings, body_plain: str, limit: int,
    *, footer: bool = True,
) -> tuple[str, bool]:
    """Author, text, channel footer. No header, no link, no timestamp.

    Survives a 1024-character caption budget far better than `rich`, which is why
    it is the default companion caption when media is present.
    """
    return _compose(
        optional_pre=[],
        required_pre=[_minimal_byline(tweet)],
        body_plain=body_plain,
        required_post=_required_post(settings, footer),
        optional_post=[],
        limit=limit,
    )


def _custom(
    tweet: TweetData, settings: Settings, body_plain: str, limit: int,
    *, footer: bool = True,
) -> tuple[str, bool]:
    """Operator-supplied template, split around its `{text}` placeholder.

    Splitting on the placeholder — rather than substituting into the finished
    string — is what keeps the truncation safe: `{text}` lands in the body slot,
    so it is still plain text when the budget is applied.

    A template with no `{text}` cannot have its body truncated, and the tweet text
    would be silently dropped. The placeholder is therefore appended when missing,
    with a warning, rather than publishing a post with no post in it.
    """
    template = settings.TEMPLATE_CUSTOM
    if "{text}" not in template:
        log.warning(
            "TEMPLATE_CUSTOM has no {text} placeholder; the tweet text was appended",
            extra={"template_length": len(template)},
        )
        template = f"{template}\n\n{{text}}"

    before, _, after = template.partition("{text}")

    def expand(part: str) -> str:
        values = {
            "author": _author_line(tweet, settings),
            # The source link and timestamp are no longer rendered. They stay as
            # empty strings so a template that still contains `{link}` or `{time}`
            # degrades to nothing rather than publishing the literal placeholder.
            "link": "",
            "time": "",
            "header": settings.TEMPLATE_HEADER.strip(),
            "footer": settings.TEMPLATE_FOOTER.strip(),
            "signature": settings.TEMPLATE_SIGNATURE.strip() if settings.INCLUDE_SIGNATURE else "",
        }
        # `str.replace` rather than `.format`: an operator template may legitimately
        # contain braces (JSON, code samples) and `.format` would raise on them.
        for key, value in values.items():
            part = part.replace(f"{{{key}}}", value)
        return collapse_blank_lines(part)

    pre = [block for block in (expand(before),) if block]
    post = [block for block in (expand(after),) if block]
    if footer:
        # Appended rather than substituted into `{footer}`: the channel identity
        # must reach the bottom of the post whatever the operator's template does.
        channel = _channel_footer(settings)
        if channel:
            post = [*post, channel]
    return _compose(
        optional_pre=[],
        required_pre=pre,
        body_plain=body_plain,
        required_post=post,
        optional_post=[],
        limit=limit,
    )


_BUILDERS = {
    TemplateName.RICH: _rich,
    TemplateName.MINIMAL: _minimal,
    TemplateName.CUSTOM: _custom,
}


# ----------------------------------------------------------------- public API
def render_post(
    tweet: TweetData, settings: Settings, *, template: TemplateName | None = None
) -> RenderedPost:
    """Render `tweet` into the messages that should be sent.

    Photos and video are both published. A video whose `remote_url` is empty is
    one the provider described but offered no playable rendition for; it is not
    counted as published, so it lands in `skipped_media_count` for the admin
    receipt to surface. `ALLOW_VIDEO=false` omits video the same way.
    """
    name = template or settings.POST_TEMPLATE
    builder = _BUILDERS.get(name, _rich)

    body_plain = _body_text(tweet)
    photos = tuple(m for m in tweet.photos if m.remote_url)
    videos = (
        tuple(m for m in tweet.videos if m.remote_url) if settings.ALLOW_VIDEO else ()
    )
    photo_count = len(photos)
    video_count = len(videos)
    skipped = len(tweet.media) - photo_count - video_count

    if photo_count + video_count == 0:
        caption, truncated = builder(tweet, settings, body_plain, MAX_MESSAGE_LENGTH)
        return RenderedPost(
            caption=caption,
            order="single",
            photo_count=0,
            video_count=0,
            skipped_media_count=skipped,
            truncated=truncated,
        )

    # With media, try the whole post as a caption first — for the vast majority of
    # tweets it fits and the result is one clean message. Telegram's caption limit
    # is the same 1024 for photos and video.
    caption, truncated = builder(tweet, settings, body_plain, MAX_CAPTION_LENGTH)
    if visible_length(caption) <= MAX_CAPTION_LENGTH and not truncated:
        return RenderedPost(
            caption=caption,
            order="single",
            photo_count=photo_count,
            video_count=video_count,
            skipped_media_count=skipped,
        )

    mode = settings.LONG_CAPTION_MODE

    if mode is LongCaptionMode.TRUNCATE:
        return RenderedPost(
            caption=caption,
            order="single",
            photo_count=photo_count,
            video_count=video_count,
            skipped_media_count=skipped,
            truncated=True,
        )

    # The text has to live somewhere else. It gets the full text-message budget,
    # and the media carries only the short byline caption. Whichever message
    # lands last carries the channel footer, so it appears once per post.
    text_first = mode is LongCaptionMode.TEXT_FIRST
    text_message, text_truncated = builder(
        tweet, settings, body_plain, MAX_MESSAGE_LENGTH, footer=not text_first
    )
    short_caption, _ = _minimal(
        tweet, settings, "", MAX_CAPTION_LENGTH, footer=text_first
    )

    return RenderedPost(
        caption=short_caption,
        order="text_first" if text_first else "media_first",
        text_message=text_message,
        photo_count=photo_count,
        video_count=video_count,
        skipped_media_count=skipped,
        truncated=text_truncated,
    )


def render_preview(
    tweet: TweetData, settings: Settings, *, already_published: bool = False
) -> str:
    """The admin-facing message shown while a post is being relayed.

    This is *not* the channel post: it is wrapped in a labelled blockquote and
    carries the operational facts (media count, what was skipped, whether the text
    had to be truncated). There are no buttons — the link the admin sent is the
    approval, so this is a receipt, not a confirmation prompt.
    """
    rendered = render_post(tweet, settings)
    notes: list[str] = []

    media_bits: list[str] = []
    if rendered.photo_count:
        media_bits.append(f"{rendered.photo_count} photo(s)")
    if rendered.video_count:
        media_bits.append(f"{rendered.video_count} video(s)")
    notes.append(" + ".join(media_bits) if media_bits else "no media")
    if rendered.skipped_media_count:
        notes.append(f"⚠️ {rendered.skipped_media_count} unsupported item(s) will be omitted")
    if rendered.truncated:
        notes.append("⚠️ text will be truncated")
    if rendered.order == "text_first":
        notes.append("text will be sent as a separate message first")
    elif rendered.order == "media_first":
        notes.append("text will be sent as a separate message after the media")
    if already_published:
        notes.append("🚫 already published to the channel")

    heading = (
        "<b>Already published</b> — this post is already in the channel"
        if already_published
        else "<b>Publishing…</b> — this is going to the channel"
    )
    facts = escape_html(" · ".join(notes))
    return collapse_blank_lines(
        f"{heading}\n"
        f"<blockquote>{rendered.caption}</blockquote>\n"
        f"<i>{facts}</i>"
    )
