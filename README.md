# tweet-relay-bot

A Telegram bot that republishes Twitter/X posts to a Telegram channel
automatically, with a durable retry queue and a record of everything it has
posted.

You paste a link into a private chat with the bot. It fetches the post and
publishes it — there is no confirmation step. The published post carries the
tweet's text, its photos or its video, and your channel's footer, and
deliberately **not** the original author's `@handle`, the tweet's URL or its
timestamp.

---

## Status

**The code in this repository has not been executed, linted, or tested.** It was
written in an environment where the command-safety classifier was unavailable, so
`pytest` and `ruff` could not be run at all. Everything below describes the
*intended* behaviour, and every statement in `docs/` is a description of the code
as written rather than of observed output.

One consequence is worth stating separately, because it is the only claim this
project makes about the outside world:

> **The tweet retrieval path has not been verified against the live endpoint.**
> `TWEET_PROVIDER=syndication` (the default) asks X's own embed endpoint and
> derives the required token with a formula reverse-engineered from X's embed
> script. The code is written and the failure paths are explicit — a 200 with an
> empty body is treated as "no data" rather than as an empty post, and the
> provider retries once without a token if the computed one is refused — but no
> request has ever been made from here, because this machine has no outbound
> network. The first thing to run on your VPS is the command in
> `docs/configuration.md` § 3:
>
> ```bash
> docker compose exec bot python -m app.providers.syndication 1519480761749016577
> ```
>
> It sends exactly what the bot sends and tells you which problem you have. A
> bare `curl` with `?id=` alone is a different request and answers 200 with an
> empty body, so it settles nothing.

The same applies to **video publishing**, which this build now does: the rendition
is chosen from the embed payload, downloaded under the file ceiling, and uploaded
with `sendVideo`. No video tweet has been fetched or uploaded from here, so the
first real video you relay is the first time that path runs against X and
Telegram. An unavailable video, or one offered only as an HLS playlist, degrades
to a text post and is reported as such rather than failing silently.

Before trusting it with a real channel, run:

```bash
ruff check app tests && ruff format --check app tests
pytest -m "not integration"
docker compose run --rm --no-deps bot python -c \
  "from app.database.models import Base; print(sorted(Base.metadata.tables))"
```

The database layer additionally needs a migration round-trip against a real
PostgreSQL — `alembic upgrade head`, then `alembic check` (which asserts the
hand-written migration and the models agree, with no pending diff), then
`alembic downgrade base && alembic upgrade head`.

`docs/deployment.md` § "Troubleshooting" lists the failure modes seen in similar
setups; none of them have been reproduced here.

---

## How it works

```
        Telegram                    ┌──────────────────────────────┐
  ┌──────────────────┐              │  bot                         │
  │ admin pastes a   │  long poll   │   handlers → JobRunner       │
  │ link             │◄────────────►│   writes jobs, publishes     │
  │                  │              └───────────┬──────────────────┘
  └──────────────────┘                          │
                                                │  INSERT jobs
        Telegram                                ▼
  ┌──────────────────┐              ┌──────────────────────────────┐
  │ channel          │◄─────────────│  PostgreSQL                  │
  └──────────────────┘   publish    │   jobs · tweets · media      │
                                    │   publications · submissions │
                                    │   admins · throttle · stats  │
                                    └───────────▲──────────────────┘
                                                │  claim (SKIP LOCKED)
                                    ┌───────────┴──────────────────┐
                                    │  worker                      │
                                    │   fetch → render → download  │
                                    │   → upload, with retries     │
                                    └──────────────────────────────┘
```

`bot` and `worker` are the same image with different `RUN_MODE`. They never talk
to each other directly: the database is the queue, so either can be restarted,
upgraded, or scaled independently while the other keeps working.

### Why a queue instead of "fetch then send"

A naive relay does the work inside the Telegram update handler. That couples a
30-second media upload to a webhook timeout, loses the job when the process
restarts, and retries by re-sending a channel post that may already have gone
out. Here the handler's only job is to **record an intent and acknowledge**;
everything with a network call in it happens in the worker, where it can be
retried, deferred, or abandoned deliberately.

### The pieces

| Path | What lives there |
|---|---|
| `app/config.py` | Every setting, with cross-field validation and a `startup_problems()` list |
| `app/providers/` | `syndication` (default), `x_api`, `generic_http`, `mock` — one `TweetProvider` interface |
| `app/services/` | `fetcher`, `render`, `media`, `publisher`, `heartbeat` |
| `app/worker/` | `runner.py` (the claim loop) and `jobs.py` (the handlers) |
| `app/database/` | Models, async session, repositories, Alembic migrations |
| `app/bot/` | Handlers, keyboards, middleware, the message catalogue |
| `app/utils/` | `net` (SSRF-safe HTTP), `retry`, `text`, `urls`, `timeutil`, `files` |

---

## The parts worth knowing about

**Duplicates are prevented by the database, not by a check.** A partial unique
index on `(tweet_id, channel_id)` where the publication is not `failed` or
`deleted` makes a second post of the same tweet *impossible*, not merely
unlikely. A check-then-insert has a race window; an index does not. The same
trick keeps two identical jobs from being queued at once.

