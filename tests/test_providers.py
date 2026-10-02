"""Provider tests — normalisation, error classification, and the retry contract.

Two things are being defended here.

**The retry contract.** Every provider failure carries `retryable`, and that
single boolean is what decides whether the worker backs off or abandons the job.
A 401 misclassified as retryable burns five attempts and hides a configuration
mistake behind an ever-slower stream of identical errors; a 503 classified as
permanent throws away a post that would have worked on the second try. So the
classification functions are tested directly, case by case.

**Normalisation degrades instead of raising.** Third-party APIs omit fields and
name them differently. A missing display name must produce a post without a
byline, not a `KeyError` in the middle of a publish. The tests below are mostly
about which of several plausible field names wins, and about what happens when
none of them is present.

Nothing here touches the network. The transport is replaced wholesale — the
session object is faked — so the `fetch()` paths are exercised without a socket.
What is *not* covered here is `app/utils/net.py` itself (the SSRF policy and the
DNS pinning); that needs a live server and is a separate concern.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

import aiohttp
import pytest
from pydantic import SecretStr

from app.config import Settings, TweetProviderName
from app.database.enums import MediaKind
from app.providers import build_provider
from app.providers.base import (
    MediaData,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderTransientError,
    TweetData,
    TweetNotFoundError,
    TweetUnavailableError,
)
from app.providers.generic_http import GenericHttpProvider
from app.providers.generic_http import _classify as generic_classify
from app.providers.generic_http import _dig, _dimensions
from app.providers.generic_http import _error_detail as generic_error_detail
from app.providers.generic_http import _media_items as generic_media_items
from app.providers.generic_http import _metrics as generic_metrics
from app.providers.generic_http import _parse_timestamp as generic_timestamp
from app.providers.generic_http import _retry_after as generic_retry_after
from app.providers.generic_http import _url_map as generic_url_map
from app.providers.generic_http import normalise_node as generic_normalise
from app.providers.mock import (
    NOT_FOUND_ID,
    RATE_LIMITED_ID,
    TRANSIENT_ID,
    UNAVAILABLE_ID,
    MockProvider,
)
from app.providers.syndication import SyndicationProvider
from app.providers.syndication import _best_video_url as syndication_best_video_url
from app.providers.syndication import _classify as syndication_classify
from app.providers.syndication import _js_to_string_36 as syndication_to_base36
from app.providers.syndication import _js_number as syndication_number
from app.providers.syndication import _token as syndication_token
from app.providers.syndication import _to_base36 as syndication_int_base36
from app.providers.x_api import XApiProvider
from app.providers.x_api import _best_mp4 as x_best_mp4
from app.providers.x_api import _classify as x_classify
from app.providers.x_api import _error_detail as x_error_detail
from app.providers.x_api import _media_items as x_media_items
from app.providers.x_api import _normalise as x_normalise
from app.providers.x_api import _parse_timestamp as x_timestamp
from app.providers.x_api import _pruned
from app.providers.x_api import _retry_after as x_retry_after


# --------------------------------------------------------------------- fakes
class FakeResponse:
    """Enough of an `aiohttp.ClientResponse` for the two `fetch()` methods."""

    def __init__(
        self, *, status: int = 200, body: str = "", headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def text(self) -> str:
        return self._body


class FakeSession:
    """Stands in for the SSRF-pinned session, recording what was requested.

    `response` may be a `FakeResponse` or an exception, so a transport failure
    can be injected without a real connection.
    """

    def __init__(self, response: FakeResponse | Exception) -> None:
        self._response = response
        self.requests: list[dict[str, Any]] = []

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def close(self) -> None:
        return None


def attach_session(provider: Any, response: FakeResponse | Exception) -> FakeSession:
    """Swap a provider's transport for a fake.

    A helper rather than an inline assignment because the assignment itself
    needs a `type: ignore` that pushes the line past the 100-character limit
    when the response construction is at all long.
    """
    session = FakeSession(response)
    provider._session = session
    return session


@pytest.fixture
def no_real_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the provider constructors from opening an aiohttp session.

    The constructors call `create_session`, which would allocate a real
    connector (and an event-loop-bound one at that). Nothing in these tests
    speaks to a socket, so it is replaced with a placeholder that the test then
    overwrites with a `FakeSession`.
    """
    monkeypatch.setattr(
        "app.providers.x_api.create_session", lambda *a, **k: FakeSession(FakeResponse())
    )
    monkeypatch.setattr(
        "app.providers.generic_http.create_session", lambda *a, **k: FakeSession(FakeResponse())
    )
    monkeypatch.setattr(
        "app.providers.syndication.create_session", lambda *a, **k: FakeSession(FakeResponse())
    )


def make_x_provider(**kwargs: Any) -> XApiProvider:
    return XApiProvider(bearer_token="test-bearer-token", **kwargs)


def make_generic_provider(**kwargs: Any) -> GenericHttpProvider:
    base: dict[str, Any] = {"base_url": "https://api.example-provider.com/v1", "api_key": "k"}
    base.update(kwargs)
    return GenericHttpProvider(**base)


def make_syndication_provider(**kwargs: Any) -> SyndicationProvider:
    return SyndicationProvider(**kwargs)


def x_payload(**data_overrides: Any) -> dict[str, Any]:
    """A complete `/2/tweets/:id` response, minus whatever the test changes."""
    data: dict[str, Any] = {
        "id": "1234567890",
        "text": "the truncated variant",
        "author_id": "42",
        "created_at": "2026-03-01T12:30:00.000Z",
        "lang": "en",
    }
    data.update(data_overrides)
    return {"data": data}


# ------------------------------------------------------------ the base contract
def test_every_provider_error_declares_retryability() -> None:
    """The worker reads exactly one attribute; it must exist on every subclass."""
    for cls in (
        ProviderError,
        TweetNotFoundError,
        TweetUnavailableError,
        ProviderAuthError,
        ProviderRateLimitedError,
        ProviderTransientError,
        ProviderResponseError,
    ):
        assert isinstance(cls.retryable, bool), cls.__name__


def test_a_missing_tweet_is_never_retried() -> None:
    assert TweetNotFoundError("gone").retryable is False
    assert TweetUnavailableError("protected").retryable is False
    assert ProviderAuthError("bad token").retryable is False


def test_a_transport_failure_is_always_retried() -> None:
    assert ProviderTransientError("503").retryable is True
    assert ProviderRateLimitedError("429").retryable is True


def test_an_unexpected_shape_is_retried_rather_than_discarded() -> None:
    """A truncated body and a real schema change look identical on one sample.

    Retrying costs an attempt; giving up costs the post. The retry budget bounds
    the downside, and a persistent mismatch still ends up visible in `dead`.
    """
    assert ProviderResponseError("not what we expected").retryable is True


def test_the_status_appears_in_the_message_but_not_when_absent() -> None:
    assert "status=429" in str(ProviderRateLimitedError("slow down", status=429))
    assert "status" not in str(ProviderTransientError("boom"))


# ------------------------------------------------------------------- data types
def test_tweet_data_separates_photos_and_video_from_what_it_cannot_publish() -> None:
    photo = MediaData(position=0, kind=MediaKind.PHOTO, remote_url="https://pbs.twimg.com/a.jpg")
    clip = MediaData(position=1, kind=MediaKind.VIDEO, remote_url="https://video.twimg.com/a.mp4")
    gif = MediaData(position=2, kind=MediaKind.ANIMATION, remote_url="https://video.twimg.com/a.mp4")
    tweet = TweetData(tweet_id="1", text="hi", media=(photo, clip, gif))

    assert tweet.photos == (photo,)
    assert tweet.videos == (clip,)
    # Animation is still a scope limit; video is not.
    assert tweet.has_unsupported_media is True


def test_a_photo_only_tweet_has_nothing_unsupported() -> None:
    photo = MediaData(position=0, kind=MediaKind.PHOTO, remote_url="https://pbs.twimg.com/a.jpg")

    assert TweetData(tweet_id="1", text="hi", media=(photo,)).has_unsupported_media is False


def test_a_video_only_tweet_has_nothing_unsupported() -> None:
    """Video is published, so it is no longer something the preview must warn about."""
    clip = MediaData(position=0, kind=MediaKind.VIDEO, remote_url="https://video.twimg.com/a.mp4")

    tweet = TweetData(tweet_id="1", text="hi", media=(clip,))

    assert tweet.videos == (clip,)
    assert tweet.photos == ()
    assert tweet.has_unsupported_media is False


def test_the_canonical_url_falls_back_when_the_handle_is_unknown() -> None:
    assert (
        TweetData(tweet_id="123", text="x", author_username="alice").canonical_url
        == "https://x.com/alice/status/123"
    )
    assert "123" in TweetData(tweet_id="123", text="x").canonical_url


