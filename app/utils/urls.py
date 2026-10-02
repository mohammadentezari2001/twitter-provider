"""Twitter/X URL recognition, validation and normalisation.

Everything that decides "is this a tweet link, and which tweet?" lives here so
the rules are testable in isolation and identical for every entry point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.constants import (
    CANONICAL_PROFILE_URL,
    CANONICAL_TWEET_URL,
    CANONICAL_TWEET_URL_NO_USER,
    TWEET_ID_MAX_DIGITS,
    X_HOSTS,
    X_MIRROR_HOSTS,
)

#: /<user>/status/<id>, /<user>/statuses/<id>, /i/web/status/<id>, /i/status/<id>
_PATH_RE = re.compile(
    r"""^/
    (?:
        (?P<username>[A-Za-z0-9_]{1,15})/status(?:es)?/(?P<id1>\d+)
      | i/(?:web/)?status/(?P<id2>\d+)
    )
    (?P<trailer>/(?:photo|video|analytics|retweets|likes|quotes)(?:/\d+)?)?
    /?$""",
    re.VERBOSE,
)

_URL_IN_TEXT_RE = re.compile(
    r"(?:https?://)?(?:[A-Za-z0-9-]+\.)*(?:twitter\.com|x\.com|vxtwitter\.com|fxtwitter\.com|twittpr\.com|t\.co)"
    r"/[^\s<>\"']*",
    re.IGNORECASE,
)

#: Usernames that are reserved routes, never real authors.
_RESERVED_USERNAMES = frozenset({"i", "home", "search", "explore", "notifications", "messages"})


class InvalidTweetUrlError(ValueError):
    """The input is not a usable Twitter/X status URL.

    `reason` is a machine-readable discriminator so callers can pick a
    user-facing message without string matching.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ParsedTweetUrl:
    tweet_id: str
    username: str | None
    canonical_url: str
    input_url: str
    host: str

    @property
    def author_profile_url(self) -> str | None:
        if not self.username:
            return None
        return CANONICAL_PROFILE_URL.format(username=self.username)


def _normalise_input(raw: str) -> str:
    candidate = raw.strip().strip("<>").rstrip(".,;)!?‏‎")
    if not candidate:
        raise InvalidTweetUrlError("empty")
    if "://" not in candidate:
        # Accept "x.com/user/status/1" and "www.x.com/..." without a scheme.
        if candidate.startswith("//"):
            candidate = f"https:{candidate}"
        else:
            candidate = f"https://{candidate}"
    return candidate


def parse_tweet_url(raw: str) -> ParsedTweetUrl:
    """Parse and normalise a Twitter/X status URL.

    Raises:
        InvalidTweetUrlError: with a `reason` of `empty`, `not_a_url`,
            `bad_scheme`, `shortened`, `unsupported_host`, `not_a_status`
            or `bad_tweet_id`.
    """
    candidate = _normalise_input(raw)

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise InvalidTweetUrlError("not_a_url", str(exc)) from None

    if parts.scheme not in ("http", "https"):
        raise InvalidTweetUrlError("bad_scheme", f"scheme {parts.scheme!r} is not supported")

    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidTweetUrlError("not_a_url", "missing hostname")

    if host in ("t.co", "www.t.co"):
        raise InvalidTweetUrlError(
            "shortened",
            "t.co links hide the tweet ID; open the link and send the full x.com URL",
        )

    if host not in X_HOSTS:
        raise InvalidTweetUrlError("unsupported_host", f"{host} is not a Twitter/X host")

    match = _PATH_RE.match(parts.path)
    if not match:
        raise InvalidTweetUrlError(
            "not_a_status", "the URL does not point at a single post/status"
        )

    tweet_id = match.group("id1") or match.group("id2")
    if not tweet_id or len(tweet_id) > TWEET_ID_MAX_DIGITS or tweet_id.lstrip("0") == "":
        raise InvalidTweetUrlError("bad_tweet_id", "the post ID is out of range")

    username = match.group("username")
    if username and username.lower() in _RESERVED_USERNAMES:
        username = None

    canonical = (
        CANONICAL_TWEET_URL.format(username=username, tweet_id=tweet_id)
        if username
        else CANONICAL_TWEET_URL_NO_USER.format(tweet_id=tweet_id)
    )

    return ParsedTweetUrl(
        tweet_id=tweet_id,
        username=username,
        canonical_url=canonical,
        input_url=raw.strip(),
        host="x.com" if host in X_MIRROR_HOSTS else host,
    )


def try_parse_tweet_url(raw: str) -> ParsedTweetUrl | None:
    try:
        return parse_tweet_url(raw)
    except InvalidTweetUrlError:
        return None


def find_tweet_urls(text: str, *, limit: int = 20) -> list[ParsedTweetUrl]:
    """Extract every distinct tweet URL from a free-form message.

    Order is preserved and duplicates (by tweet ID) are collapsed.
    """
    found: list[ParsedTweetUrl] = []
    seen: set[str] = set()
    for match in _URL_IN_TEXT_RE.finditer(text or ""):
        parsed = try_parse_tweet_url(match.group(0))
        if parsed and parsed.tweet_id not in seen:
            seen.add(parsed.tweet_id)
            found.append(parsed)
            if len(found) >= limit:
                break
    return found


def looks_like_tweet_link(text: str) -> bool:
    """True if the text mentions an X host at all (even if unusable).

    Used to distinguish "you sent a broken tweet link" from "you sent prose".
    """
    return bool(_URL_IN_TEXT_RE.search(text or ""))


def canonical_tweet_url(tweet_id: str, username: str | None) -> str:
    return (
        CANONICAL_TWEET_URL.format(username=username, tweet_id=tweet_id)
        if username
        else CANONICAL_TWEET_URL_NO_USER.format(tweet_id=tweet_id)
    )
