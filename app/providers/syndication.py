r"""Tweet retrieval from X's own public embed endpoint. No API key, no third party.

This is the default provider, and it is the one the bot is meant to run on.

`cdn.syndication.twimg.com/tweet-result` is the endpoint X's own embedded-tweet
widget calls from `platform.twitter.com`. It answers with a compact JSON object
holding exactly what this bot needs — the text, the author's display name and
handle, the avatar, the timestamp, and the photo URLs:

    {
      "id_str": "1519480761749016577",
      "text": "...",
      "created_at": "2022-04-28T...",
      "lang": "en",
      "user": {"name": "...", "screen_name": "...",
               "profile_image_url_https": "https://pbs.twimg.com/..."},
      "photos": [{"url": "https://pbs.twimg.com/media/...jpg",
                  "width": 1200, "height": 800}],
      "entities": {"urls": [...]}
    }

A tweet carrying a video instead of photos has no `photos` entries and a
`video` object, whose renditions are listed under `variants` with the URL in
`src` (the public API calls the same field `url`) — see `_video_item`. The text
of any media tweet ends with one `t.co` short link per attachment; those are
X's, not the author's, and `app/services/render.py` drops them.

Two consequences worth being clear about, because they are the reason this file
exists rather than a five-line config for `generic_http`:

* **No third party is in the middle.** A mirror API such as `vxtwitter` or
  `fxtwitter` would also work and needs no token, but every URL you relay would
  pass through someone else's server, and their continued existence would be a
  dependency of yours. This endpoint is X's own CDN.
* **The response shape is not identical to the public API.** It is close enough
  that the tolerant normaliser written for `generic_http` consumes it unchanged;
  that is what `normalise_node` is imported for.

## The token, and why it is the fragile part

The endpoint expects a `token` query parameter derived from the tweet ID. The
formula below is the one X's embed script uses:

    ((Number(id) / 1e15) * Math.PI).toString(36).replace(/(0+|\.)/g, "")

`_token` reproduces it. Two parts of that expression are easy to get subtly
wrong, and both are handled explicitly rather than approximated:

* **`Number(id)` rounds to a double before dividing.** A modern 19-digit tweet ID
  does not fit in a double (2**53 is 16 digits), so the rounding is lossy, and a
  token computed from the *exact* integer is a different string for every tweet
  posted since 2010. `_token` converts with `float()`, which rounds the same way.
* **`toString(36)` emits the shortest base-36 string that still parses back to
  the same double** — not a fixed number of digits. `_js_to_string_36` emits
  digits until the result round-trips, which *is* that definition rather than an
  approximation of it, so it agrees with V8 by construction.

**The algorithm has not been checked against a JavaScript engine, and no request
to this endpoint has ever been made from the machine this was written on.** So
the provider is built to survive the token being wrong:

1. The request is made with the computed token (or `SYNDICATION_TOKEN`, if set).
2. If that yields nothing — a 400/403/404, or a 200 with an empty body — it is
   tried **once more with no token at all**, because some deployments do not
   check the parameter.
3. A 200 carrying an empty object is treated as *no data*. It is never turned
   into an empty tweet, so an empty post can never reach the channel.
4. Only then does the fetch raise, and the error names the endpoint, the status,
   and the exact command that settles it.

To see what this provider actually sends and gets back, run it directly — this is
the command to reach for first when retrieval fails:

    docker compose exec bot python -m app.providers.syndication 1519480761749016577

If the token is what the endpoint dislikes, copy a working one out of the embed
page's network tab into `SYNDICATION_TOKEN` and it is used verbatim.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
from dataclasses import replace
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
    TweetUnavailableError,
    TweetProvider,
)
from app.providers.generic_http import normalise_node
from app.utils.net import UrlPolicy, create_session

log = get_logger(__name__)

SYNDICATION_HOST = "cdn.syndication.twimg.com"
SYNDICATION_URL = f"https://{SYNDICATION_HOST}/tweet-result"

_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"

#: Cloudflare fronts this endpoint and rejects requests that look like a bare
#: script. These are the headers the embed widget itself sends.
_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://platform.twitter.com/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

#: The most base-36 fraction digits any double can need, derived rather than
#: chosen. The smallest subnormal is 2**-1074, and half of it is the finest
#: distinction a double can carry; 36**-209 is smaller than that, so 209 digits
#: render any finite double exactly. Real values here (thousands, so `id / 1e15
#: * pi` for an 18- or 19-digit ID) stop after ten or so. The bound exists so a
#: broken round-trip check cannot spin forever — it is never what sets precision.
_MAX_FRACTION_DIGITS = 209


def _token(tweet_id: str) -> str:
    """Reproduce `((Number(id) / 1e15) * PI).toString(36)`, zeros and dots removed.

    See `_js_number` for why the ID is converted with `float()` rather than
    `int()`; that distinction is the whole reason this function is not one line.
    """
    numeric = _js_number(tweet_id)
    if not math.isfinite(numeric) or numeric <= 0:
        return ""

    return _js_to_string_36((numeric / 1e15) * math.pi).replace("0", "").replace(".", "")


def _js_number(tweet_id: str) -> float:
    """`Number(tweet_id)` — JavaScript's string-to-double conversion.

    Named separately because the rounding here is load-bearing and easy to
    mistake for pedantry: a 19-digit tweet ID is larger than 2**53 and does not
    fit in a double, so this loses its low digits. JavaScript divides *that*
    rounded value. Dividing the exact integer instead gives a token that is
    wrong for every tweet posted since 2010 while still being right for the
    short IDs a person would test with.

    NaN for anything `Number()` would not make a number, which `_token` then
    rejects.
    """
    try:
        return float(tweet_id)
    except (TypeError, ValueError):
        return math.nan


def _js_to_string_36(value: float) -> str:
    """`Number.prototype.toString(36)` for a positive finite double.

    Emits fraction digits until the string parses back to exactly `value`, which
    makes it the shortest round-tripping expansion. That is the property V8's
    `DoubleToRadixCString` exists to produce, so reproducing the definition gets
    the same answer as reproducing its C loop — without depending on a
    recollection of that loop being right.
    """
    whole = int(math.floor(value))
    fraction = value - whole
    if fraction <= 0:
        return _to_base36(whole)

    digits: list[str] = []
    for _ in range(_MAX_FRACTION_DIGITS):
        fraction *= 36
        digit = min(int(fraction), 35)
        fraction -= digit
        digits.append(_BASE36[digit])
        if _round_trip_36(whole, digits) == value:
            break

    return f"{_to_base36(whole)}.{''.join(digits)}"


def _to_base36(value: int) -> str:
    """The integer part, exactly. No double ever enters this."""
    if value == 0:
        return "0"
    digits = ""
    while value:
        value, remainder = divmod(value, 36)
        digits = _BASE36[remainder] + digits
    return digits


def _round_trip_36(whole: int, digits: list[str]) -> float:
    """Parse `<whole>.<digits>` back to a double in a single rounded division."""
    if not digits:
        return float(whole)
    text = "".join(digits)
    scale = 36 ** len(text)
    return (whole * scale + int(text, 36)) / scale


# ------------------------------------------------------------------- video
#: This endpoint names a rendition's URL `src`; the public API names it `url`.
_VIDEO_URL_KEYS = ("src", "url")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _variant_url(variant: Mapping[str, Any]) -> str | None:
    for key in _VIDEO_URL_KEYS:
        value = variant.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _best_video_url(variants: Any) -> str | None:
    """The highest-bitrate MP4 rendition, or None when only HLS is offered.

    Telegram is handed a file to upload, so an `.m3u8` playlist is useless here
    even though it is often the entry with the largest number next to it. The
    playable renditions are the `video/mp4` ones, and the highest bitrate among
    them is the closest to what the author posted.
    """
    best_url: str | None = None
    best_bitrate = -1
    for variant in variants if isinstance(variants, list) else ():
        if not isinstance(variant, Mapping):
            continue
        if variant.get("content_type") not in (None, "video/mp4"):
            continue
        url = _variant_url(variant)
        if url is None or urlsplit(url).path.lower().endswith(".m3u8"):
            continue
        bitrate = _as_int(variant.get("bitrate")) or 0
        if bitrate > best_bitrate:
            best_bitrate = bitrate
            best_url = url
    return best_url


def _video_item(payload: Mapping[str, Any]) -> MediaData | None:
    """The tweet's video, when the payload carries one.

    An empty `remote_url` is returned deliberately when the payload names a
    video but offers no playable MP4 — the media is unavailable, or only an HLS
    manifest exists. The renderer treats an entry with no URL as *omitted*, so
    an unavailable video is reported to the admin rather than silently replaced
    by its poster frame.
    """
    video = payload.get("video")
    if not isinstance(video, Mapping):
        return None

    availability = video.get("mediaAvailability")
    status = availability.get("status") if isinstance(availability, Mapping) else None
    variants = video.get("variants")

    # A `video` object carrying neither renditions nor an availability verdict
    # says nothing about the media, and replacing the photos with it would drop
    # a real attachment on the strength of an empty object.
    if status is None and not isinstance(variants, list):
        return None

    if isinstance(status, str) and status and status.lower() != "available":
        log.warning("syndication video is not available", extra={"status": status})
        return MediaData(
            position=0, kind=MediaKind.VIDEO, remote_url="", content_type="video/mp4"
        )

    url = _best_video_url(variants)
    if not url:
        log.warning(
            "syndication video had no playable MP4 variant; it will be omitted",
            extra={"variant_count": len(variants or ())},
        )
    return MediaData(
        position=0,
        kind=MediaKind.VIDEO,
        remote_url=url or "",
        content_type="video/mp4",
        duration_ms=_as_int(video.get("durationMs") or video.get("duration_ms")),
    )


class SyndicationProvider(TweetProvider):
    """Retrieves a tweet from X's embed endpoint. No credentials required."""

    name = "syndication"

    def __init__(
        self,
        *,
        token: str = "",
        timeout: float = 15.0,
        base_url: str = SYNDICATION_URL,
    ) -> None:
        self._fixed_token = token.strip()
        self._base_url = base_url.rstrip("/") or SYNDICATION_URL

        host = urlsplit(self._base_url).hostname
        if not host:
            raise ProviderResponseError(f"SYNDICATION_URL {base_url!r} has no hostname")

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
        token = self._fixed_token or _token(tweet_id)

        payload, status = await self._request(tweet_id, token)
        tweet = self._read(payload, tweet_id)
        if tweet is not None:
            return tweet

        # A 400/403/404 — or, as this endpoint more often answers, a 200 whose
        # body is `{}` — is the signature of a parameter it did not like.
        # Retrying without one costs a single request and rescues the case where
        # the token is simply not checked.
        if token:
            log.info(
                "syndication returned no usable data; retrying without a token",
                extra={"tweet_id": tweet_id, "status": status},
            )
            payload, status = await self._request(tweet_id, "")
            tweet = self._read(payload, tweet_id)
            if tweet is not None:
                return tweet

        raise self._failure(tweet_id, status, bool(token), empty=payload is not None)

    def _read(self, payload: Mapping[str, Any] | None, tweet_id: str) -> TweetData | None:
        """Turn a response body into a tweet, or None when there is nothing in it.

        Raises `TweetUnavailableError` for a tombstone, which is final: it means
        the tweet is gone, so trying a different token would only be a request
        that cannot succeed.

        An empty object is emphatically **not** an empty tweet. This endpoint
        answers `{}` — two bytes — both for a tweet it will not serve and for a
        request it did not like, and the one outcome that must never happen is a
        post with no text in it reaching a real channel.
        """
        if not payload:
            return None
        if payload.get("__typename") == "TweetTombstone":
            # How the endpoint reports a deleted or protected tweet.
            raise TweetUnavailableError(
                "this tweet is no longer publicly visible (deleted, protected, "
                "or from a suspended account)"
            )

        tweet = normalise_node(payload, tweet_id, response_root="")

        video = _video_item(payload)
        if video is None:
            return tweet
        # A tweet carries either photos or a single video, never both, so the
        # `video` object is authoritative when present. Anything under `photos`
        # is the poster frame of that video, not a picture to publish on its own.
        return replace(tweet, media=(video,))

    async def _request(
        self, tweet_id: str, token: str
    ) -> tuple[Mapping[str, Any] | None, int]:
        """One attempt.

        Returns `(payload, status)`. The payload is None when the endpoint gave
        no usable body — a 400/403/404 — and `{}` when it answered 200 with
        nothing in it. Both are the caller's cue to try a different token, and
        both are distinguished from a tweet. The status is carried back so the
        final error can say what actually happened rather than guessing.
        """
        params: dict[str, str] = {"id": tweet_id, "lang": "en"}
        if token:
            params["token"] = token

        try:
            response = await self._session.get(
                self._base_url, params=params, headers=_BROWSER_HEADERS
            )
            async with response:
                body = await response.text()
                status = response.status
                if status != 200:
                    # 401 is deliberately absent: it means the endpoint is
                    # refusing this client outright, which no token will fix.
                    if status in (400, 403, 404):
                        log.debug(
                            "syndication attempt refused",
                            extra={
                                "tweet_id": tweet_id,
                                "status": status,
                                "had_token": bool(token),
                            },
                        )
                        return None, status
                    raise _classify(status, body)
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderTransientError(
                "cdn.syndication.twimg.com timed out. If this VPS sits behind a "
                "restrictive network, that is the cause — not the token."
            ) from exc
        except aiohttp.ClientError as exc:
            raise ProviderTransientError(
                f"could not reach cdn.syndication.twimg.com: {exc!r}"
            ) from exc

        if not body.strip():
            # A 200 with a zero-byte body is "nothing to say", not a shape change.
            return {}, 200

        try:
            payload = json.loads(body)
        except ValueError as exc:
            # This endpoint answers with an HTML challenge page when Cloudflare
            # is unhappy, which is indistinguishable from a shape change until
            # you look at the body — so the first 120 characters are surfaced.
            raise ProviderResponseError(
                f"cdn.syndication.twimg.com returned {len(body)} bytes that are not "
                f"JSON. First 120 characters: {body[:120]!r}"
            ) from exc

        if not isinstance(payload, Mapping):
            raise ProviderResponseError("cdn.syndication.twimg.com returned a non-object")

        return payload, 200

    def _failure(
        self, tweet_id: str, status: int, had_token: bool, *, empty: bool
    ) -> ProviderError:
        """Choose the error that describes what actually happened.

        The distinction matters to whoever reads it: "this tweet is gone" is not
        actionable, "the retrieval method stopped working" is, and they arrive
        with the same status code depending on which half broke.
        """
        if status == 404:
            return TweetNotFoundError(
                f"no public tweet with id {tweet_id} — it may have been deleted, "
                "or the account may be protected or suspended"
            )
        if empty:
            error = ProviderResponseError(
                f"cdn.syndication.twimg.com returned no data for tweet {tweet_id} "
                "(HTTP 200, empty result). Either the tweet is not public, or the "
                "retrieval token was refused — this endpoint answers the same way "
                "for both. Tell them apart with: "
                f"docker compose exec bot python -m app.providers.syndication {tweet_id}"
            )
            # `ProviderResponseError` is retryable by default, on the grounds that
            # a truncated body and a real schema change look alike. Here they do
            # not: the retry sends a byte-identical request (the token is a pure
            # function of the ID), and the answer is decided by the tweet's
            # visibility or by the token, neither of which changes in thirty
            # seconds. Retrying would fire ten more requests and report the same
            # thing, so the operator waits for backoff to learn nothing.
            error.retryable = False
            return error
        return ProviderResponseError(self._failure_hint(tweet_id, status, had_token))

    def _failure_hint(self, tweet_id: str, status: int, had_token: bool) -> str:
        """Say what happened, then the one command that settles why.

        Deliberately verbose: this is the error an operator sees when the whole
        retrieval method has stopped working, and the difference between "the
        token formula drifted", "X changed the endpoint" and "this VPS cannot
        reach X" is not guessable from the status code alone.

        The command it names runs *this provider*, not a bare `curl` — the two
        are not the same request (token, `lang`, browser User-Agent and Referer),
        and a bare curl answering 200 with no body is the expected result rather
        than evidence of anything.
        """
        attempts = "with a token and without one" if had_token else "without a token"
        return (
            f"could not retrieve tweet {tweet_id} from cdn.syndication.twimg.com "
            f"(last status {status}, tried {attempts}). Diagnose with: "
            f"docker compose exec bot python -m app.providers.syndication {tweet_id} "
            "— it prints the token it computes and the exact body that came back. "
            "If that returns a tweet, retrieval works and the failure is elsewhere. "
            "If it returns an HTML challenge, this server's IP is being blocked. If "
            "it does not connect at all, the VPS cannot reach X. If it returns an "
            "empty body for a tweet you can see in a browser, the token formula has "
            "drifted: copy a working token from the embed page's network tab into "
            "SYNDICATION_TOKEN and it is used verbatim."
        )