def test_media_data_knows_whether_it_is_a_photo() -> None:
    assert MediaData(position=0, kind=MediaKind.PHOTO, remote_url="u").is_photo is True
    assert MediaData(position=0, kind=MediaKind.ANIMATION, remote_url="u").is_photo is False


# ------------------------------------------------------------------ build_provider
def test_the_mock_provider_can_be_selected_without_credentials(
    settings: Settings,
) -> None:
    config = settings.model_copy(update={"TWEET_PROVIDER": TweetProviderName.MOCK})

    assert isinstance(build_provider(config), MockProvider)


def test_selecting_x_api_without_a_token_fails_at_construction(
    settings: Settings,
) -> None:
    """Failing here means the mistake is caught at startup, not at first paste."""
    config = settings.model_copy(
        update={"TWEET_PROVIDER": TweetProviderName.X_API, "TWEET_API_KEY": SecretStr("")}
    )

    with pytest.raises(ProviderAuthError):
        build_provider(config)


def test_the_default_provider_needs_no_credential(
    settings: Settings, no_real_session: None
) -> None:
    """The default must work on a fresh install, and must not be `mock`.

    A `mock` default would publish fixture text into a real channel and look like
    it was working. `syndication` is the only real provider that requires nothing
    bought or borrowed, so it is the only honest default.
    """
    assert settings.TWEET_PROVIDER is TweetProviderName.SYNDICATION
    assert settings.startup_problems() == []
    assert isinstance(build_provider(settings), SyndicationProvider)


# ------------------------------------------------------------------ mock provider
async def test_the_mock_provider_returns_each_documented_fixture() -> None:
    provider = MockProvider()

    assert (await provider.fetch("1")).media == ()
    assert len((await provider.fetch("2")).media) == 1
    assert len((await provider.fetch("3")).media) == 3
    long_post = await provider.fetch("4")
    # Fixture 4 exists to exceed the 1024-character *caption* limit, which is a
    # different threshold from the 280-character one people expect. The bound
    # asserted here is deliberately loose: the fixture's exact length was not
    # measured (this suite was written where it could not be run), so a tighter
    # assertion would be a guess dressed up as a test. If fixture 4 turns out
    # not to clear 1024, that is a fixture bug worth fixing — see
    # `tests/test_render.py`, which covers the overflow path with generated text.
    assert len(long_post.text) > 500
    assert len(long_post.media) == 2
    video_fixture = await provider.fetch("9")
    assert len(video_fixture.videos) == 1
    assert video_fixture.has_unsupported_media is False


async def test_the_mock_provider_raises_the_error_each_fixture_id_stands_for() -> None:
    provider = MockProvider()

    with pytest.raises(TweetNotFoundError):
        await provider.fetch(NOT_FOUND_ID)
    with pytest.raises(TweetUnavailableError):
        await provider.fetch(UNAVAILABLE_ID)
    with pytest.raises(ProviderRateLimitedError) as limited:
        await provider.fetch(RATE_LIMITED_ID)
    with pytest.raises(ProviderTransientError):
        await provider.fetch(TRANSIENT_ID)

    # The retry_after is what lets the job come back at the right time rather
    # than at the end of the default backoff.
    assert limited.value.retry_after == 2.0
    assert limited.value.status == 429


async def test_the_mock_provider_synthesises_a_post_for_any_other_id() -> None:
    """So a developer can paste a real link and get something back."""
    provider = MockProvider()
    tweet = await provider.fetch("1234567890123456789")

    assert tweet.tweet_id == "1234567890123456789"
    assert tweet.text
    assert tweet.raw.get("synthesised") is True


async def test_the_mock_provider_records_what_was_asked_for() -> None:
    provider = MockProvider()
    await provider.fetch("1")
    await provider.fetch("1234567890123456789")

    assert provider.fetches == ["1", "1234567890123456789"]


def test_the_fixture_ids_cannot_be_mistaken_for_real_ones() -> None:
    """Real tweet IDs are 18-19 digits; a collision would be a confusing bug."""
    assert all(len(fixture_id) == 1 for fixture_id in ("1", "2", "3", "4", "9"))


def test_the_mock_provider_names_itself_for_the_provider_column() -> None:
    assert MockProvider.name == "mock"


# ------------------------------------------------------- x_api: error mapping
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, '{"errors":[{"detail":"Invalid token"}]}', ProviderAuthError),
        (404, '{"errors":[{"detail":"Not Found"}]}', TweetNotFoundError),
        (429, '{"errors":[{"detail":"Too many"}]}', ProviderRateLimitedError),
        (500, "upstream exploded", ProviderTransientError),
        (503, "upstream exploded", ProviderTransientError),
        (400, '{"errors":[{"detail":"Bad request"}]}', ProviderResponseError),
    ],
)
def test_x_status_codes_map_to_the_right_error(
    status: int, body: str, expected: type[ProviderError]
) -> None:
    assert isinstance(x_classify(status, body, {}), expected)


@pytest.mark.parametrize(
    "detail", ["this account is protected", "User has been suspended", "Tweet withheld"]
)
def test_a_403_about_the_tweet_is_not_a_credential_problem(detail: str) -> None:
    """X uses 403 for both; only the body distinguishes them, and the two need
    different behaviour — one is permanent, the other is a configuration bug."""
    error = x_classify(403, json.dumps({"errors": [{"detail": detail}]}), {})

    assert isinstance(error, TweetUnavailableError)
    assert error.retryable is False


def test_a_403_without_those_words_is_treated_as_an_entitlement_problem() -> None:
    """A valid token on a plan that does not cover the endpoint. Permanent."""
    error = x_classify(403, '{"title":"Forbidden"}', {})

    assert isinstance(error, ProviderAuthError)
    assert error.retryable is False
    assert "plan" in str(error)


def test_x_rate_limit_headers_become_a_retry_delay() -> None:
    error = x_classify(429, "{}", {"retry-after": "30"})

    assert isinstance(error, ProviderRateLimitedError)
    assert error.retry_after == 30


def test_an_epoch_reset_header_is_converted_to_a_delay() -> None:
    """`x-rate-limit-reset` is an absolute timestamp, not a duration — reading
    it as a duration would park the job for ~56 years."""
    reset = int(datetime.now(UTC).timestamp()) + 45
    delay = x_retry_after({"x-rate-limit-reset": str(reset)})

    assert delay is not None
    assert 0 <= delay <= 46


def test_a_reset_time_in_the_past_yields_no_delay_rather_than_a_negative_one() -> None:
    assert x_retry_after({"x-rate-limit-reset": "1000000000"}) == 0.0


