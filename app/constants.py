"""Hard limits imposed by the Telegram Bot API and other protocol constants.

These are *not* configurable: they describe the remote API, not our preferences.
Sources: https://core.telegram.org/bots/api  and  https://core.telegram.org/bots/faq
"""

from __future__ import annotations

# --- Message / caption sizes (counted in UTF-16 code units, approximated as chars) ---
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024

# --- Media groups ---
MIN_MEDIA_GROUP_ITEMS = 2
MAX_MEDIA_GROUP_ITEMS = 10

# --- Upload ceilings for bots using the public Bot API server ---
MAX_PHOTO_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_FILE_UPLOAD_BYTES = 50 * 1024 * 1024

# --- Flood control. Telegram documents ~20 messages/minute to the same chat for
#     bulk notifications, and ~30 messages/second overall. ---
CHANNEL_MESSAGES_PER_MINUTE = 20

# --- Telegram only accepts these inline tags in parse_mode=HTML ---
ALLOWED_HTML_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "span",
        "tg-spoiler",
        "a",
        "code",
        "pre",
        "blockquote",
        "tg-emoji",
    }
)

# --- Twitter/X ---
TWEET_ID_MAX_DIGITS = 25
X_HOSTS = frozenset(
    {
        "twitter.com",
        "x.com",
        "www.twitter.com",
        "www.x.com",
        "mobile.twitter.com",
        "mobile.x.com",
        "m.twitter.com",
        "m.x.com",
        "nitter.net",  # recognised so we can give a helpful error, never fetched
        "vxtwitter.com",
        "fxtwitter.com",
        "twittpr.com",
    }
)
#: Hosts we accept but which are third-party front-ends; normalised to x.com.
X_MIRROR_HOSTS = frozenset({"vxtwitter.com", "fxtwitter.com", "twittpr.com", "nitter.net"})

CANONICAL_TWEET_URL = "https://x.com/{username}/status/{tweet_id}"
CANONICAL_TWEET_URL_NO_USER = "https://x.com/i/status/{tweet_id}"
CANONICAL_PROFILE_URL = "https://x.com/{username}"

# --- Media ---
SUPPORTED_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
SUPPORTED_ANIMATION_MIMES = frozenset({"image/gif"})
SUPPORTED_VIDEO_MIMES = frozenset({"video/mp4", "video/webm", "video/quicktime"})

MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}

#: Read this many bytes before deciding whether a download is acceptable.
MIME_SNIFF_BYTES = 32
