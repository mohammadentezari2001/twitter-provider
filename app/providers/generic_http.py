"""Configurable REST provider for a licensed third-party tweet API.

This is the escape hatch for the case that actually matters in practice: you have
a contract with a provider that is neither X nor one of the free mirrors, and it
returns something close to X's shape but not identical. Rather than the operator
needing a code change, they point this at it and describe the shape with five
settings:

    TWEET_PROVIDER=generic_http
    TWEET_API_BASE_URL=https://api.example-provider.com/v1
    TWEET_API_TWEET_PATH=/tweets/{tweet_id}
    TWEET_API_AUTH_HEADER=Authorization
    TWEET_API_AUTH_SCHEME=Bearer
    TWEET_API_RESPONSE_ROOT=data

## The expected response

`TWEET_API_RESPONSE_ROOT` is a dotted path to the tweet object inside the JSON
body (`data`, `tweet`, `result.tweet`, or empty for the body itself). Within that
object, the normaliser accepts the field names real APIs use, preferring the
first of each group that is present:

| Field | Accepted keys |
|---|---|
| id | `id_str`, `id`, `tweet_id`, `rest_id` |
| text | `note_tweet.text`, `full_text`, `text`, `content`, `description` |
| created_at | `created_at`, `date`, `posted_at`, `timestamp` (ISO 8601, RFC 2822, or Unix seconds) |
| lang | `lang`, `language` |
| author | `user`, `author`, `account` — with `screen_name`/`username`/`handle`, `name`/`display_name`, `profile_image_url_https`/`avatar` |
| media | `extendedEntities.media`, `extended_entities.media`, `entities.media`, `media`, `photos`, `attachments.media` |
| link expansion | `entities.url.urls[].{url,expanded_url}`, `entities.urls[].urls[]` |

Media entries may be objects (`media_url_https`, `type`, `sizes`, `alt_text`,
`video_info.variants`) or bare URL strings. Photo URLs must be on a host in
`MEDIA_ALLOWED_HOSTS` or the download will be refused by the SSRF policy —
**add your provider's CDN host to that list** or no images will publish.

## What this provider will not do

It does not implement a provider's pagination, search or thread-reconstruction
endpoints, and it does not retry internally. One ID in, one tweet out.

**Check the provider's terms before pointing this at it.** Redistributing
someone's posts to a public channel is exactly the use most APIs restrict, and
"the endpoint answered" is not the same as "this is permitted".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
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

log = get_logger(__name__)

_ID_KEYS = ("id_str", "id", "tweet_id", "rest_id")
_TEXT_KEYS = ("full_text", "text", "content", "description")
_TIME_KEYS = ("created_at", "date", "posted_at", "timestamp", "time")
_LANG_KEYS = ("lang", "language")
_USER_KEYS = ("user", "author", "account", "user_info")
_MEDIA_CONTAINER_KEYS = (
    "extendedEntities",
    "extended_entities",
    "media",
    "photos",
    "attachments",
    "media_items",
)
_MEDIA_LIST_KEYS = ("media", "photos", "items", "media_items", "results")
_USERNAME_KEYS = ("screen_name", "username", "handle", "user_name", "login")
_NAME_KEYS = ("name", "display_name", "full_name", "nickname")
_AVATAR_KEYS = (
    "profile_image_url_https",
    "profile_image_url",
    "avatar",
    "avatar_url",
    "profile_picture",
)
_PHOTO_URL_KEYS = ("media_url_https", "media_url", "url", "url_https", "src", "image")
_ALT_KEYS = ("alt_text", "altText", "description")
_TYPE_KEYS = ("type", "kind", "media_type")
_MEDIA_KIND = {
    "photo": MediaKind.PHOTO,
    "image": MediaKind.PHOTO,
    "video": MediaKind.VIDEO,
    "animated_gif": MediaKind.ANIMATION,
    "gif": MediaKind.ANIMATION,
}

_UNAVAILABLE_MARKERS = ("protected", "suspended", "not authorized", "withheld", "deleted")


class GenericHttpProvider(TweetProvider):
    name = "generic_http"

    def __init__(
        self,
        *,
        base_url: str,
        tweet_path: str = "/tweets/{tweet_id}",
        api_key: str = "",
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
        response_root: str = "data",
        timeout: float = 15.0,
    ) -> None:
        if not base_url.strip():
            raise ProviderResponseError("TWEET_API_BASE_URL is required")
        if "{tweet_id}" not in tweet_path:
            raise ProviderResponseError("TWEET_API_TWEET_PATH must contain '{tweet_id}'")

        self._base_url = base_url.rstrip("/")
        self._tweet_path = tweet_path
        self._api_key = api_key.strip()
        self._auth_header = auth_header.strip()
        self._auth_scheme = auth_scheme.strip()
        self._response_root = response_root.strip()

        host = urlsplit(self._base_url).hostname
        if not host:
            raise ProviderResponseError(f"TWEET_API_BASE_URL {base_url!r} has no hostname")

        self._session = create_session(
            UrlPolicy(allowed_hosts=(host,), allow_private=False),
            connect_timeout=min(timeout, 10.0),
            total_timeout=timeout,
            limit=8,
        )

    async def aclose(self) -> None:
        await self._session.close()

    # ------------------------------------------------------------------ fetch

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            return {"Accept": "application/json"}
        value = (
            f"{self._auth_scheme} {self._api_key}".strip()
            if self._auth_scheme
            else self._api_key
        )
        return {"Accept": "application/json", self._auth_header: value}

    async def fetch(self, tweet_id: str) -> TweetData:
        path = self._tweet_path.replace("{tweet_id}", tweet_id)
        url = f"{self._base_url}{path}"

        try:
            response = await self._session.get(url, headers=self._headers())
            async with response:
                raw = await response.text()
                if response.status != 200:
                    raise _classify(response.status, raw, dict(response.headers))
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderTransientError("provider timed out") from exc
        except aiohttp.ClientError as exc:
            raise ProviderTransientError(f"provider transport error: {exc!r}") from exc

        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise ProviderResponseError(
                f"provider returned a body that is not JSON ({len(raw)} bytes)"
            ) from exc

        node = _dig(payload, self._response_root)
        if not isinstance(node, Mapping):
            raise ProviderResponseError(
                f"no tweet object at response root {self._response_root!r}; "
                f"top-level keys were {sorted(payload)[:10] if isinstance(payload, Mapping) else type(payload).__name__}"
            )

        return normalise_node(node, tweet_id, response_root=self._response_root)


# --------------------------------------------------------------- error mapping
def _classify(status: int, body: str, headers: Mapping[str, str]) -> ProviderError:
    detail = _error_detail(body)

    if status in (401, 403):
        lowered = detail.lower()
        if any(marker in lowered for marker in _UNAVAILABLE_MARKERS):
            return TweetUnavailableError(f"tweet is not retrievable: {detail}", status=status)
        return ProviderAuthError(f"provider rejected the credentials: {detail}", status=status)
    if status == 404:
        return TweetNotFoundError(f"tweet not found: {detail}", status=status)
    if status == 410:
        return TweetUnavailableError(f"tweet was deleted: {detail}", status=status)
    if status == 429:
        return ProviderRateLimitedError(
            f"provider rate limit reached: {detail}",
            status=status,
            retry_after=_retry_after(headers),
        )
    if status >= 500:
        return ProviderTransientError(f"provider returned {status}: {detail}", status=status)
    return ProviderResponseError(f"provider returned {status}: {detail}", status=status)


def _error_detail(body: str) -> str:
    try:
        payload = json.loads(body)
    except ValueError:
        return body.strip()[:200] or "no response body"

    if isinstance(payload, Mapping):
        for key in ("error", "message", "detail", "description", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
            if isinstance(value, Mapping):
                inner = value.get("message") or value.get("detail")
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()[:200]
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, str):
                return first[:200]
            if isinstance(first, Mapping):
                for key in ("message", "detail", "title"):
                    value = first.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()[:200]
    return body.strip()[:200] or "no response body"


def _retry_after(headers: Mapping[str, str]) -> float | None:
    lowered = {k.lower(): v for k, v in headers.items()}
    for key in ("retry-after", "x-ratelimit-reset", "x-rate-limit-reset"):
        raw = lowered.get(key)
        if raw and raw.strip().isdigit():
            value = float(raw)
            # `x-ratelimit-reset` is an epoch timestamp; `retry-after` is a
            # duration. Anything implausibly large is treated as the former.
            return max(0.0, value - datetime.now(UTC).timestamp()) if value > 1e6 else value
    return None


# ----------------------------------------------------------------- normalising
def normalise_node(
    node: Mapping[str, Any], tweet_id: str, *, response_root: str = ""
) -> TweetData:
    identifier = _first_str(node, _ID_KEYS) or tweet_id

    text = _first_str(node, _TEXT_KEYS)
    if text is None:
        note = node.get("note_tweet")
        if isinstance(note, Mapping):
            text = _first_str(note, ("text",))
        elif isinstance(note, str):
            text = note
    if text is None:
        raise ProviderResponseError(f"provider returned no text for tweet {tweet_id}")

    author_node = _first_mapping(node, _USER_KEYS)
    media = _media_items(node)

    return TweetData(
        tweet_id=str(identifier),
        text=text,
        author_username=_first_str(author_node, _USERNAME_KEYS),
        author_name=_first_str(author_node, _NAME_KEYS),
        author_id=_first_str(author_node, _ID_KEYS) if author_node else None,
        author_avatar_url=_first_str(author_node, _AVATAR_KEYS),
        lang=_first_str(node, _LANG_KEYS),
        posted_at=_parse_timestamp(_first_value(node, _TIME_KEYS)),
        media=media,
        url_map=_url_map(node),
        metrics=_metrics(node),
        # The whole node is kept, minus nothing: unlike the X provider there is no
        # oversized `note_tweet` object to strip, and retaining the raw shape is
        # what makes a mapping mistake debuggable from the database alone.
        raw=dict(node) | {"_response_root": response_root},
    )


def _media_items(node: Mapping[str, Any]) -> tuple[MediaData, ...]:
    """Collect media whatever shape the provider used.

    Handles both `media: [...]` and `attachments: {media: [...]}`, and both object
    entries and bare URL strings, because providers differ on all four.
    """
    entries: list[Any] = []
    for key in _MEDIA_CONTAINER_KEYS:
        container = node.get(key)
        if container is None:
            continue
        if isinstance(container, list):
            entries.extend(container)
            continue
        if isinstance(container, Mapping):
            for list_key in _MEDIA_LIST_KEYS:
                inner = container.get(list_key)
                if isinstance(inner, list):
                    entries.extend(inner)
                    break
        if entries:
            break

    items: list[MediaData] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            # A bare URL has no type information, so it is assumed to be a photo.
            if entry.startswith(("http://", "https://")):
                items.append(
                    MediaData(
                        position=index,
                        kind=MediaKind.PHOTO,
                        remote_url=entry,
                        content_type="image/jpeg",
                    )
                )
            continue
        if not isinstance(entry, Mapping):
            continue

        raw_type = _first_str(entry, _TYPE_KEYS)
        # An unrecognised type defaults to photo: a bare entry with a `url` is far
        # more often a picture than anything else, and publishing it as a photo is
        # recoverable whereas dropping it is not.
        kind = (_MEDIA_KIND.get(raw_type.lower()) if raw_type else None) or MediaKind.PHOTO

        if kind is MediaKind.PHOTO:
            remote_url = _first_str(entry, _PHOTO_URL_KEYS)
        else:
            remote_url = _best_mp4(entry) or _first_str(
                entry, ("preview_image_url", "thumbnail", *_PHOTO_URL_KEYS)
            )

        if not remote_url:
            log.debug("media entry had no usable URL", extra={"position": index})
            continue

        width, height = _dimensions(entry)
        items.append(
            MediaData(
                position=index,
                kind=kind,
                remote_url=remote_url,
                content_type="image/jpeg" if kind is MediaKind.PHOTO else "video/mp4",
                width=width,
                height=height,
                duration_ms=_first_int(entry, ("duration_ms", "duration", "duration_millis")),
                alt_text=_first_str(entry, _ALT_KEYS),
            )
        )
    return tuple(items)


def _best_mp4(entry: Mapping[str, Any]) -> str | None:
    video_info = entry.get("video_info") or entry.get("video")
    if not isinstance(video_info, Mapping):
        return None
    variants = video_info.get("variants")
    if not isinstance(variants, list):
        return None
    best_url: str | None = None
    best_bitrate = -1
    for variant in variants:
        if not isinstance(variant, Mapping):
            continue
        url = variant.get("url")
        if not isinstance(url, str) or not url:
            continue
        if variant.get("content_type") not in (None, "video/mp4"):
            continue
        bitrate = _first_int(variant, ("bitrate",)) or 0
        if bitrate > best_bitrate:
            best_bitrate = bitrate
            best_url = url
    return best_url


def _dimensions(entry: Mapping[str, Any]) -> tuple[int | None, int | None]:
    width = _first_int(entry, ("width", "w"))
    height = _first_int(entry, ("height", "h"))
    if width and height:
        return width, height

    sizes = entry.get("sizes")
    if isinstance(sizes, Mapping):
        for size_name in ("large", "medium", "small", "thumb"):
            size = sizes.get(size_name)
            if isinstance(size, Mapping):
                return _first_int(size, ("w", "width")), _first_int(size, ("h", "height"))
    return width, height


def _url_map(node: Mapping[str, Any]) -> dict[str, str]:
    """t.co-style short link → destination, from whichever entity shape is used."""
    result: dict[str, str] = {}
    entities = node.get("entities")
    if not isinstance(entities, Mapping):
        return result

    candidates: list[Any] = []
    urls_holder = entities.get("url")
    if isinstance(urls_holder, Mapping):
        candidates.extend(_as_list(urls_holder.get("urls")))
    candidates.extend(_as_list(entities.get("urls")))

    for entry in candidates:
        if not isinstance(entry, Mapping):
            continue
        short = entry.get("url")
        expanded = entry.get("expanded_url") or entry.get("unwound_url") or entry.get("display_url")
        if isinstance(short, str) and isinstance(expanded, str):
            result[short] = expanded
    return result


def _metrics(node: Mapping[str, Any]) -> dict[str, int]:
    for key in ("public_metrics", "metrics", "stats"):
        value = node.get(key)
        if isinstance(value, Mapping):
            return {
                str(k): v
                for k, v in value.items()
                if isinstance(v, int) and not isinstance(v, bool)
            }
    return {}


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _from_epoch(float(value))
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    if text.isdigit():
        return _from_epoch(float(text))

    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        pass
    else:
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    # Twitter's classic format, still emitted by several third-party APIs.
    try:
        parsed = datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        log.debug("unparseable timestamp from provider", extra={"value": text[:40]})
        return None
    return parsed


def _from_epoch(seconds: float) -> datetime | None:
    # Values this large are milliseconds, which several APIs use.
    if seconds > 1e11:
        seconds = seconds / 1000.0
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


# --------------------------------------------------------------------- helpers
def _dig(payload: Any, path: str) -> Any:
    """Follow a dotted path. An empty path returns the payload unchanged."""
    if not path:
        return payload
    node = payload
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first_value(mapping: Mapping[str, Any] | None, keys: tuple[str, ...]) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _first_str(mapping: Mapping[str, Any] | None, keys: tuple[str, ...]) -> str | None:
    value = _first_value(mapping, keys)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return value if isinstance(value, str) and value.strip() else None


def _first_int(mapping: Mapping[str, Any] | None, keys: tuple[str, ...]) -> int | None:
    value = _first_value(mapping, keys)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _first_mapping(
    mapping: Mapping[str, Any] | None, keys: tuple[str, ...]
) -> Mapping[str, Any] | None:
    value = _first_value(mapping, keys)
    return value if isinstance(value, Mapping) else None