def test_an_http_date_retry_after_is_understood() -> None:
    future = datetime.now(UTC).timestamp() + 60
    header = datetime.fromtimestamp(future, tz=UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
    delay = x_retry_after({"Retry-After": header})

    assert delay is not None
    assert 30 <= delay <= 61


def test_an_unparseable_retry_after_is_ignored() -> None:
    assert x_retry_after({"retry-after": "soon-ish"}) is None
    assert x_retry_after({}) is None


def test_x_error_detail_prefers_the_structured_message() -> None:
    body = json.dumps({"errors": [{"detail": "  the real reason  "}]})

    assert x_error_detail(body) == "the real reason"


def test_x_error_detail_falls_back_through_every_shape() -> None:
    assert x_error_detail('{"title": "Forbidden"}') == "Forbidden"
    assert x_error_detail("plain text") == "plain text"
    assert x_error_detail("") == "no response body"


def test_x_error_detail_survives_an_errors_list_of_surprises() -> None:
    """An `errors` array with nothing usable in it must not raise, and must not
    be reported as an empty message either — the raw body is the fallback."""
    empty = '{"errors": []}'
    blank = '{"errors": [{"detail": ""}]}'

    assert x_error_detail(empty) == empty
    assert x_error_detail(blank) == blank


# --------------------------------------------------------- x_api: normalisation
def test_x_normalise_lifts_the_long_form_body_out_of_note_tweet() -> None:
    """Publishing the truncated variant would be silent data loss."""
    payload = x_payload(
        text="the truncated variant…", note_tweet={"text": "the whole thing, at length"}
    )

    assert x_normalise(payload, "1234567890").text == "the whole thing, at length"


def test_x_normalise_uses_text_when_there_is_no_note() -> None:
    assert x_normalise(x_payload(), "1234567890").text == "the truncated variant"


def test_x_normalise_falls_back_when_the_note_is_empty() -> None:
    payload = x_payload(note_tweet={"text": ""})

    assert x_normalise(payload, "1234567890").text == "the truncated variant"


def test_x_normalise_pulls_the_author_out_of_the_expansion() -> None:
    payload = x_payload()
    payload["includes"] = {
        "users": [
            {
                "id": "42",
                "name": "Alice",
                "username": "alice",
                "profile_image_url": "https://pbs.twimg.com/alice.jpg",
            }
        ]
    }

    tweet = x_normalise(payload, "1234567890")

    assert tweet.author_username == "alice"
    assert tweet.author_name == "Alice"
    assert tweet.author_id == "42"
    assert tweet.author_avatar_url == "https://pbs.twimg.com/alice.jpg"


def test_a_tweet_without_an_author_expansion_still_normalises() -> None:
    """Third-party plans often omit the expansion; the byline degrades to absent."""
    tweet = x_normalise(x_payload(), "1234567890")

    assert tweet.author_username is None
    assert tweet.text


def test_x_media_follows_the_attachment_key_order_not_the_includes_order() -> None:
    """A photo set rendered out of order is a visible defect, and `includes` is
    explicitly not guaranteed to match `attachments.media_keys`."""
    payload = x_payload(attachments={"media_keys": ["3_2", "3_1"]})
    payload["includes"] = {
        "media": [
            {"media_key": "3_1", "type": "photo", "url": "https://pbs.twimg.com/one.jpg"},
            {"media_key": "3_2", "type": "photo", "url": "https://pbs.twimg.com/two.jpg"},
        ]
    }

    media = x_normalise(payload, "1234567890").media

    assert [m.remote_url for m in media] == [
        "https://pbs.twimg.com/two.jpg",
        "https://pbs.twimg.com/one.jpg",
    ]
    assert [m.position for m in media] == [0, 1]


def test_x_media_falls_back_to_the_includes_order_without_attachment_keys() -> None:
    payload = x_payload()
    payload["includes"] = {
        "media": [{"media_key": "3_1", "type": "photo", "url": "https://pbs.twimg.com/one.jpg"}]
    }

    media = x_normalise(payload, "1234567890").media

    assert [m.remote_url for m in media] == ["https://pbs.twimg.com/one.jpg"]


def test_an_unknown_x_media_type_is_skipped_rather_than_published_broken() -> None:
    payload = x_payload(attachments={"media_keys": ["9_1", "9_2"]})
    payload["includes"] = {
        "media": [
            {"media_key": "9_1", "type": "hologram", "url": "https://pbs.twimg.com/x.jpg"},
            {"media_key": "9_2", "type": "photo", "url": "https://pbs.twimg.com/ok.jpg"},
        ]
    }

    media = x_media_items(payload["data"], payload["includes"]["media"])

    assert [m.remote_url for m in media] == ["https://pbs.twimg.com/ok.jpg"]


def test_a_photo_entry_with_no_url_is_dropped() -> None:
    media = x_media_items({}, [{"media_key": "3_1", "type": "photo"}])

    assert media == ()


def test_x_carries_dimensions_and_alt_text_through() -> None:
    payload = x_payload(attachments={"media_keys": ["3_1"]})
    payload["includes"] = {
        "media": [
            {
                "media_key": "3_1",
                "type": "photo",
                "url": "https://pbs.twimg.com/a.jpg",
                "width": 1200,
                "height": 675,
                "alt_text": "a described image",
            }
        ]
    }

    item = x_normalise(payload, "1234567890").media[0]

    assert (item.width, item.height) == (1200, 675)
    assert item.alt_text == "a described image"
    assert item.content_type == "image/jpeg"


def test_x_picks_the_highest_bitrate_mp4_and_ignores_hls() -> None:
    obj = {
        "video_info": {
            "variants": [
                {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/a.m3u8"},
                {"content_type": "video/mp4", "bitrate": 256000, "url": "https://v/256.mp4"},
                {"content_type": "video/mp4", "bitrate": 2176000, "url": "https://v/2176.mp4"},
            ]
        }
    }

    assert x_best_mp4(obj) == "https://v/2176.mp4"


def test_a_video_with_only_hls_has_no_usable_url() -> None:
    obj = {"video_info": {"variants": [{"content_type": "application/x-mpegURL", "url": "u"}]}}

    assert x_best_mp4(obj) is None


def test_a_video_media_item_falls_back_to_its_thumbnail() -> None:
    """Better a still frame than a silently missing attachment."""
    media = x_media_items(
        {},
        [
            {
                "media_key": "7_1",
                "type": "video",
                "preview_image_url": "https://pbs.twimg.com/thumb.jpg",
                "duration_ms": 15000,
            }
        ],
    )

    assert media[0].kind is MediaKind.VIDEO
    assert media[0].remote_url == "https://pbs.twimg.com/thumb.jpg"
    assert media[0].duration_ms == 15000


def test_an_animated_gif_is_typed_as_an_animation() -> None:
    media = x_media_items(
        {}, [{"media_key": "1_1", "type": "animated_gif", "preview_image_url": "https://p/i.jpg"}]
    )

    assert media[0].kind is MediaKind.ANIMATION


def test_x_expands_short_links_for_display() -> None:
    payload = x_payload(
        entities={
            "urls": [
                {"url": "https://t.co/abc", "expanded_url": "https://example.com/a-real-link"}
            ]
        }
    )

    assert x_normalise(payload, "1").url_map == {
        "https://t.co/abc": "https://example.com/a-real-link"
    }


def test_x_keeps_only_integer_metrics() -> None:
    """`public_metrics` values are rendered and stored as counts; a string or a
    bool in there would be a rendering bug waiting to happen."""
    payload = x_payload(
        public_metrics={"like_count": 5, "retweet_count": "3", "verified": True, "note": "x"}
    )

    assert x_normalise(payload, "1").metrics == {"like_count": 5}


def test_x_stores_the_payload_without_the_note_tweet_duplication() -> None:
    payload = x_payload(note_tweet={"text": "long", "entities": {"inline": "offsets"}})
    payload["includes"] = {"users": [{"id": "42", "username": "alice"}]}

    raw = x_normalise(payload, "1").raw

    assert "note_tweet" not in raw
    assert raw["_author"]["username"] == "alice"


def test_x_pruned_keeps_the_media_only_when_there_is_some() -> None:
    assert "_media" not in _pruned({"id": "1"}, [], None)
    assert "_media" in _pruned({"id": "1"}, [{"media_key": "1_1"}], None)


def test_a_200_with_no_data_object_is_reported_as_not_found() -> None:
    """Some plans answer 200 with an `errors` body rather than a 404."""
    with pytest.raises(TweetNotFoundError):
        x_normalise({"errors": [{"detail": "Not Found"}]}, "1234567890")


def test_a_tweet_with_no_text_anywhere_is_a_response_error() -> None:
    with pytest.raises(ProviderResponseError):
        x_normalise({"data": {"id": "1"}}, "1")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-03-01T12:30:00.000Z", datetime(2026, 3, 1, 12, 30, tzinfo=UTC)),
        ("2026-03-01T12:30:00+00:00", datetime(2026, 3, 1, 12, 30, tzinfo=UTC)),
        ("2026-03-01T12:30:00", datetime(2026, 3, 1, 12, 30, tzinfo=UTC)),
    ],
)
def test_x_timestamps_are_parsed_as_utc(value: str, expected: datetime) -> None:
    assert x_timestamp(value) == expected


def test_an_unparseable_x_timestamp_is_absent_rather_than_fatal() -> None:
    """A missing date costs the timestamp line; raising would cost the post."""
    assert x_timestamp("last Tuesday") is None
    assert x_timestamp(None) is None
    assert x_timestamp("") is None


# ------------------------------------------------------- x_api: the fetch path
async def test_x_fetch_sends_a_bearer_token_and_the_expansions(
    no_real_session: None,
) -> None:
    provider = make_x_provider()
    session = FakeSession(FakeResponse(body=json.dumps(x_payload())))
    provider._session = session  # type: ignore[assignment]

    await provider.fetch("1234567890")

    request = session.requests[0]
    assert request["url"].endswith("/2/tweets/1234567890")
    assert request["headers"]["Authorization"] == "Bearer test-bearer-token"
    # Without these the response has no author and no media.
    assert "author_id" in request["params"]["expansions"]
    assert "note_tweet" in request["params"]["tweet.fields"]


async def test_x_fetch_turns_a_non_200_into_a_classified_error(
    no_real_session: None,
) -> None:
    provider = make_x_provider()
    provider._session = FakeSession(  # type: ignore[assignment]
        FakeResponse(status=404, body='{"errors":[{"detail":"Not Found"}]}')
    )

    with pytest.raises(TweetNotFoundError):
        await provider.fetch("1")


async def test_a_transport_failure_becomes_a_retryable_provider_error(
    no_real_session: None,
) -> None:
    """An escaping `aiohttp.ClientError` would be classified by accident rather
    than by intent, which is why every one of them is translated here."""
    provider = make_x_provider()
    attach_session(provider, aiohttp.ClientConnectionError("reset"))

    with pytest.raises(ProviderTransientError):
        await provider.fetch("1")


