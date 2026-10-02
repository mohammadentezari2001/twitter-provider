"""Official X (Twitter) API v2 provider.

The only provider in this build that is unambiguously licensed for this use. It
requires a paid API plan: the free tier does not include `GET /2/tweets/:id`
with the expansions used here.

    TWEET_PROVIDER=x_api
    TWEET_API_KEY=<bearer token from the X developer portal>

Requests one tweet with author and media expansions in a single call, which is
the minimum number of requests the endpoint allows for a complete post.

Two behaviours worth knowing about:

* **Response errors are classified, not just reported.** `retryable` on the
  raised `ProviderError` is what decides whether the job backs off or dies, so
  mapping 401 to "retry" would burn the retry budget on a credential that is
  never going to become valid.
* **Long posts.** Posts over the classic 280-character limit return their full
  body under `note_tweet.text`; `data.text` holds a truncated version. The
  longer one is preferred, because publishing the truncation would be a silent
  data loss.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Mapping
from urllib.parse import urlsplit

import aiohttp

from app.database.enums import MediaKind
from app.logging_config import get_logger
from app.providers.base import (
    MediaData,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderTransientError,
    TweetData,
    TweetNotFoundError,
    TweetProvider,
    TweetUnavailableError,
)
from app.utils.net import UrlPolicy, create_session
from app.utils.timeutil import utcnow

log = get_logger(__name__)

X_API_BASE = "https://api.x.com/2"

TWEET_FIELDS = "created_at,lang,text,entities,attachments,author_id,public_metrics,note_tweet"
EXPANSIONS = "author_id,attachments.media_keys"
MEDIA_FIELDS = "url,preview_image_url,type,width,height,alt_text,duration_ms,variants"
USER_FIELDS = "id,name,username,profile_image_url"

_MEDIA_KIND = {
    "photo": MediaKind.PHOTO,
    "video": MediaKind.VIDEO,
    "animated_gif": MediaKind.ANIMATION,
}

#: Substrings that turn a 403 into "this tweet cannot be shown" rather than
#: "your credentials are wrong". X uses 403 for both, and only the body
#: distinguishes them.
_UNAVAILABLE_MARKERS = (
    "protected",
    "not authorized",
    "suspended",
    "withheld",
    "deleted",
)


class XApiProvider(TweetProvider):
    name = "x_api"

    def __init__(self, *, bearer_token: str, timeout: float = 15.0) -> None:
        if not bearer_token.strip():
            # Fail here rather than at the first fetch: an unauthenticated
            # provider is a configuration mistake, and discovering it when an
            # admin pastes a link is a worse time to find out.
            raise ProviderAuthError("TWEET_API_KEY is required for TWEET_PROVIDER=x_api")

        self._token = bearer_token.strip()
        host = urlsplit(X_API_BASE).hostname or "api.x.com"
        self._session = create_session(
            UrlPolicy(allowed_hosts=(host,), allow_private=False),
            connect_timeout=min(timeout, 10.0),
            total_timeout=timeout,
            limit=8,
        )

    async def aclose(self) -> None:
        await self._session.close()

    # ------------------------------------------------------------------ fetch

    async def fetch(self, tweet_id: str) -> TweetData:
        url = f"{X_API_BASE}/tweets/{tweet_id}"
        params = {
            "tweet.fields": TWEET_FIELDS,
            "expansions": EXPANSIONS,
            "media.fields": MEDIA_FIELDS,
            "user.fields": USER_FIELDS,
        }
        headers = {"Authorization": f"Bearer {self._token}"}

        try:
            response = await self._session.get(url, params=params, headers=headers)
            async with response:
                raw = await response.text()
                if response.status != 200:
                    raise _classify(response.status, raw, dict(response.headers))
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderTransientError("X API timed out") from exc
        except aiohttp.ClientError as exc:
            # Covers DNS failures, TLS errors, resets — none of which are the
            # tweet's fault, so all of them are worth retrying.
            raise ProviderTransientError(f"X API transport error: {exc!r}") from exc

        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise ProviderResponseError(
                f"X API returned a body that is not JSON ({len(raw)} bytes)"
            ) from exc

        if not isinstance(payload, Mapping):
            raise ProviderResponseError("X API returned a JSON value that is not an object")

        return _normalise(payload, tweet_id)


# --------------------------------------------------------------- error mapping
def _classify(status: int, body: str, headers: Mapping[str, str]) -> ProviderError:
    detail = _error_detail(body)
    if status == 401:
        return ProviderAuthError(
            f"X API rejected the bearer token: {detail}", status=status
        )
    if status == 403:
        lowered = detail.lower()
        if any(marker in lowered for marker in _UNAVAILABLE_MARKERS):
            return TweetUnavailableError(
                f"tweet is not retrievable: {detail}", status=status
            )
        # 403 without those words is an entitlement problem: the token is valid
        # but the plan does not cover this endpoint.
        return ProviderAuthError(
            f"X API refused access (is the API plan sufficient?): {detail}", status=status
        )
    if status == 404:
        return TweetNotFoundError(f"tweet not found: {detail}", status=status)
    if status == 429:
        return ProviderRateLimitedError(
            f"X API rate limit reached: {detail}",
            status=status,
            retry_after=_retry_after(headers),
        )
    if status >= 500:
        return ProviderTransientError(f"X API returned {status}: {detail}", status=status)
    return ProviderResponseError(f"X API returned {status}: {detail}", status=status)


def _error_detail(body: str) -> str:
    """Pull `errors[0].detail`/`.title` out of an error body, or fall back."""
    try:
        payload = json.loads(body)
    except ValueError:
        return body.strip()[:200] or "no response body"
    if isinstance(payload, Mapping):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, Mapping):
                for key in ("detail", "title", "message"):
                    value = first.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()[:200]
        for key in ("detail", "title", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
    return body.strip()[:200] or "no response body"


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds to wait, from either rate-limit header X may send."""
    lowered = {k.lower(): v for k, v in headers.items()}

    raw_reset = lowered.get("x-rate-limit-reset")
    if raw_reset and raw_reset.strip().isdigit():
        # An absolute epoch timestamp, not a duration.
        return max(0.0, float(raw_reset) - utcnow().timestamp())

    raw_after = lowered.get("retry-after")
    if raw_after:
        if raw_after.strip().isdigit():
            return float(raw_after)
        try:
            when = parsedate_to_datetime(raw_after)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - utcnow()).total_seconds())
    return None


