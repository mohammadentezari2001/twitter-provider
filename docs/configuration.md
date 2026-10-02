# Configuration

Every setting lives in `.env` (copy `.env.example`) and is read once at startup by
`app/config.py`. Field names match the environment variable names exactly, so this
document, `.env.example` and the code cannot drift.

`docker compose up` refuses to start when a **blocking** problem is present and
prints exactly which variable is wrong. Exit code `2` means "misconfigured"; exit
code `1` means "crashed". `docker compose ps` shows which.

```bash
cp .env.example .env
chmod 600 .env          # contains a bot token and a database password
$EDITOR .env
```

---

## 1. Telegram — required

| Variable | Notes |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) → `/newbot`. Format `123456789:AA…` |
| `TELEGRAM_CHANNEL_ID` | `-100…` (preferred) or `@username`. A username is resolved once at startup and the **numeric ID is what gets stored** — a channel can be renamed, and its username changed with it. |
| `TELEGRAM_CHANNEL_USERNAME` | Optional. Public `@username` of the channel. Used for the **footer of every published post** and for `https://t.me/<name>/<id>` links. Empty is fine: a `@name` `TELEGRAM_CHANNEL_ID` is used instead, and a fully private channel simply gets no handle in the footer — one is never invented. |
| `TELEGRAM_CHANNEL_NAME` | Optional. Display name shown on the first line of the post footer, e.g. `📢 Dovio Twitter`. Empty means the footer shows the username alone. |
| `ADMIN_USER_IDS` | Comma-separated Telegram user IDs. **The lowest ID is granted the `owner` role at startup.** Get yours from [@userinfobot](https://t.me/userinfobot), or send `/id` to the bot once it is running. |

### Getting the numeric channel ID

Forward any post from the channel to [@userinfobot](https://t.me/userinfobot), or
open the channel on web and read the number after `/c/` in the URL, prefixed with
`-100`.

### The bot must be a channel administrator

Administrator, with **Post Messages** enabled. Without it every publish fails with
`TelegramForbiddenError` and the job is marked non-retryable — retrying a request
Telegram has already refused just produces the same refusal five times, more
slowly. The permission is checked at startup and logged as a warning:

```
❌ The bot cannot post to <channel>: the bot is not an administrator of the channel
```

Removing an ID from `ADMIN_USER_IDS` and restarting **deactivates** that admin
rather than deleting them, because `publications.published_by` and
`jobs.requested_by` reference the row: revoking access must not erase who
published what.

---

## 2. Datastores

| Variable | Notes |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://user:password@host:5432/db`. Compose uses the service name `postgres` as the host. A `postgres://` or `postgresql://` prefix is rewritten automatically. |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Read by the `postgres` container only. Must agree with `DATABASE_URL`. |
| `REDIS_URL` | **Not used by this build.** See § 8. |

The password must be URL-encoded if it contains `@`, `:`, `/` or `%` — in
`DATABASE_URL` *and* in the `postgres` service's own variables.

---

## 3. The tweet provider

`TWEET_PROVIDER` selects how a post is retrieved. The default is `syndication`,
which needs nothing configured — **a fresh install retrieves real tweets with no
account, no key, and no third party in the middle.**

It is never `mock` by default: that would let a misconfigured deployment publish
fixture text into a real channel and look like it was working.

### `syndication` — X's own public embed endpoint (default)

```env
TWEET_PROVIDER=syndication
```

No other setting is required. The provider asks
`https://cdn.syndication.twimg.com/tweet-result?id=<id>&lang=en&token=<derived>`,
which is the endpoint X's own embedded-tweet widget calls from
`platform.twitter.com`. It returns the text, the author's display name and
handle, the avatar, the timestamp, and the photo URLs — everything the post
template needs.

**Why this and not a mirror.** `vxtwitter`, `fxtwitter` and similar services also
work and also need no key, but every URL you relay would pass through someone
else's server, and their continued existence would become a dependency of yours.
This endpoint is X's own CDN.

**What is fragile about it, in one paragraph.** The endpoint expects a `token`
query parameter derived from the tweet ID, computed by a formula in X's embed
script:

```js
((Number(id) / 1e15) * Math.PI).toString(36).replace(/(0+|\.)/g, "")
```

`app/providers/syndication.py` reproduces that formula in Python, including the
two parts of it that are easy to get wrong:

- **`Number(id)` rounds the ID to a double *before* dividing.** A 19-digit tweet
  ID is larger than 2^53 and does not fit exactly, so a token computed from the
  exact integer is a *different string* for every tweet posted since 2010 — and
  still looks right for the short IDs you would test with.
- **`toString(36)` emits the shortest base-36 string that parses back to the same
  double**, not a fixed number of digits.

**Neither the formula nor the endpoint has been verified from the machine this
was written on**, which had no outbound network. The provider is built to survive
the token being wrong: it tries the computed token; if that yields nothing — a
400/403/404, or a 200 with an empty body — it tries **once more with no token at
all**; and only then does it raise.

**A 200 with an empty body is treated as "no data", never as an empty tweet.**
This endpoint answers `{}` — two bytes, `content-length: 2` — both for a tweet it
will not serve *and* for a request whose token it did not like, so the two cannot
be told apart from the response. Nothing is previewed or published from it.

### If retrieval fails

Run the command from the error message **before changing anything else**. It
exercises this provider's real request, which a bare `curl` does not:

```bash
docker compose exec bot python -m app.providers.syndication 1519480761749016577
```

It prints the ID, the token it computed, and then either the tweet or the exact
error. The answer tells you which problem you have:

| What you see | What it means | What to do |
|---|---|---|
| The author, photo count and text | Retrieval works | If the bot still fails, check `docker compose logs worker` — the failure is elsewhere |
| `FAILED: ProviderResponseError` … `returned no data` | The endpoint answered 200 with `{}`: either the tweet is not public, or the token was refused | Try a tweet you can see in a browser. If that fails too, the token has drifted — see below |
| `FAILED: ProviderTransientError` mentioning an HTML challenge | Cloudflare is challenging this server's IP | Run the bot from a different host, or switch to another provider |
| `FAILED: ProviderTransientError` … timed out / could not reach | The VPS cannot reach X | Check outbound HTTPS; `curl -sS -o /dev/null -w '%{http_code}\n' https://api.telegram.org` will also fail |
| `FAILED: TweetNotFoundError` | The endpoint says there is no such public tweet | Protected, deleted, or the ID is wrong |

> A bare `curl '…/tweet-result?id=<id>'` with no token answers **200 with an
> empty body**. That is the expected result for that request, not evidence of a
> fault — it omits the token, the `lang`, the browser `User-Agent` and the
> `Referer` that the real request sends. Use the command above instead.

If the token is what the endpoint dislikes, open the tweet in a browser, read the
`token` query parameter out of the embed request in the network tab, and put it
in `SYNDICATION_TOKEN`; it is used verbatim.

| Setting | Meaning |
|---|---|
| `SYNDICATION_URL` | The endpoint. Changing it is supported; there is rarely a reason to. Must be `https://`. |
| `SYNDICATION_TOKEN` | Optional. Used verbatim instead of the computed token. This is the escape hatch for the day the formula stops matching. |

**On X's terms.** This endpoint is public and serves X's own embeds, and the bot
asks for one specific tweet that you named — it does not crawl, search or
discover. It is nonetheless an internal endpoint that X owes you no stability on,
and redistributing someone's posts is a decision about their content, not about
the endpoint. Both are yours to make; this provider only makes the retrieval
work.

### `x_api` — the official X API

```env
TWEET_PROVIDER=x_api
TWEET_API_KEY=<your App's Bearer Token>
```

**What you need:** an [X developer](https://developer.x.com) account with a
project and an App, and a **Bearer Token** from that App's *Keys and tokens* page.
The endpoint used is `GET https://api.x.com/2/tweets/{id}` with `expansions=author_id,attachments.media_keys`.

**What this costs and what it will not do.** Tweet lookup on the X API requires a
paid tier as of 2025 — the free tier does not include it. The provider is written
against the documented v2 response and is the only one that needs no third party
in the loop, but you must confirm your plan includes `GET /2/tweets/:id` before
relying on it. When the API answers with a 401 or 403 the job fails immediately
and **does not retry** — a credential problem is configuration, and retrying only
burns quota and hides the real error.

A missing `TWEET_API_KEY` with this provider selected is a **blocking startup
error**, not a runtime surprise.

`TWEET_API_KEY` is a `SecretStr`: it is never logged, and `safe_dump()` reports it
as `<set:NN chars>`.

### `generic_http` — a licensed third-party API

```env
TWEET_PROVIDER=generic_http
TWEET_API_BASE_URL=https://api.example-provider.com/v1
TWEET_API_TWEET_PATH=/tweets/{tweet_id}
TWEET_API_KEY=...
TWEET_API_AUTH_HEADER=Authorization
TWEET_API_AUTH_SCHEME=Bearer
TWEET_API_RESPONSE_ROOT=data
```

This is the escape hatch for a provider that is neither X nor a free mirror. One
tweet ID in, one JSON object out. `TWEET_API_RESPONSE_ROOT` is a **dotted path**
to the tweet inside the body (`data`, `tweet`, `result.tweet`, or empty for the
body itself).

Within that object the normaliser accepts the field names real APIs actually use,
taking the first of each group that is present:

| Field | Accepted keys |
|---|---|
| id | `id_str`, `id`, `tweet_id`, `rest_id` |
| text | `note_tweet.text`, `full_text`, `text`, `content`, `description` |
| created_at | `created_at`, `date`, `posted_at`, `timestamp`, `time` — ISO 8601, RFC 2822, Unix seconds, or Twitter's classic format |
| lang | `lang`, `language` |
| author | `user`, `author`, `account`, `user_info` — with `screen_name`/`username`/`handle`/`login`, `name`/`display_name`, `profile_image_url_https`/`avatar` |
| media | `extendedEntities.media`, `extended_entities.media`, `entities.media`, `media`, `photos`, `attachments.media` |
| link expansion | `entities.url.urls[].{url,expanded_url}`, `entities.urls[].urls[]` |

Media entries may be objects (`media_url_https`, `type`, `sizes`, `alt_text`,
`video_info.variants`) or bare URL strings.

> **Images will not publish until you add your provider's CDN host to
> `MEDIA_ALLOWED_HOSTS`.** Media downloads are SSRF-protected: only hosts on the
> allowlist are fetched. The default list covers `pbs.twimg.com` and friends; a
> third-party CDN is refused with a clear `MediaRejectedError` until you add it.

This provider implements no pagination, no search, and no thread reconstruction,
and does not retry internally. **Check the provider's terms first** —
redistributing someone's posts to a public channel is exactly the use most APIs
restrict, and "the endpoint answered" is not the same as "this is permitted".

> Do not point this at a free `vxtwitter`/`fxtwitter`/`nitter`-style mirror. They
> are undocumented, unversioned, rate-limit aggressively, and are not authorized
> for redistribution. Their hostnames are recognised by the URL parser only so
> that a link pasted from one gets a helpful error instead of a confusing one.

### `mock` — fixtures, for development and tests only

```env
TWEET_PROVIDER=mock
```

No network, no credentials. Fixture IDs `1`–`4` and `9` return canned posts;
`5` returns "not found", `6` "unavailable", `7` a rate limit, `8` a transient
error — which is how the retry and failure paths are exercised without a live
provider. Selecting it logs a startup warning. **Never leave this in a deployment
that posts to a real channel.**

---

## 4. Publishing behaviour

**Publishing is automatic.** An authorized admin sending a tweet URL is the
approval: the bot fetches the post and publishes it with no confirmation step, so
there is no `AUTO_PUBLISH` / `REQUIRE_CONFIRMATION` switch. The bot still reports
what it did in the admin's chat, but that message is a receipt, not a prompt.

| Variable | Default | Notes |
|---|---|---|
| `MAX_CONCURRENT_FETCHES` | `4` | |
| `MAX_CONCURRENT_PUBLISHES` | `2` | Separate from fetches so a burst of slow uploads cannot starve the fetches feeding them. |
| `MAX_RETRIES` | `5` | Attempts, counting the first. A job that exhausts them lands in `dead`, not `failed` — `dead` needs a human, `failed` never had a chance. |
| `RETRY_BASE_DELAY` | `2.0` | Backoff is `base * 2**(attempt-1)`, capped at `RETRY_MAX_DELAY`, with equal jitter. |
| `RETRY_MAX_DELAY` | `900.0` | |
| `PUBLISH_COOLDOWN` | `4.0` | Minimum seconds between two channel posts. Telegram's documented guidance is ~20 messages/minute per chat; `4.0` ≈ 15/min leaves headroom for multi-message publishes. Enforced across processes in PostgreSQL, so it holds with `bot` and `worker` in separate containers. Below `3.0` the app warns at startup. |
| `JOB_LOCK_TIMEOUT` | `600` | A job held longer than this is assumed to belong to a dead worker and is requeued. Must exceed the slowest plausible publish (download + upload). |
| `WORKER_POLL_INTERVAL` | `2.0` | How often an idle worker checks the queue. |

**Deferral is not failure.** A publish cooldown or a Telegram `429` puts the job
back with a `run_after` in the future *without consuming an attempt*, so a busy
channel can never exhaust the retry budget of a job that never failed.

---

## 5. Media

| Variable | Default | Notes |
|---|---|---|
| `MEDIA_MAX_SIZE_MB` | `45` | Telegram's bot upload ceiling is 50 MB. The effective ceiling is the lower of this and the Bot API's own limit for the upload: 10 MB for a photo, 50 MB for a video or any other file. |
| `MEDIA_ALLOWED_HOSTS` | `pbs.twimg.com,video.twimg.com,abs.twimg.com,ton.twimg.com` | Suffix match. **Empty means "any public host"** — private, loopback and link-local addresses are refused regardless. |
| `ALLOW_PRIVATE_MEDIA_HOSTS` | `false` | **DANGEROUS.** Disables SSRF protection. For local testing against a LAN media server. Never enable in production; doing so logs a startup warning. |
| `MEDIA_CONNECT_TIMEOUT` / `MEDIA_DOWNLOAD_TIMEOUT` | `10.0` / `120.0` | |
| `MEDIA_FAILURE_MODE` | `text_link` | Applies to photos and video alike. `text_link` and `skip_media` both publish the text alone (the parent tweet's URL is never rendered, so the two are equivalent in this build); `fail` fails the job so it can be retried. **All downloads complete before the first byte is sent**, which is what makes `fail` truthful — nothing has reached the channel when it aborts. |
| `LONG_CAPTION_MODE` | `text_first` | A post with media gets a 1024-character caption budget instead of 4096. `text_first` sends the full text as its own message then the media; `media_first` reverses that; `truncate` keeps one message and shortens the text. |
| `MEDIA_CACHE_ENABLED` | `true` | **Reserved, not implemented.** See § 8. |
| `ALLOW_VIDEO` | `true` | Publish a video attachment as a playable clip (`sendVideo`), downloading the MP4 rendition the provider selected. A tweet carries photos or one video/GIF, never both, so this adds a message rather than changing the photo path. The download shares the file ceiling (§ above): `MEDIA_MAX_SIZE_MB` capped by the Bot API's 50 MB. Set `false` to relay such posts as text; either way the admin receipt reports how many items will be omitted. |

---

## 6. Post template

The renderer measures the fixed blocks (header, byline, channel footer) with
`visible_length()` — Telegram's own rule, tags stripped and entities decoded
— and gives what is left to the tweet text, which is truncated **before** it is
escaped. That ordering is why a cut can never land inside a tag and produce a
message Telegram rejects with "can't parse entities".

A published post is: the author's display name, the tweet text, its photos or its video, and
the channel footer. **The author's `@handle`, the original post's URL and its
publication time are never rendered** — there is no setting to turn them back on.

| Variable | Default | Notes |
|---|---|---|
| `POST_TEMPLATE` | `rich` | `rich`, `minimal` or `custom`. |
| `INCLUDE_SIGNATURE` + `TEMPLATE_SIGNATURE` | `false` / empty | |
| `TEMPLATE_HEADER`, `TEMPLATE_FOOTER` | empty | **Trusted markup** — not escaped, so you may use `<i>`, `<a href>`, `<code>`. Dropped automatically if the post would not otherwise fit. The channel footer is appended after them and is never dropped. |
| `TEMPLATE_AUTHOR_FORMAT` | `{name}` | `{name}` is the only value the shipped template renders; `{username}`, `{handle}` and `{id}` still parse but render empty. A typo falls back to `{name}` with a warning rather than failing the publish. |
| `TEMPLATE_EMOJI_AUTHOR`, `TEMPLATE_EMOJI_CHANNEL` | 🐦 📢 | Set either empty to remove it. `TEMPLATE_EMOJI_CHANNEL` prefixes the channel name in the footer. |
| `TEMPLATE_CUSTOM` | empty | Only for `POST_TEMPLATE=custom`. Telegram HTML with a required `{text}` placeholder. Also `{author}`, `{header}`, `{footer}`, `{signature}`; `{link}` and `{time}` are still accepted but render empty, so an older template degrades gracefully. Braces elsewhere are fine — substitution uses `str.replace`, not `.format`. Missing `{text}` is appended with a warning rather than publishing a header with no post in it. The channel footer is appended regardless of the template. |

### The channel footer

Appended to the bottom of every published post, from the configured channel
details only:

```
📢 Dovio Twitter
@doviotwitter
```

* The name line comes from `TELEGRAM_CHANNEL_NAME` (empty → no name line).
* The handle comes from `TELEGRAM_CHANNEL_USERNAME`, falling back to a `@name`
  `TELEGRAM_CHANNEL_ID`. A private channel with neither gets no handle — the bot
  never invents one.
* When the text of a long media post is sent as a separate message
  (`LONG_CAPTION_MODE`), the footer rides on whichever message is sent last, so
  it appears once per post rather than on both.

---

## 7. Runtime

| Variable | Default | Notes |
|---|---|---|
| `RUN_MODE` | `all` | `bot`, `worker` or `all`. Compose overrides this per service. |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FORMAT` | `json` | `json` for machine-readable, `console` for a local terminal. |
| `LOG_DIR`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT` | `/app/logs`, 10 MiB, 7 | Rotating file, in addition to stdout. An unwritable directory disables file logging with a warning rather than stopping the app. |
| `TIMEZONE` | `UTC` | **Changes how `stats_daily` buckets existing rows.** A day is the local calendar date: in UTC+3:30, local midnight on the 1st is 20:30 UTC on the 30th, so the rollup converts to the zone *before* taking the date. Changing this re-files future days under a different heading than past ones. |
| `HEARTBEAT_FILE` | `/app/run/heartbeat` | Touched on a timer; the container healthcheck fails when it goes stale. Compose overrides it per service (`heartbeat-bot`, `heartbeat-worker`). |
| `HEARTBEAT_MAX_AGE` | `120` | Seconds. Beats happen at a quarter of this, so three may be missed before the healthcheck complains. |

Every log line carries a `correlation_id`:

```bash
docker compose logs worker | grep '"correlation_id":"<uuid>"'
```

### Secrets in logs

`logging_config.RedactionFilter` scrubs bot tokens, `Bearer` values, anything
matching `api_key`/`password`/`secret` assignments, and credentials embedded in
connection URLs — from the message, its arguments, and every string in `extra`.

---

## 8. Accepted but not used by this build

These are validated and reported as startup warnings, but nothing acts on them.
They are listed here rather than quietly dropped, because "setting it did
nothing" is worse than "setting it is documented as inert".

| Variable | Status |
|---|---|
| `REDIS_URL` | **Nothing connects to Redis.** Every piece of durable state — the job queue, the tweet cache, the publish cooldown, pending previews — is in PostgreSQL. The `redis` service has been removed from `docker-compose.yml` and the `redis` package from `requirements.txt` for that reason; leaving this variable empty is the correct configuration. |
| `MEDIA_CACHE_ENABLED` | Media is downloaded into a per-publish workspace (`MEDIA_TMP_DIR`) and removed when the publish ends. There is no cross-job media cache, so a re-publish re-downloads. |
| `SHOW_AUTHOR_AVATAR` | Stored on the tweet and available to a custom template; the shipped templates do not render it. |
| `LOCALE` | One language ships. Every user-facing string is already centralised in `app/bot/messages.py`, which is the expensive half of adding a second. |

---

## 9. Verifying a configuration

```bash
# 1. Structure only — does not contact anything.
docker compose run --rm --no-deps bot python -c \
  "from app.config import load_settings_or_report as l; print('ok'); print(l().safe_dump())"

# 2. Startup problems, without starting the app.
docker compose run --rm --no-deps bot python -c \
  "from app.config import load_settings_or_report as l; p=l().startup_problems(); print('\n'.join(p) or 'no blocking problems')"

# 3. The real thing.
docker compose up -d --build
docker compose logs -f bot worker

# 4. Retrieval, end to end — the real request, with the token and headers the
#    bot sends. A bare curl with ?id= alone answers 200 with an empty body and
#    settles nothing. See § 3.
docker compose exec bot python -m app.providers.syndication 1519480761749016577
```

Then send `/status` to the bot. It reports the resolved channel, database
latency, worker liveness, queue depth, the current publish cooldown, and every
configuration warning — which is usually enough to diagnose a misconfiguration
without opening a log file.