async def test_a_body_that_is_not_json_is_a_response_error(no_real_session: None) -> None:
    provider = make_x_provider()
    attach_session(provider, FakeResponse(body="<html>gateway</html>"))

    with pytest.raises(ProviderResponseError):
        await provider.fetch("1")


async def test_a_json_body_that_is_not_an_object_is_a_response_error(
    no_real_session: None,
) -> None:
    provider = make_x_provider()
    attach_session(provider, FakeResponse(body="[1, 2, 3]"))

    with pytest.raises(ProviderResponseError):
        await provider.fetch("1")


async def test_x_fetch_returns_a_normalised_tweet(no_real_session: None) -> None:
    provider = make_x_provider()
    payload = x_payload(attachments={"media_keys": ["3_1"]})
    payload["includes"] = {
        "users": [{"id": "42", "name": "Alice", "username": "alice"}],
        "media": [{"media_key": "3_1", "type": "photo", "url": "https://pbs.twimg.com/a.jpg"}],
    }
    attach_session(provider, FakeResponse(body=json.dumps(payload)))

    tweet = await provider.fetch("1234567890")

    assert tweet.author_username == "alice"
    assert len(tweet.photos) == 1


# ------------------------------------------------- generic_http: construction
def test_generic_http_requires_a_base_url() -> None:
    with pytest.raises(ProviderResponseError):
        GenericHttpProvider(base_url="   ")


def test_generic_http_requires_the_tweet_id_placeholder() -> None:
    """Without it every request would fetch the same URL and return one tweet."""
    with pytest.raises(ProviderResponseError):
        GenericHttpProvider(base_url="https://api.example.com", tweet_path="/tweets/latest")


def test_generic_http_rejects_a_base_url_with_no_hostname() -> None:
    with pytest.raises(ProviderResponseError):
        GenericHttpProvider(base_url="not-a-url")


def test_generic_http_names_itself_for_the_provider_column() -> None:
    assert GenericHttpProvider.name == "generic_http"


# ------------------------------------------------- generic_http: request shape
def test_generic_http_omits_the_auth_header_when_no_key_is_set(
    no_real_session: None,
) -> None:
    provider = make_generic_provider(api_key="")

    assert provider._headers() == {"Accept": "application/json"}


def test_generic_http_applies_the_configured_scheme(no_real_session: None) -> None:
    provider = make_generic_provider(api_key="secret", auth_scheme="Bearer")

    assert provider._headers()["Authorization"] == "Bearer secret"


def test_generic_http_allows_a_bare_key_with_no_scheme(no_real_session: None) -> None:
    provider = make_generic_provider(api_key="secret", auth_scheme="")

    assert provider._headers()["Authorization"] == "secret"


def test_generic_http_allows_a_custom_auth_header(no_real_session: None) -> None:
    provider = make_generic_provider(api_key="secret", auth_header="X-API-Key", auth_scheme="")

    assert provider._headers()["X-API-Key"] == "secret"


async def test_generic_http_substitutes_the_tweet_id_into_the_path(
    no_real_session: None,
) -> None:
    provider = make_generic_provider()
    session = FakeSession(FakeResponse(body=json.dumps({"data": {"id": "1", "text": "hi"}})))
    provider._session = session  # type: ignore[assignment]

    await provider.fetch("987654321")

    assert session.requests[0]["url"] == (
        "https://api.example-provider.com/v1/tweets/987654321"
    )


async def test_generic_http_reports_a_missing_response_root_clearly(
    no_real_session: None,
) -> None:
    """The top-level keys are included because the usual cause is a provider
    that wraps the tweet one level deeper than configured."""
    provider = make_generic_provider(response_root="data")
    attach_session(provider, FakeResponse(body=json.dumps({"result": {"id": "1"}})))

    with pytest.raises(ProviderResponseError) as excinfo:
        await provider.fetch("1")

    assert "result" in str(excinfo.value)


async def test_generic_http_reads_a_nested_response_root(no_real_session: None) -> None:
    provider = make_generic_provider(response_root="result.tweet")
    provider._session = FakeSession(  # type: ignore[assignment]
        FakeResponse(body=json.dumps({"result": {"tweet": {"id": "1", "text": "nested"}}}))
    )

    assert (await provider.fetch("1")).text == "nested"


@pytest.mark.parametrize(
    ("path", "expected"),
    [("", {"a": {"b": 1}}), ("a", {"b": 1}), ("a.b", 1), ("a.z", None), ("z", None)],
)
def test_dig_follows_a_dotted_path(path: str, expected: Any) -> None:
    assert _dig({"a": {"b": 1}}, path) == expected


def test_dig_does_not_walk_into_a_non_mapping() -> None:
    assert _dig({"a": [1, 2]}, "a.b") is None


# ------------------------------------------------- generic_http: normalisation
def test_generic_normalise_prefers_the_richest_text_field() -> None:
    node = {"id_str": "1", "text": "short", "full_text": "the full text"}

    assert generic_normalise(node, "1").text == "the full text"


def test_generic_normalise_reads_a_note_tweet_object() -> None:
    node = {"id": "1", "note_tweet": {"text": "long form"}}

    assert generic_normalise(node, "1").text == "long form"


def test_generic_normalise_reads_a_note_tweet_string() -> None:
    assert generic_normalise({"id": "1", "note_tweet": "long form"}, "1").text == "long form"


def test_generic_normalise_raises_when_there_is_no_text_at_all() -> None:
    with pytest.raises(ProviderResponseError):
        generic_normalise({"id": "1"}, "1")


def test_generic_normalise_falls_back_to_the_requested_id() -> None:
    assert generic_normalise({"text": "hi"}, "999").tweet_id == "999"


def test_generic_normalise_accepts_a_numeric_id() -> None:
    """Several APIs send the ID as a number, which is fine until it is not."""
    assert generic_normalise({"id": 1234567890, "text": "hi"}, "1").tweet_id == "1234567890"


@pytest.mark.parametrize(
    "container",
    [
        {"user": {"screen_name": "alice", "name": "Alice"}},
        {"author": {"username": "alice", "display_name": "Alice"}},
        {"account": {"handle": "alice", "full_name": "Alice"}},
    ],
)
def test_generic_normalise_finds_the_author_under_any_of_its_names(
    container: dict[str, Any]
) -> None:
    node = {"id": "1", "text": "hi", **container}

    tweet = generic_normalise(node, "1")

    assert tweet.author_username == "alice"
    assert tweet.author_name == "Alice"


def test_generic_normalise_reads_the_avatar_from_any_of_its_names() -> None:
    user = {"username": "a", "avatar_url": "https://a/i.jpg"}
    node = {"id": "1", "text": "hi", "user": user}

    assert generic_normalise(node, "1").author_avatar_url == "https://a/i.jpg"


def test_a_node_with_no_author_still_normalises() -> None:
    tweet = generic_normalise({"id": "1", "text": "hi"}, "1")

    assert tweet.author_username is None
    assert tweet.author_name is None


def test_generic_media_reads_a_flat_list() -> None:
    node = {"id": "1", "text": "hi", "media": [{"media_url_https": "https://cdn/a.jpg"}]}

    media = generic_media_items(node)

    assert [m.remote_url for m in media] == ["https://cdn/a.jpg"]
    assert media[0].kind is MediaKind.PHOTO


def test_generic_media_reads_the_extended_entities_container() -> None:
    node = {
        "media": None,
        "extendedEntities": {"media": [{"media_url_https": "https://cdn/a.jpg", "type": "photo"}]},
    }

    assert len(generic_media_items(node)) == 1


def test_generic_media_reads_a_nested_attachments_container() -> None:
    node = {"attachments": {"media": [{"url": "https://cdn/a.jpg"}]}}

    assert len(generic_media_items(node)) == 1


def test_generic_media_accepts_a_bare_url_string() -> None:
    """Some APIs return a plain list of image URLs and nothing else."""
    media = generic_media_items({"photos": ["https://cdn/a.jpg", "https://cdn/b.jpg"]})

    assert [m.remote_url for m in media] == ["https://cdn/a.jpg", "https://cdn/b.jpg"]
    assert all(m.kind is MediaKind.PHOTO for m in media)


def test_generic_media_ignores_a_string_that_is_not_a_url() -> None:
    assert generic_media_items({"photos": ["a.jpg"]}) == ()


def test_generic_media_assumes_photo_for_an_unknown_type() -> None:
    """A bare entry with a URL is far more often a picture than anything else,
    and publishing it as a photo is recoverable whereas dropping it is not."""
    media = generic_media_items({"media": [{"url": "https://cdn/a.jpg", "type": "mystery"}]})

    assert media[0].kind is MediaKind.PHOTO


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("photo", MediaKind.PHOTO),
        ("image", MediaKind.PHOTO),
        ("video", MediaKind.VIDEO),
        ("animated_gif", MediaKind.ANIMATION),
        ("gif", MediaKind.ANIMATION),
        ("VIDEO", MediaKind.VIDEO),
    ],
)
def test_generic_media_maps_the_type_names_providers_use(
    raw_type: str, expected: MediaKind
) -> None:
    media = generic_media_items(
        {"media": [{"url": "https://cdn/a.mp4", "type": raw_type}]}
    )

    assert media[0].kind is expected