**Failed is not dead.** A job that exhausts `MAX_RETRIES` lands in `dead` — a
state that exists to be looked at. `failed` means "this will never work"
(a deleted post, a missing channel permission) and is not retried, because
retrying a refusal just produces the same refusal more slowly.

**A deferral is not a failure.** Hitting the channel cooldown or a Telegram `429`
reschedules the job *without consuming an attempt*. A busy channel cannot wear
down the retry budget of a job that never failed.

**Media downloads are SSRF-protected.** `app/utils/net.py` resolves hostnames
itself, rejects private/loopback/link-local addresses after resolution (so a
public name pointing at `169.254.169.254` is caught), and pins the connection to
the validated address. Only hosts in `MEDIA_ALLOWED_HOSTS` are fetched. Same
machinery, and the same policy, for the provider API.

**Truncation happens before escaping.** Telegram rejects a message whose
entities do not parse, so text is cut to length *first* and HTML-escaped
*second*. Doing it the other way round can slice `&amp;` into `&am` — the message
still sends, and the post is quietly wrong. `tests/test_render.py` asserts the
invariant on every rendered output.

**Notifications are queued, not sent mid-transaction.** A handler that messaged
the admin inside its transaction could announce a publish that then rolls back.
Handlers return the notification; the worker sends it after the commit lands.

---

## Quick start

Requires Docker with the Compose v2 plugin.

```bash
git clone <your-repo> tweet-relay && cd tweet-relay
cp .env.example .env
chmod 600 .env
$EDITOR .env        # token, channel ID, your user ID
docker compose up -d --build
docker compose logs -f bot worker
```

`docs/configuration.md` explains every setting. The short version of what must be
set:

```env
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHANNEL_ID=-1001234567890
ADMIN_USER_IDS=<your numeric Telegram ID>
POSTGRES_PASSWORD=<openssl rand -base64 32>
DATABASE_URL=postgresql+asyncpg://tweetrelay:<same password>@postgres:5432/tweetrelay
TWEET_PROVIDER=syndication
```

That is the whole list. `syndication` is the default and needs no credential, so
there is no fifth line to fill in — the bot retrieves a real tweet on the first
link you paste.

Then add the bot to the channel as an **administrator** with Post Messages
enabled, send `/start`, and paste a link.

**Check retrieval once, deliberately**, before you rely on it:

```bash
docker compose exec bot python -m app.providers.syndication 1519480761749016577
```

If that prints the author and the text, you are done. If it does not,
`docs/configuration.md` § 3 has a table mapping what you actually see to the one
thing to change.

Run it locally without a channel by setting `TWEET_PROVIDER=mock`; fixture IDs
`1`–`9` return canned posts, including "not found", "rate limited" and
"transient error" so the failure paths can be exercised. **Never leave `mock`
selected in a deployment that posts to a real channel** — it will happily
publish fixture text and look like it is working.

### Commands

| Command | |
|---|---|
| `/start` | Greeting and a one-line how-to |
| `/help` | Full usage |
| `/id` | Your Telegram user ID — how you find the value for `ADMIN_USER_IDS` |
| `/status` | Channel, database latency, worker liveness, queue depth, config warnings |
| `/stats` | Today and the last 7 days |
| `/recent` | The last few publications, with links |

---

## What this build does not do yet

Deferred deliberately, and listed here so that "setting it did nothing" is never
a surprise. `docs/configuration.md` § 8 has the details.

* **Animated GIFs.** X serves a GIF as a silent looping video, and that is how it
  is described to the publisher (`kind = animation`), so a post carrying one is
  relayed as text and the admin receipt says the item will be omitted. Video
  itself *is* published — see the media section above.
* **Redis.** `REDIS_URL` is accepted and validated and **nothing connects to
  it** — the job queue, the tweet cache, the publish cooldown and the pending
  submissions are all in PostgreSQL. The `redis` service that used to sit in
  `docker-compose.yml` has been removed for exactly that reason.
* **Several templates, channels, or languages.** One of each. Every user-facing
  string is centralised in `app/bot/messages.py`, which is the expensive half of
  adding a second language.
* **Scheduling and an admin dashboard.** Neither exists.
* **Media caching.** Media is downloaded per publish into a workspace that is
  deleted afterwards; a re-publish re-downloads.

---

## Documentation

| | |
|---|---|
| `docs/deployment.md` | VPS setup, day-two operations, backup, troubleshooting |
| `docs/configuration.md` | Every setting, and what each provider requires |

---

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
ruff check app tests
pytest -m "not integration"        # no database needed
TEST_DATABASE_URL=postgresql+asyncpg://... pytest -m integration
```

Tests marked `integration` need a live PostgreSQL and are skipped without
`TEST_DATABASE_URL`.

## Licence and use

Relaying other people's posts to a public channel is a redistribution, and most
platforms' terms restrict it. The provider layer is deliberately a plug-in point
so that the retrieval route is a decision you make and can point at, rather than
one baked into the code. Check the terms of whichever provider you configure —
"the endpoint answered" is not the same as "this is permitted".

Only relay posts you have the right to relay, and keep the attribution link on.