# ----------------------------------------------------------------- normalising
def _normalise(payload: Mapping[str, Any], tweet_id: str) -> TweetData:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        # A 200 with no `data` is how X reports "no such tweet" on some plans.
        raise TweetNotFoundError(
            f"X API returned no tweet for {tweet_id}: {_error_detail(json.dumps(payload))}"
        )

    includes = payload.get("includes")
    includes = includes if isinstance(includes, Mapping) else {}

    users = _by_key(includes.get("users"), "id")
    media_objects = includes.get("media")
    media_objects = media_objects if isinstance(media_objects, list) else []

    author = None
    author_id = data.get("author_id")
    if author_id is not None:
        author = users.get(str(author_id))

    # `note_tweet.text` is the untruncated body for long posts; `text` is cut to
    # the classic limit. Preferring the longer one avoids silently publishing a
    # truncated quote.
    note = data.get("note_tweet")
    note_text = note.get("text") if isinstance(note, Mapping) else None
    text = note_text if isinstance(note_text, str) and note_text else data.get("text")
    if not isinstance(text, str):
        raise ProviderResponseError(f"X API returned no text for tweet {tweet_id}")

    url_map: dict[str, str] = {}
    entities = data.get("entities")
    if isinstance(entities, Mapping):
        for entry in _as_list(entities.get("urls")):
            if not isinstance(entry, Mapping):
                continue
            short = entry.get("url")
            expanded = entry.get("expanded_url") or entry.get("unwound_url")
            if isinstance(short, str) and isinstance(expanded, str):
                url_map[short] = expanded

    media = _media_items(data, media_objects)

    metrics: dict[str, int] = {}
    public_metrics = data.get("public_metrics")
    if isinstance(public_metrics, Mapping):
        for key, value in public_metrics.items():
            if isinstance(value, int) and not isinstance(value, bool):
                metrics[str(key)] = value

    return TweetData(
        tweet_id=str(data.get("id") or tweet_id),
        text=text,
        author_username=_as_str(author, "username"),
        author_name=_as_str(author, "name"),
        author_id=str(author_id) if author_id is not None else None,
        author_avatar_url=_as_str(author, "profile_image_url"),
        lang=_as_str(data, "lang"),
        posted_at=_parse_timestamp(data.get("created_at")),
        media=media,
        url_map=url_map,
        metrics=metrics,
        raw=_pruned(data, media_objects, author),
    )