# --------------------------------------------------------------- error mapping
def _classify(status: int, body: str) -> ProviderError:
    """Map a refusal that no retry can fix to a non-retryable error.

    400/403/404 never reach here — `_request` treats them as "try a different
    token", and `fetch` decides what they mean once both attempts are done. What
    lands here is the endpoint saying something that will not change.
    """
    detail = body.strip()[:200] or "no response body"

    if status == 401:
        # Not a credential problem — this provider has no credentials. A 401 here
        # means the endpoint refuses this client outright (a blocked IP, or a
        # missing browser header), which another attempt will not fix.
        return ProviderAuthError(
            "cdn.syndication.twimg.com refused this client outright (401). This is "
            f"not a credential problem: {detail}",
            status=status,
        )
    if status == 410:
        return TweetUnavailableError(
            f"the tweet has been deleted: {detail}", status=status
        )
    if status == 429:
        return ProviderRateLimitedError(
            "cdn.syndication.twimg.com is rate limiting this server; slow down",
            status=status,
        )
    if status >= 500:
        return ProviderTransientError(
            f"cdn.syndication.twimg.com returned {status}: {detail}", status=status
        )
    return ProviderResponseError(
        f"cdn.syndication.twimg.com returned {status}: {detail}", status=status
    )


# ------------------------------------------------------------------ self-test
async def _selftest(tweet_id: str) -> int:
    """Fetch one tweet through this provider and print what came back.

    Run as `python -m app.providers.syndication <tweet_id>`. It exists because
    "retrieval is broken" has three very different causes — the token, the
    network, the tweet — and only this provider's own request shape can tell
    them apart. A bare `curl` with `?id=` alone is a different request and is
    expected to answer with an empty body, so it settles nothing.

    It prints no configuration and no secret.
    """
    print(f"id    : {tweet_id}")
    print(f"token : {_token(tweet_id) or '(none — not a positive numeric id)'}")
    print(f"url   : {SYNDICATION_URL}?id={tweet_id}&lang=en&token=…")

    provider = SyndicationProvider()
    try:
        tweet = await provider.fetch(tweet_id)
    except ProviderError as exc:
        print(f"FAILED: {type(exc).__name__}")
        print(f"        {exc}")
        return 1
    finally:
        await provider.aclose()

    print(f"author: {tweet.author_name} (@{tweet.author_username})")
    print(f"photos: {len(tweet.photos)}")
    print(f"videos: {len(tweet.videos)}")
    print(f"text  : {tweet.text[:300]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m app.providers.syndication <tweet_id>`."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m app.providers.syndication <tweet_id>")
        return 2
    return asyncio.run(_selftest(args[0]))


if __name__ == "__main__":
    raise SystemExit(main())