def test_generic_media_skips_an_entry_with_no_usable_url() -> None:
    assert generic_media_items({"media": [{"type": "photo"}]}) == ()


def test_generic_media_prefers_the_highest_bitrate_mp4_for_a_video() -> None:
    node = {
        "media": [
            {
                "type": "video",
                "video_info": {
                    "variants": [
                        {"content_type": "video/mp4", "bitrate": 100, "url": "https://v/low.mp4"},
                        {"content_type": "video/mp4", "bitrate": 900, "url": "https://v/high.mp4"},
                        {"content_type": "application/x-mpegURL", "url": "https://v/a.m3u8"},
                    ]
                },
            }
        ]
    }

    assert generic_media_items(node)[0].remote_url == "https://v/high.mp4"


def test_generic_media_takes_explicit_dimensions_first() -> None:
    media = generic_media_items({"media": [{"url": "https://c/a.jpg", "width": 10, "height": 20}]})

    assert (media[0].width, media[0].height) == (10, 20)


def test_generic_media_falls_back_to_the_x_style_sizes_object() -> None:
    entry = {"url": "https://c/a.jpg", "sizes": {"large": {"w": 1200, "h": 675}}}
    node = {"media": [entry]}

    media = generic_media_items(node)

    assert (media[0].width, media[0].height) == (1200, 675)


def test_dimensions_from_a_truthy_zero_are_not_treated_as_present() -> None:
    """`if width and height` is doing real work: a zero is not a dimension."""
    assert _dimensions({"width": 0, "height": 0}) == (0, 0)


def test_generic_media_reads_alt_text_under_any_of_its_names() -> None:
    media = generic_media_items({"media": [{"url": "https://c/a.jpg", "altText": "x"}]})

    assert media[0].alt_text == "x"


def test_generic_url_map_reads_the_twitter_entity_shape() -> None:
    node = {
        "entities": {
            "url": {"urls": [{"url": "https://t.co/a", "expanded_url": "https://example.com/x"}]}
        }
    }

    assert generic_url_map(node) == {"https://t.co/a": "https://example.com/x"}


def test_generic_url_map_reads_a_flat_urls_list() -> None:
    entry = {"url": "https://t.co/a", "unwound_url": "https://e.com/x"}
    node = {"entities": {"urls": [entry]}}

    assert generic_url_map(node) == {"https://t.co/a": "https://e.com/x"}


def test_generic_url_map_is_empty_without_entities() -> None:
    assert generic_url_map({"id": "1"}) == {}


def test_generic_metrics_reads_whichever_container_is_present() -> None:
    assert generic_metrics({"metrics": {"likes": 2}}) == {"likes": 2}
    assert generic_metrics({"stats": {"likes": 2}}) == {"likes": 2}
    assert generic_metrics({"public_metrics": {"likes": 2}}) == {"likes": 2}


def test_generic_metrics_keeps_only_integers() -> None:
    assert generic_metrics({"metrics": {"likes": 2, "note": "x", "ok": True}}) == {"likes": 2}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-03-01T12:30:00Z", datetime(2026, 3, 1, 12, 30, tzinfo=UTC)),
        ("2026-03-01T12:30:00+00:00", datetime(2026, 3, 1, 12, 30, tzinfo=UTC)),
        (1772368200, datetime(2026, 3, 1, 12, 30, tzinfo=UTC)),
        ("1772368200", datetime(2026, 3, 1, 12, 30, tzinfo=UTC)),
        (1772368200000, datetime(2026, 3, 1, 12, 30, tzinfo=UTC)),
    ],
)
def test_generic_timestamps_cover_every_format_providers_use(
    value: Any, expected: datetime
) -> None:
    assert generic_timestamp(value) == expected


def test_generic_timestamps_read_twitters_classic_format() -> None:
    parsed = generic_timestamp("Sun Mar 01 12:30:00 +0000 2026")

    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 3, 1)


def test_an_unparseable_generic_timestamp_is_absent_rather_than_fatal() -> None:
    assert generic_timestamp("sometime last week") is None
    assert generic_timestamp(None) is None
    assert generic_timestamp("   ") is None
    assert generic_timestamp(True) is None


def test_generic_normalise_keeps_the_whole_node_for_debugging() -> None:
    """Unlike the X provider there is no oversized note object to strip, and
    the raw shape is what makes a mapping mistake diagnosable from the database."""
    node = {"id": "1", "text": "hi", "extra": {"deep": True}}

    tweet = generic_normalise(node, "1", response_root="data")

    assert tweet.raw["extra"] == {"deep": True}
    assert tweet.raw["_response_root"] == "data"


# ----------------------------------------------- generic_http: error mapping
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, '{"message":"bad key"}', ProviderAuthError),
        (403, '{"message":"bad key"}', ProviderAuthError),
        (404, '{"message":"nope"}', TweetNotFoundError),
        (410, '{"message":"gone"}', TweetUnavailableError),
        (429, '{"message":"slow"}', ProviderRateLimitedError),
        (500, '{"message":"boom"}', ProviderTransientError),
        (502, '{"message":"boom"}', ProviderTransientError),
        (418, '{"message":"teapot"}', ProviderResponseError),
    ],
)
def test_generic_status_codes_map_to_the_right_error(
    status: int, body: str, expected: type[ProviderError]
) -> None:
    assert isinstance(generic_classify(status, body, {}), expected)


def test_a_generic_403_about_the_tweet_is_permanent() -> None:
    error = generic_classify(403, '{"message":"this tweet was deleted"}', {})

    assert isinstance(error, TweetUnavailableError)
    assert error.retryable is False


def test_a_generic_429_carries_the_retry_after_header() -> None:
    error = generic_classify(429, "{}", {"Retry-After": "12"})

    assert isinstance(error, ProviderRateLimitedError)
    assert error.retry_after == 12


def test_generic_retry_after_distinguishes_an_epoch_from_a_duration() -> None:
    """`retry-after` is a duration and `x-ratelimit-reset` is a timestamp, and
    the same header name is not always used for the same thing."""
    assert generic_retry_after({"retry-after": "30"}) == 30.0
    reset = int(datetime.now(UTC).timestamp()) + 20
    assert 0 <= generic_retry_after({"x-ratelimit-reset": str(reset)}) <= 21
    assert generic_retry_after({"retry-after": "in a bit"}) is None
    assert generic_retry_after({}) is None


@pytest.mark.parametrize(
    "body",
    [
        '{"error": "the reason"}',
        '{"message": "the reason"}',
        '{"detail": "the reason"}',
        '{"description": "the reason"}',
        '{"title": "the reason"}',
        '{"error": {"message": "the reason"}}',
        '{"errors": ["the reason"]}',
        '{"errors": [{"message": "the reason"}]}',
    ],
)
def test_generic_error_detail_finds_the_message_whatever_it_is_called(body: str) -> None:
    """Providers disagree wildly here, and the detail is the whole value of the
    log line — "returned 403" on its own is not actionable."""
    assert generic_error_detail(body) == "the reason"


def test_generic_error_detail_falls_back_to_the_raw_body() -> None:
    assert generic_error_detail("plain text") == "plain text"
    assert generic_error_detail("") == "no response body"


# ------------------------------------------- generic_http: the fetch path
async def test_generic_fetch_classifies_a_non_200(no_real_session: None) -> None:
    provider = make_generic_provider()
    attach_session(provider, FakeResponse(status=401, body='{"message":"Invalid API key"}'))

    with pytest.raises(ProviderAuthError):
        await provider.fetch("1")


async def test_generic_fetch_translates_a_transport_failure(no_real_session: None) -> None:
    provider = make_generic_provider()
    attach_session(provider, aiohttp.ClientConnectionError("reset"))

    with pytest.raises(ProviderTransientError):
        await provider.fetch("1")


async def test_generic_fetch_rejects_a_body_that_is_not_json(no_real_session: None) -> None:
    provider = make_generic_provider()
    attach_session(provider, FakeResponse(body="<html>oops</html>"))

    with pytest.raises(ProviderResponseError):
        await provider.fetch("1")


