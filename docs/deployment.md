# Deployment

Single-VPS deployment with Docker Compose. Written for a fresh Ubuntu 22.04/24.04
or Debian 12 host, but nothing here is distro-specific beyond the Docker install.

## What you need first

| Thing | Where to get it |
|---|---|
| Bot token | [@BotFather](https://t.me/BotFather) → `/newbot`. Looks like `123456789:AA...` |
| Channel ID | Numeric `-100...` (preferred) — forward a channel post to [@userinfobot](https://t.me/userinfobot). A public `@username` also works. |
| Your Telegram user ID | [@userinfobot](https://t.me/userinfobot). Comma-separate several admins. |
| Tweet provider credential | **None, by default.** `TWEET_PROVIDER=syndication` needs no account. Only if you switch providers — see `.env.example` section 3. |

**Do not skip this:** add the bot to the channel as an **administrator** with the
"Post Messages" permission. A bot that is merely a member cannot post, and the
failure appears only at publish time as a `ChatWriteForbidden` error.

### Sizing

2 GB RAM is comfortable (PostgreSQL plus the two Python processes). 1 GB works if
you cap PostgreSQL's `shared_buffers`; 512 MB is tight, because media is staged
on disk and partly in memory while it is being uploaded.

## 1. Prepare the server

```bash
# Docker Engine + the Compose v2 plugin
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"     # log out and back in for this to apply
sudo systemctl enable --now docker  # so the stack survives a reboot
```

No firewall changes are needed. The bot uses **long polling**, so it dials out to
`api.telegram.org` and nothing ever connects *in*. There is no port to open, no
domain, and no TLS certificate to renew.

Confirm outbound access:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://api.telegram.org
```

## 2. Put the code on the server

```bash
sudo mkdir -p /opt/tweet-relay && sudo chown "$USER" /opt/tweet-relay
# then, one of:
git clone <your-repo> /opt/tweet-relay
scp -r ./tweet-relay/* you@vps:/opt/tweet-relay/
cd /opt/tweet-relay
```

## 3. Create the environment file

```bash
cp .env.example .env
chmod 600 .env        # contains the bot token and the database password
nano .env
```

At minimum, set:

* `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `ADMIN_USER_IDS`
* `POSTGRES_PASSWORD` — a long random string; also used inside `DATABASE_URL`
* `DATABASE_URL` — must reference the **`postgres`** hostname and match
  `POSTGRES_PASSWORD`:
  `postgresql+asyncpg://tweetrelay:<same password>@postgres:5432/tweetrelay`

That is everything required. `TWEET_PROVIDER` defaults to `syndication`, which
needs no credential; leave it alone unless you have a reason not to.

Leave `RUN_MODE` alone — Compose sets it per service. Worth setting once, for the
footer of every published post:

```bash
TELEGRAM_CHANNEL_NAME=Your Channel Name  # footer line 1; empty shows only the handle
TELEGRAM_CHANNEL_USERNAME=yourchannel    # footer line 2; optional if TELEGRAM_CHANNEL_ID is @yourchannel
```

Generate a password with:

```bash
openssl rand -base64 32
```

## 4. Start it

```bash
docker compose up -d --build
```

What happens, in order:

1. `postgres` starts and passes its healthcheck.
2. `migrate` runs `alembic upgrade head` and **exits**. Both app services wait for
   it to succeed, so a broken migration stops the deploy instead of leaving the
   app running against a schema it does not understand.
3. `bot` and `worker` start. The bot begins long polling; the worker begins
   claiming jobs.

## 5. Verify

```bash
docker compose ps                    # every service Up; migrate shows "Exited (0)"
docker compose logs -f bot worker
```

In Telegram, send `/start` to your bot from an admin account. `docker compose ps`
should show `bot` and `worker` as `healthy` within about a minute of starting.

### Verify tweet retrieval before you rely on it

This is the step that matters, and it is separate from "the containers are up".
Nothing in this repository has ever made a request to the retrieval endpoint, so
the first real check is yours. Run the provider itself:

```bash
docker compose exec bot python -m app.providers.syndication 1519480761749016577
```

Do **not** substitute a bare
`curl 'https://cdn.syndication.twimg.com/tweet-result?id=…'`. That request omits
the token, the `lang`, the browser `User-Agent` and the `Referer`, and the
endpoint answers it with **200 and a 2-byte body** — which looks like the fault
but is simply what that request gets. Use the command above, which sends exactly
what the bot sends.

| What it prints | What to do |
|---|---|
| The author, photo count and text | Working. Paste a link into the bot and the rest follows. The self-test also prints a `videos:` count — one means the post is a video tweet, which this build publishes as a playable clip. |
| `FAILED: ProviderResponseError` … `returned no data` | The endpoint answered with `{}`. Try a tweet you can see in a browser; if that also fails, the computed token has drifted — copy a working `token` into `SYNDICATION_TOKEN` (`docs/configuration.md` § 3). |
| `FAILED: ProviderTransientError` mentioning an HTML challenge | Cloudflare is challenging this VPS's IP. Use another host or another provider. |
| `FAILED: ProviderTransientError` … timeout or connection error | The VPS has no outbound HTTPS to X. `curl -sS -o /dev/null -w '%{http_code}\n' https://api.telegram.org` will fail too. |

Then confirm the whole path end to end: paste a public tweet URL into the bot.
There is no confirmation step — the post should appear in the channel with its
photos or its video, the tweet text, and your channel footer, and **without** the
author's `@handle`, the original tweet link or its timestamp. A video arrives as
a playable clip; if the tweet's video is unavailable or offered only as an HLS
stream, the post goes out as text and the admin receipt says one item was
omitted.

If you see `unhealthy`, ask why before assuming it is cosmetic:

```bash
docker inspect --format '{{json .State.Health}}' tweet-relay-bot-1 | jq
docker compose exec bot python -m app.healthcheck
```

## Everyday operations

```bash
docker compose logs -f --tail=100 worker   # follow one service
docker compose restart worker              # restart without touching storage
docker compose down                        # stop, keep data
docker compose down -v                     # stop AND DELETE the database — careful

# Update to new code
git pull
docker compose up -d --build               # migrate runs automatically

# Database backup / restore
docker compose exec -T postgres pg_dump -U tweetrelay tweetrelay > backup-$(date +%F).sql
docker compose exec -T postgres psql -U tweetrelay tweetrelay < backup-2026-01-01.sql
```

Back up on a schedule — the `postgres-data` volume holds every publication record
and therefore the duplicate-suppression index. Losing it means the bot can
repost things it has already posted. A daily `pg_dump` into a directory that is
itself backed up is enough.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `migrate` exits non-zero | Read `docker compose logs migrate`. Usually `DATABASE_URL` does not match `POSTGRES_PASSWORD`. |
| `postgres` never becomes healthy | `POSTGRES_PASSWORD` unset in `.env`. Compose aborts with an explicit message. |
| Bot starts, replies to nothing | `ADMIN_USER_IDS` is empty or does not include your ID — the bot ignores everyone else by design. Check `docker compose logs bot`. |
| `ChatWriteForbidden` on publish | The bot is not an administrator of the channel, or lacks "Post Messages". |
| `unhealthy` but logs look fine | Event loop wedged or the database went away. `docker compose exec bot python -m app.healthcheck` gives the reason. |
| "could not retrieve tweet … from cdn.syndication.twimg.com" | Run the `python -m app.providers.syndication <id>` command the message names, then the table in step 5. This is the one failure this project could not pre-verify. |
| "…returned no data for tweet … (HTTP 200, empty result)" | The endpoint answered `{}`. Try a tweet you can see in a browser; if that also fails, the token has drifted. See `docs/configuration.md` § 3. |
| Publish errors at ~20/minute | Expected — Telegram's channel limit. Raise `PUBLISH_COOLDOWN` rather than retrying faster. |

## Notes

* **Redis is not part of this stack.** It used to be a service in
  `docker-compose.yml`; it was removed because nothing ever connected to it.
  `REDIS_URL` is still accepted and validated for forward compatibility, but
  every durable thing — the job queue, the tweet cache, the publish cooldown, the
  pending confirmations — lives in PostgreSQL, so leaving it empty is correct.
* **Enum values are stored lowercase** (`pending`, not `PENDING`). Hand-written SQL
  against these tables must match, or a `CHECK` constraint will reject it.
* **Timezone.** `TIMEZONE` affects display and the daily-stats rollover only;
  everything in the database is UTC. Changing it re-buckets future stats rows but
  not existing ones.
