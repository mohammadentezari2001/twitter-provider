# syntax=docker/dockerfile:1

# --------------------------------------------------------------------------- #
#  tweet-relay-bot
#
#  Two stages, so the compiler needed by asyncpg/uvloop/greenlet on an unusual
#  platform never ships in the runtime image.
# --------------------------------------------------------------------------- #

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Wheels for the C extensions in requirements.txt exist for the common
# platforms, but not all of them. Keeping a toolchain in *this* stage only means
# an unusual architecture still builds without bloating the runtime image.
RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=UTC

# `tini` as PID 1: it reaps orphaned children and, more importantly here,
# forwards SIGTERM to the Python process. Without it a `docker compose down`
# would kill the container after the timeout instead of letting the worker
# finish the publish it is in the middle of.
RUN apt-get update \
 && apt-get install --no-install-recommends -y tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Fixed uid/gid so a bind-mounted volume on the host has predictable ownership.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --chown=10001:10001 . .

# Runtime write targets, matching the defaults in app/config.py
# (LOG_DIR, MEDIA_TMP_DIR, HEARTBEAT_FILE). Created and chowned here so the
# container works even when no volume is mounted over them.
RUN mkdir -p /app/logs /app/tmp/media /app/run \
 && chown -R 10001:10001 /app/logs /app/tmp/media /app/run

USER 10001:10001

# A container that is running but wedged — event loop stuck, database gone,
# polling silently dead — is worse than one that has crashed, because Compose
# leaves it alone. This is what turns that into a restart.
# `start-period` covers migrations plus the first Telegram poll.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-m", "app.healthcheck"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]