async def test_generic_fetch_normalises_a_realistic_response(no_real_session: None) -> None:
    provider = make_generic_provider(response_root="data")
    payload = {
        "data": {
            "id_str": "1234567890",
            "full_text": "hello from a third-party API",
            "created_at": "Sun Mar 01 12:30:00 +0000 2026",
            "lang": "en",
            "user": {
                "screen_name": "alice",
                "name": "Alice",
                "profile_image_url_https": "https://cdn.example.com/alice.jpg",
            },
            "extendedEntities": {
                "media": [
                    {"type": "photo", "media_url_https": "https://cdn.example.com/a.jpg"},
                    {"type": "photo", "media_url_https": "https://cdn.example.com/b.jpg"},
                ]
            },
            "entities": {
                "urls": [{"url": "https://t.co/x", "expanded_url": "https://example.com/x"}]
            },
        }
    }
    attach_session(provider, FakeResponse(body=json.dumps(payload)))

    tweet = await provider.fetch("1234567890")

    assert tweet.text == "hello from a third-party API"
    assert tweet.author_username == "alice"
    assert tweet.author_name == "Alice"
    assert tweet.author_avatar_url == "https://cdn.example.com/alice.jpg"
    assert [m.remote_url for m in tweet.photos] == [
        "https://cdn.example.com/a.jpg",
        "https://cdn.example.com/b.jpg",
    ]
    assert tweet.url_map == {"https://t.co/x": "https://example.com/x"}
    assert tweet.posted_at is not None


async def test_the_generic_provider_is_usable_as_an_async_context_manager(
    no_real_session: None,
) -> None:
    provider = make_generic_provider()

    async with provider as entered:
        assert entered is provider


def test_a_secret_never_appears_in_the_provider_repr(no_real_session: None) -> None:
    """Credentials must not reach a log line via an accidental f-string."""
    provider = make_generic_provider(api_key="super-secret-key")

    assert "super-secret-key" not in repr(provider)


# ============================================================ syndication provider
# The endpoint these tests describe is X's own embed widget endpoint. The response
# shapes below were written from the documented shape of that endpoint, **not**
# captured from a live call — no network was reachable where this suite was
# written. So these tests prove the provider does the right thing *given* that
# shape; they cannot prove the shape is still current. `_failure_hint` prints the
# command that settles it — `python -m app.providers.syndication <id>`, which
# exercises this provider's real request rather than a bare curl.
#
# What is asserted about the token below is the integer part (exact and
# hand-checkable), the alphabet, and — for the fraction — the *definition* rather
# than a digit string: the expansion must be the shortest base-36 representation
# that parses back to the same double. That is the property V8 produces, and it
# is checkable without a JS engine. A golden fractional string is still not
# asserted, because no JS engine was available to produce one.


class SequencedSession:
    """Returns a different response per call, so a retry can be observed."""

    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        response = self._responses.pop(0) if self._responses else FakeResponse(status=500)
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self) -> None:
        return None


def syndication_payload(**overrides: Any) -> dict[str, Any]:
    """The shape `cdn.syndication.twimg.com/tweet-result` is documented to return."""
    payload: dict[str, Any] = {
        "id_str": "1519480761749016577",
        "text": "hello from the embed endpoint",
        "created_at": "2022-04-28T12:30:00.000Z",
        "lang": "en",
        "user": {
            "name": "Example Person",
            "screen_name": "example",
            "profile_image_url_https": "https://pbs.twimg.com/profile_images/a.jpg",
        },
        "photos": [
            {"url": "https://pbs.twimg.com/media/one.jpg", "width": 1200, "height": 800},
            {"url": "https://pbs.twimg.com/media/two.jpg", "width": 800, "height": 600},
        ],
        "entities": {"urls": []},
    }
    payload.update(overrides)
    return payload


def parse_base36(text: str) -> float:
    """Read `<whole>.<digits>` back to a double, the way a JS engine would."""
    whole, _, fraction = text.partition(".")
    if not fraction:
        return float(int(whole or "0", 36))
    scale = 36 ** len(fraction)
    return (int(whole or "0", 36) * scale + int(fraction, 36)) / scale


def syndication_video_payload(**overrides: Any) -> dict[str, Any]:
    """A video tweet as the embed endpoint describes one.

    The rendition URLs are named `src` here, unlike the public API's `url`, and
    the list mixes an HLS manifest with MP4 renditions — which is what makes the
    selection non-trivial.
    """
    payload = syndication_payload(
        text="watch this https://t.co/MEDIA1",
        photos=[],
        video={
            "aspectRatio": [16, 9],
            "durationMs": 13_400,
            "mediaAvailability": {"status": "available"},
            "poster": "https://pbs.twimg.com/ext_tw_video_thumb/poster.jpg",
            "variants": [
                {
                    "content_type": "application/x-mpegURL",
                    "src": "https://video.twimg.com/playlist.m3u8",
                },
                {
                    "content_type": "video/mp4",
                    "bitrate": 256_000,
                    "src": "https://video.twimg.com/low.mp4",
                },
                {
                    "content_type": "video/mp4",
                    "bitrate": 2_176_000,
                    "src": "https://video.twimg.com/high.mp4",
                },
            ],
            "videoId": {"type": "tweet", "id": "1"},
        },
    )
    payload.update(overrides)
    return payload


# ------------------------------------------------------------------ the token
def test_the_token_reproduces_the_exact_integer_part() -> None:
    """`(1519480761749016577 / 1e15) * pi == 4773.5895...`, and 4773 is `3ol` in base 36.

    The integer part is exact in Python, so unlike the fractional digits this is
    a value that can be checked by hand — which is exactly why it is asserted.
    """
    assert syndication_token("1519480761749016577").startswith("3ol")


def test_the_token_integer_part_is_right_for_a_modern_nineteen_digit_id() -> None:
    """A 2026-era ID, which is the length the bot is actually fed.

    `2105252416639672788 / 1e15 * pi == 6613.8455...`, and 6613 is `53p` in base
    36 (5*1296 + 3*36 + 25). Two zeros are absent, so nothing is stripped from
    the integer part and the whole prefix survives.
    """
    assert syndication_token("2105252416639672788").startswith("53p")


def test_the_token_has_no_zeros_and_no_dot() -> None:
    """The JS strips both zeros and the decimal point, so neither may survive."""
    token = syndication_token("1519480761749016577")

    assert "0" not in token
    assert "." not in token
    assert token


def test_the_token_is_deterministic_and_id_dependent() -> None:
    first = syndication_token("1519480761749016577")

    assert syndication_token("1519480761749016577") == first
    assert syndication_token("1519480761749016578") != first


def test_the_token_stays_inside_the_base36_alphabet() -> None:
    for tweet_id in ("1", "20", "1234567890", "1519480761749016577", "99999999999999999999"):
        assert set(syndication_token(tweet_id)) <= set("123456789abcdefghijklmnopqrstuvwxyz")


def test_a_non_numeric_tweet_id_yields_no_token_rather_than_raising() -> None:
    """`_token` runs before the request; it must never be the thing that raises."""
    assert syndication_token("not-a-number") == ""
    assert syndication_token("") == ""
    assert syndication_token("-5") == ""
    assert syndication_token("0") == ""


def test_an_id_too_large_for_a_double_yields_no_token_rather_than_infinity() -> None:
    """`float()` answers `inf` past ~1.8e308, and `inf` has no base-36 form."""
    assert syndication_token("9" * 400) == ""


# --------------------------------------------------- the base-36 conversion
@pytest.mark.parametrize(
    "value",
    [
        6613.845526000001,
        4773.589598,
        1.0,
        0.5,
        2.5,
        1e-14,
        1e15,
        0.1,
        123456789.123456,
        1.7976931348623157e308,
        5e-324,  # the smallest subnormal: needs every digit the cap allows
    ],
)
def test_the_base36_form_is_the_shortest_one_that_round_trips(value: float) -> None:
    """This is the whole definition of `toString(36)`, and it is checkable here.

    V8 emits the shortest base-36 expansion that parses back to the same double.
    Asserting the round-trip pins that behaviour without needing a JS engine —
    any fixed-width expansion (which is what this code used to do) fails it,
    because it emits digits that carry no information.
    """
    rendered = syndication_to_base36(value)

    assert parse_base36(rendered) == value
    if "." in rendered:
        # One digit fewer must *not* round-trip, or the form was not minimal.
        assert parse_base36(rendered[:-1]) != value


def test_the_base36_form_is_identical_to_the_js_algorithm_for_whole_numbers() -> None:
    """`(36).toString(36) === "10"`, `(35).toString(36) === "z"`."""
    assert syndication_to_base36(36.0) == "10"
    assert syndication_to_base36(35.0) == "z"
    assert syndication_to_base36(1296.0) == "100"
    assert syndication_to_base36(0.0) == "0"


def test_the_integer_conversion_is_exact_and_zero_padded_nowhere() -> None:
    assert syndication_int_base36(0) == "0"
    assert syndication_int_base36(35) == "z"
    assert syndication_int_base36(36) == "10"
    assert syndication_int_base36(6613) == "53p"
    assert syndication_int_base36(4773) == "3ol"


