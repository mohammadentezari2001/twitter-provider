"""Renderer tests — the message that actually reaches the channel.

The renderer is where a bug is most expensive: a malformed tag makes Telegram
reject the whole publish, and an unescaped `<` in a tweet is a (small) injection
vector. So the assertions here are less about "does it look right" and more about
two structural invariants checked on every output:

* **Well-formed markup.** Every tag opened is closed, in order, and no `<` or `>`
  survives outside a tag.
* **Truncation happened before escaping.** If the cut were applied to the
  *escaped* string it could land inside `&amp;`, producing `&am` — valid-looking
  text, but the wrong post, and invisible in a casual read. A regex for a bare
  `&` catches it.

`visible_length` is Telegram's own measure (tags stripped, entities decoded), so
the length assertions are meaningful rather than approximate.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html.parser import HTMLParser

import pytest

from app.config import LongCaptionMode, Settings, TemplateName
from app.constants import MAX_CAPTION_LENGTH, MAX_MESSAGE_LENGTH
from app.database.enums import MediaKind
from app.providers.base import MediaData, TweetData
from app.services.render import render_post, render_preview
from app.utils.text import strip_tags, visible_length

POSTED_AT = datetime(2026, 3, 1, 12, 30, tzinfo=UTC)

#: An `&` that is not the start of a valid entity — the signature of a cut that
#: landed inside one.
_BARE_AMPERSAND = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")


class _Balance(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            self.errors.append(f"</{tag}> with nothing open")
        elif self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes <{self.stack[-1]}>")
            self.stack.pop()
        else:
            self.stack.pop()


def assert_well_formed(markup: str) -> None:
    """Every structural invariant the renderer promises, in one call."""
    balance = _Balance()
    balance.feed(markup)
    assert not balance.errors, f"unbalanced tags in {markup!r}: {balance.errors}"
    assert not balance.stack, f"unclosed tags in {markup!r}: {balance.stack}"

    plain = strip_tags(markup)
    assert "<" not in plain, f"a stray '<' survived into the text: {plain!r}"
    assert ">" not in plain, f"a stray '>' survived into the text: {plain!r}"
    assert not _BARE_AMPERSAND.search(markup), f"truncation cut an HTML entity: {markup[-80:]!r}"


def make_tweet(**overrides: object) -> TweetData:
    base: dict[str, object] = {
        "tweet_id": "1234567890",
        "text": "Hello world",
        "author_username": "alice",
        "author_name": "Alice",
        "author_id": "42",
        "posted_at": POSTED_AT,
    }
    base.update(overrides)
    return TweetData(**base)  # type: ignore[arg-type]


def photo(position: int = 0) -> MediaData:
    return MediaData(
        position=position,
        kind=MediaKind.PHOTO,
        remote_url=f"https://pbs.twimg.com/media/{position}.jpg",
        content_type="image/jpeg",
        width=1200,
        height=800,
    )


def video(position: int = 0) -> MediaData:
    return MediaData(
        position=position,
        kind=MediaKind.VIDEO,
        remote_url=f"https://video.twimg.com/{position}.mp4",
        content_type="video/mp4",
    )


def with_settings(base: Settings, **updates: object) -> Settings:
    """Vary one setting without re-validating the whole model.

    `model_copy` skips validation on purpose: these tests are about the renderer,
    and a cross-field rule firing here would be testing pydantic instead.
    """
    return base.model_copy(update=updates)


# ------------------------------------------------------------------- text only
def test_plain_tweet_renders_as_one_text_message(settings: Settings) -> None:
    rendered = render_post(make_tweet(text="Just a short post."), settings)

    assert rendered.order == "single"
    assert rendered.text_message is None
    assert rendered.is_text_only
    assert not rendered.truncated
    assert "Just a short post." in rendered.caption
    # The display name stays; the @handle is deliberately gone.
    assert "Alice" in rendered.caption
    assert "@alice" not in rendered.caption
    assert_well_formed(rendered.caption)


def test_the_handle_link_and_timestamp_are_never_rendered(settings: Settings) -> None:
    """The three removals, asserted against the message that reaches the channel."""
    rendered = render_post(make_tweet(), settings)

    assert "@alice" not in rendered.caption
    assert "x.com" not in rendered.caption
    assert "twitter.com" not in rendered.caption
    assert "<a href" not in rendered.caption
    assert "2026-03-01" not in rendered.caption
    assert_well_formed(rendered.caption)


def test_markup_in_the_tweet_is_escaped_not_interpreted(settings: Settings) -> None:
    """A tweet is untrusted input, even though it comes from a provider."""
    rendered = render_post(
        make_tweet(text="<b>not bold</b> & <a href='https://evil'>link</a>"), settings
    )

    assert "&lt;b&gt;not bold&lt;/b&gt;" in rendered.caption
    assert "<b>not bold</b>" not in rendered.caption
    assert "&amp;" in rendered.caption
    assert_well_formed(rendered.caption)


def test_html_entities_from_the_provider_are_not_double_escaped(settings: Settings) -> None:
    """X returns `&amp;` pre-escaped; it must display as a single `&`."""
    rendered = render_post(make_tweet(text="tea &amp; coffee"), settings)

    assert "tea &amp; coffee" in rendered.caption
    assert "&amp;amp;" not in rendered.caption


def test_short_links_are_expanded(settings: Settings) -> None:
    rendered = render_post(
        make_tweet(
            text="read this https://t.co/abc123",
            url_map={"https://t.co/abc123": "https://example.com/article"},
        ),
        settings,
    )

    assert "https://example.com/article" in rendered.caption
    assert "t.co/abc123" not in rendered.caption


# ------------------------------------------------------- redundant media links
def test_the_media_short_link_is_dropped_from_a_photo_post(settings: Settings) -> None:
    """X appends one `t.co` link per attachment to the tweet's own text.

    That link is not part of what the author wrote, and it is not in
    `entities.urls`, so expanding short links leaves it in place and the post
    would otherwise end with a bare `t.co` URL.
    """
    rendered = render_post(
        make_tweet(text="sunset over the bay https://t.co/MEDIA1", media=(photo(0),)),
        settings,
    )

    assert "sunset over the bay" in rendered.caption
    assert "t.co" not in rendered.caption
    assert "MEDIA1" not in rendered.caption


def test_the_media_short_link_is_dropped_from_a_video_post(settings: Settings) -> None:
    rendered = render_post(
        make_tweet(text="watch this https://t.co/VIDEO1", media=(video(0),)), settings
    )

    assert "watch this" in rendered.caption
    assert "t.co" not in rendered.caption
    assert rendered.video_count == 1


def test_a_legitimate_link_survives_alongside_the_media_link(settings: Settings) -> None:
    """The removal is scoped to the shortener, not to URLs in general."""
    rendered = render_post(
        make_tweet(
            text="read https://t.co/abc123 and see https://t.co/MEDIA1",
            media=(photo(0),),
            url_map={"https://t.co/abc123": "https://example.com/article"},
        ),
        settings,
    )

    assert "https://example.com/article" in rendered.caption
    assert "MEDIA1" not in rendered.caption
    assert "t.co/abc123" not in rendered.caption


def test_a_media_link_expanded_to_the_tweets_own_permalink_is_dropped(
    settings: Settings,
) -> None:
    """Some providers list the media link as a URL entity.

    Its expansion is the tweet's own permalink, which the operator deliberately
    removed — so it must not come back in through the entity table.
    """
    rendered = render_post(
        make_tweet(
            text="a picture https://t.co/self1",
            media=(photo(0),),
            url_map={"https://t.co/self1": "https://x.com/alice/status/1234567890/photo/1"},
        ),
        settings,
    )

    assert "a picture" in rendered.caption
    assert "x.com" not in rendered.caption
    assert "t.co" not in rendered.caption


def test_an_unexpanded_short_link_without_media_is_left_alone(settings: Settings) -> None:
    """Only a tweet that carries media can carry a redundant media link.

    Removing `t.co` links unconditionally would delete a legitimate URL whenever
    a provider omitted its entity table.
    """
    rendered = render_post(
        make_tweet(text="read this https://t.co/abc123"), settings
    )

    assert "https://t.co/abc123" in rendered.caption


def test_a_url_that_merely_contains_t_co_is_not_mistaken_for_a_short_link(
    settings: Settings,
) -> None:
    """`example.com/t.co/x` is somebody's URL, not X's shortener, and blanking
    the `t.co/x` out of the middle of it would corrupt the link."""
    rendered = render_post(
        make_tweet(text="spec: https://example.com/t.co/spec", media=(photo(0),)),
        settings,
    )

    assert "https://example.com/t.co/spec" in rendered.caption


# -------------------------------------------------------------------- truncation
def test_long_text_is_truncated_within_the_message_limit(settings: Settings) -> None:
    rendered = render_post(make_tweet(text="word " * 4000), settings)

    assert rendered.truncated
    assert visible_length(rendered.caption) <= MAX_MESSAGE_LENGTH
    assert rendered.caption.endswith("…")
    assert_well_formed(rendered.caption)


def test_truncation_never_cuts_through_an_escaped_entity(settings: Settings) -> None:
    """The regression this guards against is subtle and silent.

    Escaping first and truncating second can leave `&am` at the end of the post:
    still valid HTML, still accepted by Telegram, and quietly wrong. The
    ampersand regex in `assert_well_formed` is what makes that fail here.
    """
    rendered = render_post(make_tweet(text="a & b " * 2000), settings)

    assert rendered.truncated
    assert "&amp;" in rendered.caption
    assert_well_formed(rendered.caption)


@pytest.mark.parametrize("size", [1, 10, 100, 1000])
def test_truncation_holds_at_every_length(settings: Settings, size: int) -> None:
    rendered = render_post(make_tweet(text="x & y " * size), settings)

    assert visible_length(rendered.caption) <= MAX_MESSAGE_LENGTH
    assert_well_formed(rendered.caption)


def test_the_byline_survives_truncation(settings: Settings) -> None:
    """Losing the author is worse than losing the tail of the text."""
    rendered = render_post(make_tweet(text="z" * 9000), settings)

    assert "Alice" in rendered.caption
    assert "@alice" not in rendered.caption
    assert_well_formed(rendered.caption)


# ------------------------------------------------------------------------ media
def test_short_post_with_photos_fits_in_a_single_caption(settings: Settings) -> None:
    rendered = render_post(make_tweet(text="Two photos", media=(photo(0), photo(1))), settings)

    assert rendered.order == "single"
    assert rendered.photo_count == 2
    assert rendered.text_message is None
    assert not rendered.truncated
    assert_well_formed(rendered.caption)


def test_long_post_with_photos_respects_the_caption_limit(settings: Settings) -> None:
    rendered = render_post(
        make_tweet(text="word " * 4000, media=(photo(0),)), settings
    )

    assert visible_length(rendered.caption) <= MAX_CAPTION_LENGTH
    assert rendered.truncated
    assert_well_formed(rendered.caption)


def test_text_first_moves_the_text_into_its_own_message(settings: Settings) -> None:
    long_post = make_tweet(text="word " * 4000, media=(photo(0),))
    rendered = render_post(
        with_settings(settings, LONG_CAPTION_MODE=LongCaptionMode.TEXT_FIRST), long_post
    )

    assert rendered.order == "text_first"
    assert rendered.text_message is not None
    assert visible_length(rendered.text_message) <= MAX_MESSAGE_LENGTH
    assert visible_length(rendered.caption) <= MAX_CAPTION_LENGTH
    assert_well_formed(rendered.caption)
    assert_well_formed(rendered.text_message)


def test_media_first_moves_the_text_after_the_media(settings: Settings) -> None:
    long_post = make_tweet(text="word " * 4000, media=(photo(0),))
    rendered = render_post(
        with_settings(settings, LONG_CAPTION_MODE=LongCaptionMode.MEDIA_FIRST), long_post
    )

    assert rendered.order == "media_first"
    assert rendered.text_message is not None


def test_truncate_mode_keeps_a_single_message(settings: Settings) -> None:
    long_post = make_tweet(text="word " * 4000, media=(photo(0),))
    rendered = render_post(
        with_settings(settings, LONG_CAPTION_MODE=LongCaptionMode.TRUNCATE), long_post
    )

    assert rendered.order == "single"
    assert rendered.text_message is None
    assert visible_length(rendered.caption) <= MAX_CAPTION_LENGTH


def test_video_is_published_not_reported_as_skipped(settings: Settings) -> None:
    rendered = render_post(make_tweet(text="watch this", media=(video(0),)), settings)

    assert rendered.video_count == 1
    assert rendered.photo_count == 0
    assert rendered.skipped_media_count == 0
    assert not rendered.is_text_only
    assert visible_length(rendered.caption) <= MAX_CAPTION_LENGTH


def test_video_is_reported_as_skipped_when_it_is_disabled(settings: Settings) -> None:
    """With `ALLOW_VIDEO=false` the old behaviour stands, and the receipt says so."""
    rendered = render_post(
        make_tweet(text="watch this", media=(video(0),)),
        with_settings(settings, ALLOW_VIDEO=False),
    )

    assert rendered.photo_count == 0
    assert rendered.video_count == 0
    assert rendered.skipped_media_count == 1
    assert rendered.is_text_only


def test_a_video_with_no_playable_url_is_skipped_rather_than_fetched(
    settings: Settings,
) -> None:
    """A provider that described a video but offered no MP4 lands in the count."""
    unplayable = MediaData(
        position=0, kind=MediaKind.VIDEO, remote_url="", content_type="video/mp4"
    )
    rendered = render_post(make_tweet(text="watch this", media=(unplayable,)), settings)

    assert rendered.video_count == 0
    assert rendered.skipped_media_count == 1


def test_a_mixed_post_counts_photos_and_video_separately(settings: Settings) -> None:
    rendered = render_post(
        make_tweet(text="mixed", media=(photo(0), video(1), photo(2))), settings
    )

    assert rendered.photo_count == 2
    assert rendered.video_count == 1
    assert rendered.skipped_media_count == 0


# --------------------------------------------------------------------- bylines
def test_a_missing_display_name_renders_no_byline_at_all(settings: Settings) -> None:
    """With the handle removed there is nothing to fall back to — and the handle
    must not be used as the fallback, which is the whole point of the change."""
    rendered = render_post(make_tweet(author_name=None), settings)

    assert "Hello world" in rendered.caption
    assert "@alice" not in rendered.caption
    assert "Alice" not in rendered.caption
    assert "None" not in rendered.caption
    assert "Unknown author" not in rendered.caption


def test_a_missing_handle_does_not_change_the_byline(settings: Settings) -> None:
    rendered = render_post(make_tweet(author_username=None), settings)

    assert "Alice" in rendered.caption
    assert "None" not in rendered.caption


def test_a_tweet_with_no_author_at_all_still_renders(settings: Settings) -> None:
    rendered = render_post(make_tweet(author_username=None, author_name=None), settings)

    assert "Hello world" in rendered.caption
    assert "None" not in rendered.caption
    assert "@" not in rendered.caption
    assert_well_formed(rendered.caption)


def test_markup_in_the_author_name_is_escaped(settings: Settings) -> None:
    """The author name is untrusted input, like the tweet text."""
    rendered = render_post(make_tweet(author_name="<b>Eve</b>"), settings)

    assert "&lt;b&gt;Eve&lt;/b&gt;" in rendered.caption
    assert "Eve" in rendered.caption
    assert_well_formed(rendered.caption)


def test_a_broken_author_format_does_not_fail_the_publish(settings: Settings) -> None:
    """An operator typo in configuration must degrade, not raise mid-publish."""
    rendered = render_post(
        with_settings(settings, TEMPLATE_AUTHOR_FORMAT="{nope} {missing}"), make_tweet()
    )

    assert "Alice" in rendered.caption
    assert "{nope}" not in rendered.caption
    assert_well_formed(rendered.caption)


# ---------------------------------------------------------------------- footer
CHANNEL = {
    "TELEGRAM_CHANNEL_NAME": "Dovio Twitter",
    "TELEGRAM_CHANNEL_USERNAME": "doviotwitter",
}


def test_the_channel_footer_is_appended(settings: Settings) -> None:
    rendered = render_post(make_tweet(text="body"), with_settings(settings, **CHANNEL))

    assert rendered.caption.rstrip().endswith("@doviotwitter")
    assert "Dovio Twitter" in rendered.caption
    assert "📢" in rendered.caption
    assert_well_formed(rendered.caption)


def test_a_username_only_footer_renders_without_a_name(settings: Settings) -> None:
    rendered = render_post(
        make_tweet(), with_settings(settings, TELEGRAM_CHANNEL_USERNAME="doviotwitter")
    )

    assert rendered.caption.rstrip().endswith("@doviotwitter")
    assert "📢" not in rendered.caption


def test_the_channel_id_username_is_used_when_no_username_is_configured(
    settings: Settings,
) -> None:
    """`TELEGRAM_CHANNEL_ID=@doviotwitter` is a configured detail, not an invention."""
    rendered = render_post(
        make_tweet(),
        with_settings(settings, TELEGRAM_CHANNEL_ID="@doviotwitter", TELEGRAM_CHANNEL_NAME="Dovio"),
    )

    assert "@doviotwitter" in rendered.caption
    assert "Dovio" in rendered.caption


def test_a_private_channel_footer_invents_nothing(settings: Settings) -> None:
    """Numeric channel id, no name and no username: the footer is empty."""
    rendered = render_post(make_tweet(text="body"), settings)

    assert "@" not in rendered.caption
    assert "📢" not in rendered.caption
    assert_well_formed(rendered.caption)


def test_the_footer_survives_truncation(settings: Settings) -> None:
    rendered = render_post(make_tweet(text="word " * 4000), with_settings(settings, **CHANNEL))

    assert rendered.truncated
    assert "@doviotwitter" in rendered.caption
    assert_well_formed(rendered.caption)


def test_the_footer_appears_once_when_the_text_is_a_separate_message(
    settings: Settings,
) -> None:
    long_post = make_tweet(text="word " * 4000, media=(photo(0),))
    rendered = render_post(
        with_settings(settings, LONG_CAPTION_MODE=LongCaptionMode.TEXT_FIRST, **CHANNEL),
        long_post,
    )

    assert rendered.order == "text_first"
    assert "@doviotwitter" not in (rendered.text_message or "")
    assert "@doviotwitter" in rendered.caption


def test_the_footer_is_on_the_last_message_in_media_first_mode(settings: Settings) -> None:
    long_post = make_tweet(text="word " * 4000, media=(photo(0),))
    rendered = render_post(
        with_settings(settings, LONG_CAPTION_MODE=LongCaptionMode.MEDIA_FIRST, **CHANNEL),
        long_post,
    )

    assert rendered.order == "media_first"
    assert "@doviotwitter" in (rendered.text_message or "")
    assert "@doviotwitter" not in rendered.caption


# -------------------------------------------------------------------- templates
def test_minimal_template_omits_the_timestamp_and_the_handle(settings: Settings) -> None:
    rendered = render_post(
        with_settings(settings, POST_TEMPLATE=TemplateName.MINIMAL), make_tweet()
    )

    assert "Alice" in rendered.caption
    assert "@alice" not in rendered.caption
    assert "2026-03-01" not in rendered.caption


def test_custom_template_without_a_text_placeholder_still_includes_the_post(
    settings: Settings,
) -> None:
    """The alternative — publishing a header with no post in it — is worse."""
    rendered = render_post(
        with_settings(
            settings, POST_TEMPLATE=TemplateName.CUSTOM, TEMPLATE_CUSTOM="<b>Relay</b>"
        ),
        make_tweet(text="the actual post"),
    )

    assert "the actual post" in rendered.caption
    assert "Relay" in rendered.caption


def test_custom_template_may_contain_braces(settings: Settings) -> None:
    """`.format` would raise on a template containing JSON; `str.replace` does not."""
    rendered = render_post(
        with_settings(
            settings,
            POST_TEMPLATE=TemplateName.CUSTOM,
            TEMPLATE_CUSTOM='<b>{author}</b>\n\n{text}\n\n<code>{"k": 1}</code>',
        ),
        make_tweet(text="payload"),
    )

    assert '{"k": 1}' in rendered.caption
    assert "payload" in rendered.caption
    assert "&lt;b&gt;" not in rendered.caption  # {author} is markup, and stays markup
    assert_well_formed(rendered.caption)


def test_oversized_header_is_dropped_before_the_body(settings: Settings) -> None:
    rendered = render_post(
        with_settings(settings, TEMPLATE_HEADER="H" * 3900), make_tweet(text="body text here")
    )

    assert "body text here" in rendered.caption
    assert visible_length(rendered.caption) <= MAX_MESSAGE_LENGTH


def test_the_source_link_is_never_rendered(settings: Settings) -> None:
    rendered = render_post(make_tweet(), settings)

    assert "x.com/alice/status" not in rendered.caption
    assert "<a href" not in rendered.caption


def test_the_timestamp_is_never_rendered(settings: Settings) -> None:
    rendered = render_post(make_tweet(), settings)

    assert "2026-03-01" not in rendered.caption


# --------------------------------------------------------------------- preview
def test_preview_says_the_post_is_going_to_the_channel(settings: Settings) -> None:
    preview = render_preview(make_tweet(), settings)

    assert "going to the channel" in preview
    assert "<blockquote>" in preview
    assert_well_formed(preview)


def test_preview_reports_truncation_and_unsupported_media(settings: Settings) -> None:
    preview = render_preview(
        make_tweet(text="w" * 9000, media=(video(0),)),
        with_settings(settings, ALLOW_VIDEO=False),
    )

    assert "text will be truncated" in preview
    assert "unsupported item" in preview


def test_preview_reports_a_separate_text_message(settings: Settings) -> None:
    preview = render_preview(
        with_settings(settings, LONG_CAPTION_MODE=LongCaptionMode.TEXT_FIRST),
        make_tweet(text="word " * 4000, media=(photo(0),)),
    )

    assert "separate message" in preview


def test_preview_flags_an_already_published_post(settings: Settings) -> None:
    preview = render_preview(make_tweet(), settings, already_published=True)

    assert "already published" in preview
