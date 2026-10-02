"""Every string the bot says to a human, in one place.

`LOCALE` exists in configuration and `app/i18n` is referenced in `.env.example`,
but this build ships one language. Centralising the strings here means adding a
second one later is a lookup change rather than a hunt through handlers for
inline literals — which is the expensive part of localisation, not the
translation itself.

Strings are HTML, because that is the parse mode the bot uses everywhere.
"""

from __future__ import annotations

from app.config import Settings

# ------------------------------------------------------------------- commands
START = (
    "👋 <b>tweet-relay-bot</b>\n\n"
    "Send me a link to an X/Twitter post and I will fetch it and publish it to the "
    "channel automatically — no confirmation needed.\n\n"
    "You can paste several links at once; each is published on its own.\n\n"
    "<b>Commands</b>\n"
    "/status — queue depth, worker liveness, provider\n"
    "/stats — today and the last 7 days\n"
    "/recent — the last few publications\n"
    "/id — your Telegram user ID\n"
    "/help — this message"
)

HELP = START

NOT_AUTHORISED = (
    "⛔ This bot is restricted to configured administrators.\n\n"
    "Send /id to see your user ID, then add it to <code>ADMIN_USER_IDS</code> in "
    "the environment and restart."
)

ADMIN_ONLY_CALLBACK = "Not authorised."

# ----------------------------------------------------------------- fetch flow
FETCHING = "⏳ Fetching <code>{tweet_id}</code>…"
FETCHING_MANY = "⏳ Fetching {count} posts…"

FETCH_FAILED = "❌ Could not fetch <code>{tweet_id}</code>.\n\n{reason}"
FETCH_RATE_LIMITED = (
    "⏳ The provider is rate-limiting us for <code>{tweet_id}</code>. "
    "It will be retried automatically."
)
FETCH_DEFERRED = "⏳ <code>{tweet_id}</code> is queued and will be retried automatically."

ALREADY_PUBLISHED = (
    "🚫 <code>{tweet_id}</code> has already been published to the channel.\n\n"
    "{link}\n\n"
    "If you want to post it again, delete the channel post first — that is what "
    "releases the duplicate guard."
)

ALREADY_QUEUED = "⏳ <code>{tweet_id}</code> is already in progress."

NOT_A_TWEET_LINK = (
    "That does not look like a link to a single X/Twitter post.\n\n"
    "Send something like <code>https://x.com/user/status/1234567890</code>."
)

# Keyed by `InvalidTweetUrlError.reason`, so the wording is chosen by a
# discriminator rather than by matching substrings of an exception message.
INVALID_LINK_REASONS: dict[str, str] = {
    "empty": "The link was empty.",
    "not_a_url": "That is not a valid URL.",
    "bad_scheme": "Only http and https links can be fetched.",
    "shortened": (
        "That is a <code>t.co</code> short link, which hides the post ID. "
        "Open it and send the full <code>x.com</code> URL instead."
    ),
    "unsupported_host": "That host is not a Twitter/X host.",
    "not_a_status": (
        "That URL does not point at a single post. Profile, search and home "
        "links cannot be relayed."
    ),
    "bad_tweet_id": "The post ID in that link is not valid.",
}

# ----------------------------------------------------------------- preview flow
QUEUED = "✅ Queued for publishing. I will tell you when it is done."
CANCELLED = "❌ Cancelled. Nothing was published."
SUBMISSION_GONE = (
    "This preview is no longer active — it either expired or was already handled."
)
SUBMISSION_NOT_YOURS = "This preview belongs to another administrator."

PUBLISH_SUCCEEDED = "✅ Published to the channel.\n{link}"
PUBLISH_PARTIAL = (
    "⚠️ Published, but not everything made it.\n{link}\n\n{detail}"
)
PUBLISH_FAILED = "❌ Publishing failed.\n\n{reason}"
PUBLISH_DEFERRED = "⏳ Publishing was postponed ({reason}). It will be retried."

# --------------------------------------------------------------------- status
STATUS = (
    "<b>Status</b>\n\n"
    "<b>Mode</b> {run_mode} · up {uptime}\n"
    "<b>Provider</b> <code>{provider}</code>\n"
    "<b>Channel</b> {channel}\n"
    "<b>Database</b> {database}\n"
    "<b>Worker</b> {worker}\n"
    "<b>Queue</b> {queue}\n"
    "<b>Oldest queued job</b> {oldest}\n\n"
    "{warnings}"
)

WORKER_ALIVE = "alive ({ago})"
WORKER_UNKNOWN = "no heartbeat seen"
WORKER_STALE = "⚠️ last seen {ago}"

WARNINGS_HEADER = "⚠️ <b>Configuration warnings</b>"

STATS = (
    "<b>Statistics</b> ({tz})\n\n"
    "<b>Today</b>  published {today_published} · fetched {today_fetched} · "
    "failed {today_failed}\n"
    "<b>Last {days} days</b>  published {week_published} · fetched {week_fetched} · "
    "failed {week_failed} · skipped {week_skipped}"
)

RECENT_HEADER = "<b>Recent publications</b>"
RECENT_EMPTY = "Nothing has been published yet."
RECENT_ROW = "{icon} <code>{tweet_id}</code> · {status} · {when}"

MY_ID = (
    "Your Telegram user ID is <code>{user_id}</code>.\n\n"
    "Add it to <code>ADMIN_USER_IDS</code> to grant access."
)

UNEXPECTED_ERROR = (
    "⚠️ Something went wrong handling that. It has been logged; nothing was published."
)

MEDIA_CACHE_NOTICE = (
    "ℹ️ <code>MEDIA_CACHE_ENABLED</code> is on, but this build downloads media per "
    "publish rather than keeping a cross-job cache. The setting is accepted and "
    "reserved for the media-cache phase."
)

PUBLISH_PERMISSION_NOTICE = (
    "❌ The bot cannot post to {channel}: {detail}\n"
    "Add it as an administrator with the 'Post Messages' permission."
)


def describe_invalid_link(reason: str) -> str:
    return INVALID_LINK_REASONS.get(reason, NOT_A_TWEET_LINK)


def startup_warnings(settings: Settings) -> list[str]:
    """Advisories about settings this build accepts but does not fully act on.

    Kept next to the strings they explain so the two cannot drift, and returned
    as a list so `app.main` can log them and `/status` can show them.
    """
    notes: list[str] = []
    if settings.MEDIA_CACHE_ENABLED:
        notes.append(MEDIA_CACHE_NOTICE)
    return notes