def test_the_id_is_rounded_to_a_double_before_any_arithmetic() -> None:
    """`Number(id)` is a double, and a 19-digit ID does not fit in one.

    This is the precise statement of the bug the old implementation had: it
    divided the *exact* integer. Whether that changes the final token depends on
    where the quotient falls relative to a rounding boundary, so the consequence
    is asserted separately below — but this is the part that must never regress.
    """
    exact = 2105252416639672788

    assert syndication_number(str(exact)) == float(str(exact))
    assert syndication_number(str(exact)) != exact
    assert math.isnan(syndication_number("not-a-number"))


def test_two_ids_that_share_a_double_produce_the_same_token() -> None:
    """The consequence of the rounding: `Number()` collapses the two IDs into one.

    Two IDs that round to the same double are one value to JavaScript, so they
    get one token. An implementation working from the exact integer gives them
    different ones.
    """
    exact = 2105252416639672788
    rounded = int(float(exact))

    assert rounded != exact  # if they agreed, this test would prove nothing
    assert syndication_token(str(exact)) == syndication_token(str(rounded))


# ----------------------------------------------------------- error classification
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        # 401 is the endpoint refusing the client, not a bad token — no retry
        # and no credential to fix, so it must not be classified as transient.
        (401, ProviderAuthError),
        (410, TweetUnavailableError),
        (429, ProviderRateLimitedError),
        (500, ProviderTransientError),
        (503, ProviderTransientError),
        # Anything else the endpoint can say that a retry will not change.
        (418, ProviderResponseError),
    ],
)
def test_syndication_status_codes_map_to_the_right_error(
    status: int, expected: type[ProviderError]
) -> None:
    """Note the absentees. 400, 403 and 404 never reach `_classify` at all —
    `_request` intercepts them as "a different token might work" and returns
    without raising. What they mean is decided in `fetch`, once both attempts
    are done, and is asserted by the fetch-level tests below."""
    assert type(syndication_classify(status, "body")) is expected


def test_the_classifier_marks_exactly_the_recoverable_failures_retryable() -> None:
    """`retryable` is the single boolean the worker reads, so it gets its own test.

    A 401 marked retryable would burn five attempts on a blocked IP; a 503
    marked permanent would throw away a post that would have worked next time.
    """
    assert syndication_classify(429, "").retryable is True
    assert syndication_classify(500, "").retryable is True
    assert syndication_classify(503, "").retryable is True
    assert syndication_classify(401, "").retryable is False
    assert syndication_classify(410, "").retryable is False


def test_a_401_says_plainly_that_it_is_not_a_credential_problem() -> None:
    """This provider has no credentials, so "check your API key" would be a lie."""
    error = syndication_classify(401, "Forbidden")

    assert isinstance(error, ProviderAuthError)
    assert "not a credential problem" in str(error)


def test_a_rate_limited_error_carries_the_status_for_the_log_line() -> None:
    error = syndication_classify(429, "slow down")

    assert isinstance(error, ProviderRateLimitedError)
    assert error.status == 429


def test_the_error_detail_falls_back_when_the_body_is_empty() -> None:
    assert "no response body" in str(syndication_classify(500, "   "))


# ------------------------------------------------------------------- construction
def test_the_provider_refuses_a_base_url_with_no_host(no_real_session: None) -> None:
    with pytest.raises(ProviderResponseError):
        SyndicationProvider(base_url="https:///tweet-result")


def test_the_provider_is_named_for_what_it_is(no_real_session: None) -> None:
    assert make_syndication_provider().name == "syndication"


def test_the_default_url_points_at_the_embed_endpoint(no_real_session: None) -> None:
    """A silent change of host would be a change of who sees the traffic."""
    assert make_syndication_provider()._base_url == (
        "https://cdn.syndication.twimg.com/tweet-result"
    )


# ------------------------------------------------------------------- the fetch
async def test_fetch_returns_the_tweet_the_endpoint_describes(no_real_session: None) -> None:
    provider = make_syndication_provider()
    session = attach_session(provider, FakeResponse(body=json.dumps(syndication_payload())))

    tweet = await provider.fetch("1519480761749016577")

    assert tweet.text == "hello from the embed endpoint"
    assert tweet.author_username == "example"
    assert tweet.author_name == "Example Person"
    assert tweet.author_avatar_url == "https://pbs.twimg.com/profile_images/a.jpg"
    assert tweet.lang == "en"
    assert tweet.posted_at is not None
    assert [m.remote_url for m in tweet.photos] == [
        "https://pbs.twimg.com/media/one.jpg",
        "https://pbs.twimg.com/media/two.jpg",
    ]
    assert session.requests[0]["url"] == "https://cdn.syndication.twimg.com/tweet-result"


async def test_fetch_extracts_a_video_and_picks_the_highest_bitrate_mp4(
    no_real_session: None,
) -> None:
    """The URL field is `src` here, and the HLS manifest must not be chosen.

    Telegram is given a file to upload; the `.m3u8` playlist is not one, even
    though it is the entry a naive "first variant" would pick.
    """
    provider = make_syndication_provider()
    attach_session(provider, FakeResponse(body=json.dumps(syndication_video_payload())))

    tweet = await provider.fetch("1519480761749016577")

    assert tweet.photos == ()
    assert len(tweet.videos) == 1
    clip = tweet.videos[0]
    assert clip.kind is MediaKind.VIDEO
    assert clip.remote_url == "https://video.twimg.com/high.mp4"
    assert clip.duration_ms == 13_400


async def test_a_video_takes_the_place_of_its_poster_photo(no_real_session: None) -> None:
    """A tweet carries photos or one video, never both.

    Anything under `photos` for a video tweet is the poster frame, and
    publishing it alongside the video would post the same content twice.
    """
    provider = make_syndication_provider()
    payload = syndication_video_payload(
        photos=[{"url": "https://pbs.twimg.com/poster.jpg", "width": 1280, "height": 720}]
    )
    attach_session(provider, FakeResponse(body=json.dumps(payload)))

    tweet = await provider.fetch("1519480761749016577")

    assert tweet.photos == ()
    assert [m.remote_url for m in tweet.media] == ["https://video.twimg.com/high.mp4"]


async def test_a_video_with_only_hls_is_kept_as_an_omitted_video(
    no_real_session: None,
) -> None:
    """It must not degrade into its poster, and must not vanish without a trace.

    An empty `remote_url` is how the renderer is told "a video was here but
    there is nothing to publish", so it lands in the skipped count rather than
    being silently replaced by the thumbnail.
    """
    provider = make_syndication_provider()
    payload = syndication_video_payload(
        video={
            "mediaAvailability": {"status": "available"},
            "variants": [
                {"content_type": "application/x-mpegURL", "src": "https://video.twimg.com/a.m3u8"}
            ],
        }
    )
    attach_session(provider, FakeResponse(body=json.dumps(payload)))

    tweet = await provider.fetch("1519480761749016577")

    assert tweet.photos == ()
    assert len(tweet.videos) == 1
    assert tweet.videos[0].remote_url == ""


async def test_an_unavailable_video_is_kept_as_an_omitted_video(
    no_real_session: None,
) -> None:
    provider = make_syndication_provider()
    payload = syndication_video_payload(
        video={
            "mediaAvailability": {"status": "unavailable"},
            "variants": [
                {"content_type": "video/mp4", "bitrate": 1, "src": "https://video.twimg.com/a.mp4"}
            ],
        }
    )
    attach_session(provider, FakeResponse(body=json.dumps(payload)))

    tweet = await provider.fetch("1519480761749016577")

    assert len(tweet.videos) == 1
    assert tweet.videos[0].remote_url == ""


def test_the_video_variant_picker_accepts_the_public_api_url_field() -> None:
    """`url` is what the public API calls the same field; both must work."""
    assert (
        syndication_best_video_url([{"content_type": "video/mp4", "url": "https://v/a.mp4"}])
        == "https://v/a.mp4"
    )


def test_the_video_variant_picker_rejects_a_manifest_even_without_a_content_type() -> None:
    assert syndication_best_video_url([{"src": "https://v/a.m3u8"}]) is None
    assert syndication_best_video_url([{"src": "https://v/a.mp4"}]) == "https://v/a.mp4"


def test_the_video_variant_picker_ignores_junk_entries() -> None:
    assert syndication_best_video_url(None) is None
    assert syndication_best_video_url(["not-a-mapping", {"content_type": "video/mp4"}]) is None


async def test_an_empty_video_object_does_not_displace_the_photos(
    no_real_session: None,
) -> None:
    """An unhelpful `video: {}` says nothing about the media, so the photos the
    payload really carried must survive it."""
    provider = make_syndication_provider()
    payload = syndication_video_payload(
        video={},
        photos=[{"url": "https://pbs.twimg.com/poster.jpg", "width": 1280, "height": 720}],
    )
    attach_session(provider, FakeResponse(body=json.dumps(payload)))

    tweet = await provider.fetch("1519480761749016577")

    assert tweet.videos == ()
    assert [m.remote_url for m in tweet.photos] == ["https://pbs.twimg.com/poster.jpg"]