def _media_items(
    data: Mapping[str, Any], media_objects: list[Any]
) -> tuple[MediaData, ...]:
    """Build media in the tweet's own order.

    `attachments.media_keys` is the authoritative order; `includes.media` is not
    guaranteed to match it, and a photo set rendered out of order is a visible
    defect. The key order is used when present, and the includes order only as a
    fallback.
    """
    by_key = _by_key(media_objects, "media_key")

    attachments = data.get("attachments")
    keys: list[str] = []
    if isinstance(attachments, Mapping):
        keys = [k for k in _as_list(attachments.get("media_keys")) if isinstance(k, str)]

    if keys:
        ordered = [by_key[k] for k in keys if k in by_key]
    else:
        ordered = [m for m in media_objects if isinstance(m, Mapping)]

    items: list[MediaData] = []
    for index, obj in enumerate(ordered):
        item = _media_item(obj, index)
        if item is not None:
            items.append(item)
    return tuple(items)


def _media_item(obj: Mapping[str, Any], index: int) -> MediaData | None:
    raw_type = obj.get("type")
    kind = _MEDIA_KIND.get(str(raw_type))
    if kind is None:
        # An unknown media type is skipped rather than published as a broken
        # image; the count mismatch is visible in the preview.
        log.debug("skipping unsupported X media type", extra={"media_type": raw_type})
        return None

    if kind is MediaKind.PHOTO:
        remote_url = obj.get("url") or obj.get("preview_image_url")
    else:
        remote_url = _best_mp4(obj) or obj.get("preview_image_url") or obj.get("url")

    if not isinstance(remote_url, str) or not remote_url:
        return None

    return MediaData(
        position=index,
        kind=kind,
        remote_url=remote_url,
        content_type="image/jpeg" if kind is MediaKind.PHOTO else "video/mp4",
        width=_as_int(obj.get("width")),
        height=_as_int(obj.get("height")),
        duration_ms=_as_int(obj.get("duration_ms")),
        alt_text=_as_str(obj, "alt_text"),
    )


def _best_mp4(obj: Mapping[str, Any]) -> str | None:
    """Highest-bitrate mp4 variant.

    X lists several renditions plus an HLS `.m3u8`; only mp4 is usable as a
    Telegram upload, and the highest bitrate is the closest to the original.
    """
    video_info = obj.get("video_info")
    if not isinstance(video_info, Mapping):
        return None
    best_url: str | None = None
    best_bitrate = -1
    for variant in _as_list(video_info.get("variants")):
        if not isinstance(variant, Mapping):
            continue
        url = variant.get("url")
        if not isinstance(url, str) or variant.get("content_type") != "video/mp4":
            continue
        bitrate = _as_int(variant.get("bitrate")) or 0
        if bitrate > best_bitrate:
            best_bitrate = bitrate
            best_url = url
    return best_url


def _pruned(
    data: Mapping[str, Any], media_objects: list[Any], author: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The subset stored in `tweets.payload`.

    `note_tweet` is dropped once its text has been lifted into the column: the
    long-form object also contains inline entity offsets that would roughly
    double the stored size for no benefit.
    """
    payload: dict[str, Any] = {k: v for k, v in data.items() if k != "note_tweet"}
    if media_objects:
        payload["_media"] = media_objects
    if author is not None:
        payload["_author"] = dict(author)
    return payload


# --------------------------------------------------------------------- helpers
def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_str(mapping: Mapping[str, Any] | None, key: str) -> str | None:
    if not isinstance(mapping, Mapping):
        return None
    value = mapping.get(key)
    return value if isinstance(value, str) and value else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _by_key(items: Any, key: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for item in _as_list(items):
        if isinstance(item, Mapping):
            value = item.get(key)
            if isinstance(value, str):
                result[value] = item
    return result


def _parse_timestamp(value: Any) -> datetime | None:
    """X sends RFC 3339. Anything else is treated as absent rather than fatal."""
    if not isinstance(value, str) or not value:
        return None
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        log.debug("unparseable timestamp from X API", extra={"value": value[:40]})
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