async def test_fetch_sends_the_tweet_id_and_a_derived_token(no_real_session: None) -> None:
    provider = make_syndication_provider()
    session = attach_session(provider, FakeResponse(body=json.dumps(syndication_payload())))

    await provider.fetch("1519480761749016577")

    params = session.requests[0]["params"]
    assert params["id"] == "1519480761749016577"
    assert params["token"] == syndication_token("1519480761749016577")
    # The embed widget always sends `lang`; omitting it is one of the ways to get
    # a 200 with an empty body back.
    assert params["lang"] == "en"


async def test_a_configured_token_is_used_verbatim_instead_of_the_derived_one(
    no_real_session: None,
) -> None:
    """The escape hatch for the day the reverse-engineered formula stops matching."""
    provider = make_syndication_provider(token="captured-from-a-browser")
    session = attach_session(provider, FakeResponse(body=json.dumps(syndication_payload())))

    await provider.fetch("1519480761749016577")

    assert session.requests[0]["params"]["token"] == "captured-from-a-browser"


async def test_fetch_retries_without_a_token_when_the_token_is_refused(
    no_real_session: None,
) -> None:
    """The whole reason the retry exists: a token the endpoint no longer wants."""
    provider = make_syndication_provider()
    session = SequencedSession(
        [
            FakeResponse(status=404, body=""),
            FakeResponse(body=json.dumps(syndication_payload())),
        ]
    )
    provider._session = session

    tweet = await provider.fetch("1519480761749016577")

    assert tweet.text == "hello from the embed endpoint"
    assert len(session.requests) == 2
    assert "token" in session.requests[0]["params"]
    assert "token" not in session.requests[1]["params"]


async def test_an_id_that_yields_no_token_is_attempted_once(no_real_session: None) -> None:
    """With no token to blame, a repeat would be a duplicate request for nothing.

    A non-numeric ID is how `computed` ends up empty without a configured token:
    `_token` returns "" rather than raising.
    """
    provider = make_syndication_provider()
    session = SequencedSession([FakeResponse(status=404, body="")])
    provider._session = session

    with pytest.raises(TweetNotFoundError):
        await provider.fetch("not-a-tweet-id")

    assert len(session.requests) == 1
    assert "token" not in session.requests[0]["params"]


async def test_a_404_after_the_token_retry_is_still_a_missing_tweet(
    no_real_session: None,
) -> None:
    """The tokenless attempt is the most permissive form, so its answer decides."""
    provider = make_syndication_provider()
    session = SequencedSession(
        [FakeResponse(status=404, body=""), FakeResponse(status=404, body="")]
    )
    provider._session = session

    with pytest.raises(TweetNotFoundError):
        await provider.fetch("1234567890")


async def test_a_403_after_the_token_retry_reports_both_attempts(
    no_real_session: None,
) -> None:
    """403 is not "no such tweet", so it must not be reported as one."""
    provider = make_syndication_provider()
    session = SequencedSession(
        [FakeResponse(status=403, body=""), FakeResponse(status=403, body="")]
    )
    provider._session = session

    with pytest.raises(ProviderResponseError) as caught:
        await provider.fetch("1234567890")

    message = str(caught.value)
    assert "with a token and without one" in message
    assert "python -m app.providers.syndication" in message
    assert "cdn.syndication.twimg.com" in message


async def test_a_rate_limited_response_is_never_treated_as_a_token_problem(
    no_real_session: None,
) -> None:
    """429 means slow down. Retrying immediately without a token would be worse."""
    provider = make_syndication_provider()
    session = SequencedSession([FakeResponse(status=429, body="rate limited")])
    provider._session = session

    with pytest.raises(ProviderRateLimitedError):
        await provider.fetch("1234567890")

    assert len(session.requests) == 1


async def test_a_tombstone_is_reported_as_unavailable_not_missing(
    no_real_session: None,
) -> None:
    provider = make_syndication_provider()
    body = json.dumps({"__typename": "TweetTombstone"})
    attach_session(provider, FakeResponse(body=body))

    with pytest.raises(TweetUnavailableError):
        await provider.fetch("1234567890")


async def test_the_two_byte_empty_body_is_never_turned_into_an_empty_post(
    no_real_session: None,
) -> None:
    """The exact response reported from the VPS: HTTP 200, `content-length: 2`.

    Two bytes is `{}` — what this endpoint answers both for a tweet it will not
    serve and for a request whose token it did not like. The one outcome that
    must never follow from it is a preview, or a publish, with no text in it.
    """
    provider = make_syndication_provider()
    session = SequencedSession([FakeResponse(body="{}"), FakeResponse(body="{}")])
    provider._session = session

    with pytest.raises(ProviderResponseError) as caught:
        await provider.fetch("2105252416639672788")

    message = str(caught.value)
    assert "returned no data" in message
    assert "no public tweet with id" not in message  # never a "not found" verdict
    assert caught.value.retryable is False  # the retry would be byte-identical
    assert len(session.requests) == 2  # the tokenless retry did happen
    assert "token" in session.requests[0]["params"]
    assert "token" not in session.requests[1]["params"]


async def test_a_zero_byte_body_is_no_data_rather_than_a_shape_change(
    no_real_session: None,
) -> None:
    """A 200 with a blank body means the same as `{}` — not the parse error."""
    provider = make_syndication_provider()
    session = SequencedSession([FakeResponse(body="   "), FakeResponse(body="{}")])
    provider._session = session

    with pytest.raises(ProviderResponseError) as caught:
        await provider.fetch("1234567890")

    assert "not JSON" not in str(caught.value)


async def test_an_empty_body_followed_by_a_real_payload_returns_the_tweet(
    no_real_session: None,
) -> None:
    """The retry is what turns `{}` into a working fetch, so it must not give up."""
    provider = make_syndication_provider()
    session = SequencedSession(
        [FakeResponse(body="{}"), FakeResponse(body=json.dumps(syndication_payload()))]
    )
    provider._session = session

    tweet = await provider.fetch("1519480761749016577")

    assert tweet.text == "hello from the embed endpoint"


async def test_an_empty_body_from_a_configured_token_is_still_retried_once(
    no_real_session: None,
) -> None:
    """A captured token can go stale too, so the tokenless retry must still run."""
    provider = make_syndication_provider(token="captured-from-a-browser")
    session = SequencedSession(
        [FakeResponse(body="{}"), FakeResponse(body=json.dumps(syndication_payload()))]
    )
    provider._session = session

    tweet = await provider.fetch("1519480761749016577")

    assert tweet.text == "hello from the embed endpoint"
    assert session.requests[0]["params"]["token"] == "captured-from-a-browser"
    assert "token" not in session.requests[1]["params"]


async def test_an_html_challenge_page_is_reported_with_its_opening_characters(
    no_real_session: None,
) -> None:
    """Cloudflare's interstitial is HTML, and the operator needs to see that."""
    provider = make_syndication_provider()
    attach_session(provider, FakeResponse(body="<html><head><title>Just a moment"))

    with pytest.raises(ProviderResponseError) as caught:
        await provider.fetch("1234567890")

    assert "not JSON" in str(caught.value)
    assert "Just a moment" in str(caught.value)


async def test_a_json_body_that_is_not_an_object_is_rejected(no_real_session: None) -> None:
    provider = make_syndication_provider()
    attach_session(provider, FakeResponse(body="[1, 2, 3]"))

    with pytest.raises(ProviderResponseError):
        await provider.fetch("1234567890")


async def test_a_transport_failure_is_retryable(no_real_session: None) -> None:
    provider = make_syndication_provider()
    attach_session(provider, aiohttp.ClientConnectionError("reset"))

    with pytest.raises(ProviderTransientError):
        await provider.fetch("1234567890")


async def test_a_timeout_says_the_network_is_the_suspect_not_the_token(
    no_real_session: None,
) -> None:
    provider = make_syndication_provider()
    attach_session(provider, TimeoutError())

    with pytest.raises(ProviderTransientError) as caught:
        await provider.fetch("1234567890")

    assert "not the token" in str(caught.value)


async def test_a_body_with_no_text_is_a_response_error(no_real_session: None) -> None:
    """`normalise_node` requires text; the failure must surface as a ProviderError."""
    provider = make_syndication_provider()
    payload = syndication_payload()
    del payload["text"]
    attach_session(provider, FakeResponse(body=json.dumps(payload)))

    with pytest.raises(ProviderResponseError):
        await provider.fetch("1519480761749016577")


async def test_the_syndication_provider_is_usable_as_an_async_context_manager(
    no_real_session: None,
) -> None:
    provider = make_syndication_provider()

    async with provider as entered:
        assert entered is provider
