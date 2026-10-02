"""Environment-driven configuration.

Field names intentionally mirror the environment variable names 1:1 so that
`.env.example`, the docs and the code never drift apart.

Nothing here reads the network or the database: `Settings` must be constructible
in a test process with no services running.
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
CHANNEL_ID_RE = re.compile(r"^-100\d{5,}$")
CHANNEL_USERNAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{3,31}$")


class RunMode(StrEnum):
    BOT = "bot"
    WORKER = "worker"
    ALL = "all"


class TweetProviderName(StrEnum):
    SYNDICATION = "syndication"
    MOCK = "mock"
    X_API = "x_api"
    GENERIC_HTTP = "generic_http"


class TemplateName(StrEnum):
    MINIMAL = "minimal"
    RICH = "rich"
    CUSTOM = "custom"


class LongCaptionMode(StrEnum):
    TEXT_FIRST = "text_first"
    MEDIA_FIRST = "media_first"
    TRUNCATE = "truncate"


class MediaFailureMode(StrEnum):
    TEXT_LINK = "text_link"
    SKIP_MEDIA = "skip_media"
    FAIL = "fail"


class ConfigurationError(RuntimeError):
    """Raised when the configuration is structurally valid but unusable."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
    )

    # ------------------------------------------------------------------ Telegram
    # No default on purpose. A non-empty default would (a) put a live credential
    # in source control and (b) make `startup_problems()` unable to notice that
    # the operator forgot to set one, because the field would never be empty.
    TELEGRAM_BOT_TOKEN: SecretStr = SecretStr("")
    TELEGRAM_CHANNEL_ID: str = ""
    #: Public @username of the channel. Only needed when `TELEGRAM_CHANNEL_ID` is
    #: the numeric form — a `@name` id is used as-is. Leave empty for a private
    #: channel: the footer then omits the handle rather than inventing one.
    TELEGRAM_CHANNEL_USERNAME: str = ""
    #: Display name shown on the footer of every published post, e.g. "Dovio
    #: Twitter". Purely cosmetic; empty means the footer shows the handle alone.
    TELEGRAM_CHANNEL_NAME: str = ""
    ADMIN_USER_IDS: Annotated[frozenset[int], NoDecode] = frozenset()

    # ----------------------------------------------------------------- Datastores
    DATABASE_URL: str = "postgresql+asyncpg://tweetrelay:tweetrelay@localhost:5432/tweetrelay"
    REDIS_URL: str = ""
    DB_POOL_SIZE: int = Field(default=5, ge=1, le=50)
    DB_MAX_OVERFLOW: int = Field(default=5, ge=0, le=50)
    DB_ECHO: bool = False

    # ------------------------------------------------------------------ Provider
    # Defaults to `syndication`: X's own public embed endpoint, which needs no
    # account, no API key and no third party in the middle. It is the only
    # provider that works out of the box, and therefore the only honest default
    # for a relay bot — anything else would make the documented quick start fail
    # at `startup_problems()` until a credential is bought.
    #
    # `mock` is never the default: a misconfigured deployment would publish
    # fixture text into a real channel and look like it was working.
    TWEET_PROVIDER: TweetProviderName = TweetProviderName.SYNDICATION
    TWEET_API_KEY: SecretStr = SecretStr("")
    TWEET_API_BASE_URL: str = ""
    TWEET_API_TWEET_PATH: str = "/tweets/{tweet_id}"
    TWEET_API_AUTH_HEADER: str = "Authorization"
    TWEET_API_AUTH_SCHEME: str = "Bearer"
    TWEET_API_RESPONSE_ROOT: str = "data"
    TWEET_API_TIMEOUT: float = Field(default=15.0, gt=0, le=120)
    TWEET_CACHE_TTL: int = Field(default=21_600, ge=0)

    # ------------------------------------------------- `syndication` provider
    # The endpoint X's own embedded-tweet widget calls. Changing the URL is
    # supported so the host can be moved without a code change, but there is
    # rarely a reason to.
    SYNDICATION_URL: str = "https://cdn.syndication.twimg.com/tweet-result"
    #: Optional override for the `token` query parameter. The provider derives
    #: one from the tweet ID using X's own (reverse-engineered) formula; set this
    #: only if that formula stops matching and you have captured a working token.
    SYNDICATION_TOKEN: SecretStr = SecretStr("")

    # --------------------------------------------------------------- Publishing
    # There is no confirmation step: an authorized admin sending a tweet URL is
    # the approval. These settings only govern how the publish is paced.
    MAX_CONCURRENT_FETCHES: int = Field(default=4, ge=1, le=64)
    MAX_CONCURRENT_PUBLISHES: int = Field(default=2, ge=1, le=16)
    MAX_RETRIES: int = Field(default=5, ge=0, le=50)
    RETRY_BASE_DELAY: float = Field(default=2.0, gt=0, le=600)
    RETRY_MAX_DELAY: float = Field(default=900.0, gt=0, le=86_400)
    PUBLISH_COOLDOWN: float = Field(default=4.0, ge=0, le=3600)
    JOB_LOCK_TIMEOUT: int = Field(default=600, ge=30, le=86_400)
    WORKER_POLL_INTERVAL: float = Field(default=2.0, ge=0.1, le=60)

    # -------------------------------------------------------------------- Media
    MEDIA_MAX_SIZE_MB: int = Field(default=45, ge=1, le=50)
    MEDIA_CACHE_ENABLED: bool = True
    MEDIA_CONNECT_TIMEOUT: float = Field(default=10.0, gt=0, le=120)
    MEDIA_DOWNLOAD_TIMEOUT: float = Field(default=120.0, gt=0, le=1800)
    MEDIA_TMP_DIR: Path = Path("/app/tmp/media")
    MEDIA_ALLOWED_HOSTS: Annotated[tuple[str, ...], NoDecode] = (
        "pbs.twimg.com",
        "video.twimg.com",
        "abs.twimg.com",
        "ton.twimg.com",
    )
    ALLOW_VIDEO: bool = True
    MEDIA_FAILURE_MODE: MediaFailureMode = MediaFailureMode.TEXT_LINK
    LONG_CAPTION_MODE: LongCaptionMode = LongCaptionMode.TEXT_FIRST
    ALLOW_PRIVATE_MEDIA_HOSTS: bool = False

    # ----------------------------------------------------------------- Template
    # A published post is: the author's display name, the tweet text, its photos,
    # and the channel footer. The author's @handle, the source link and the
    # original timestamp are deliberately never rendered.
    POST_TEMPLATE: TemplateName = TemplateName.RICH
    SHOW_AUTHOR_AVATAR: bool = True
    INCLUDE_SIGNATURE: bool = False
    TEMPLATE_HEADER: str = ""
    TEMPLATE_FOOTER: str = ""
    TEMPLATE_SIGNATURE: str = ""
    #: `{name}` is the only value the shipped templates substitute; `{handle}`,
    #: `{username}` and `{id}` remain for custom formats but are not rendered by
    #: default. A typo degrades to `{name}` rather than failing a publish.
    TEMPLATE_AUTHOR_FORMAT: str = "{name}"
    TEMPLATE_EMOJI_AUTHOR: str = "🐦"
    #: Prefix for the channel identity footer; set empty to render it bare.
    TEMPLATE_EMOJI_CHANNEL: str = "📢"
    TEMPLATE_CUSTOM: str = ""

    # ------------------------------------------------------------------ Runtime
    RUN_MODE: RunMode = RunMode.ALL
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"
    LOG_DIR: Path = Path("/app/logs")
    LOG_MAX_BYTES: int = Field(default=10_485_760, ge=65_536)
    LOG_BACKUP_COUNT: int = Field(default=7, ge=0, le=100)
    TIMEZONE: str = "UTC"
    HEARTBEAT_FILE: Path = Path("/app/run/heartbeat")
    HEARTBEAT_MAX_AGE: int = Field(default=120, ge=10, le=3600)
    LOCALE: str = "en"

    # ------------------------------------------------------------- Normalisation
    @field_validator("ADMIN_USER_IDS", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> frozenset[int]:
        if value is None or value == "":
            return frozenset()
        if isinstance(value, str):
            parts = [p.strip() for p in value.replace(";", ",").split(",")]
            ids: list[int] = []
            for part in parts:
                if not part:
                    continue
                if not re.fullmatch(r"-?\d+", part):
                    raise ValueError(f"{part!r} is not a valid Telegram user ID")
                ids.append(int(part))
            return frozenset(ids)
        if isinstance(value, (list, tuple, set, frozenset)):
            return frozenset(int(v) for v in value)
        raise ValueError("expected a comma-separated list of Telegram user IDs")

    @field_validator("MEDIA_ALLOWED_HOSTS", mode="before")
    @classmethod
    def _parse_hosts(cls, value: object) -> tuple[str, ...]:
        if value is None or value == "":
            return ()
        if isinstance(value, str):
            return tuple(
                h.strip().lower().lstrip(".") for h in value.split(",") if h.strip()
            )
        if isinstance(value, (list, tuple, set, frozenset)):
            return tuple(str(h).strip().lower().lstrip(".") for h in value if str(h).strip())
        raise ValueError("expected a comma-separated list of hostnames")

    @field_validator("DATABASE_URL")
    @classmethod
    def _normalise_db_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("DATABASE_URL must not be empty")
        if value.startswith("postgres://"):
            value = "postgresql://" + value[len("postgres://") :]
        if value.startswith("postgresql://"):
            value = "postgresql+asyncpg://" + value[len("postgresql://") :]
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must be a PostgreSQL URL "
                "(postgresql+asyncpg://user:password@host:5432/dbname)"
            )
        return value

    @field_validator("TELEGRAM_CHANNEL_ID")
    @classmethod
    def _normalise_channel(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        if value.lstrip("-").isdigit():
            return value
        return value if value.startswith("@") else f"@{value}"

    @field_validator("TELEGRAM_CHANNEL_USERNAME")
    @classmethod
    def _normalise_channel_username(cls, value: str) -> str:
        return value.strip().lstrip("@")

    @field_validator("TIMEZONE")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        value = value.strip() or "UTC"
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"unknown timezone {value!r}; use an IANA name such as 'Europe/Berlin'"
            ) from exc
        return value

    @field_validator("TWEET_API_BASE_URL")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("TWEET_API_BASE_URL must start with http:// or https://")
        return value

    @field_validator("SYNDICATION_URL")
    @classmethod
    def _validate_syndication_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            raise ValueError("SYNDICATION_URL must not be empty")
        if not value.startswith("https://"):
            # Plain HTTP here would send the request in clear text and, worse,
            # would have to be allowlisted past the SSRF policy's expectations.
            raise ValueError("SYNDICATION_URL must start with https://")
        return value

    @model_validator(mode="after")
    def _cross_field_checks(self) -> Settings:
        if self.RETRY_MAX_DELAY < self.RETRY_BASE_DELAY:
            raise ValueError("RETRY_MAX_DELAY must be >= RETRY_BASE_DELAY")
        if (
            self.POST_TEMPLATE is TemplateName.CUSTOM
            and not self.TEMPLATE_CUSTOM.strip()
        ):
            raise ValueError("POST_TEMPLATE=custom requires TEMPLATE_CUSTOM to be set")
        if self.TWEET_PROVIDER is TweetProviderName.GENERIC_HTTP:
            if not self.TWEET_API_BASE_URL:
                raise ValueError("TWEET_PROVIDER=generic_http requires TWEET_API_BASE_URL")
            if "{tweet_id}" not in self.TWEET_API_TWEET_PATH:
                raise ValueError("TWEET_API_TWEET_PATH must contain the '{tweet_id}' placeholder")
        return self

    # ------------------------------------------------------------- Derived views
    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.TIMEZONE)

    @property
    def media_max_bytes(self) -> int:
        return self.MEDIA_MAX_SIZE_MB * 1024 * 1024

    @property
    def redis_enabled(self) -> bool:
        return bool(self.REDIS_URL.strip())

    @property
    def runs_bot(self) -> bool:
        return self.RUN_MODE in (RunMode.BOT, RunMode.ALL)

    @property
    def runs_worker(self) -> bool:
        return self.RUN_MODE in (RunMode.WORKER, RunMode.ALL)

    @property
    def owner_id(self) -> int | None:
        """Lowest configured admin ID, bootstrapped with the `owner` role."""
        return min(self.ADMIN_USER_IDS) if self.ADMIN_USER_IDS else None

    @property
    def channel_handle(self) -> str:
        """The channel's public @username (without the `@`), or `""`.

        Falls back to a `@name` `TELEGRAM_CHANNEL_ID`, which is a configured
        detail rather than an invented one. A numeric (private) channel with no
        configured username returns empty, and the footer then omits the handle.
        """
        username = self.TELEGRAM_CHANNEL_USERNAME.strip().lstrip("@")
        if username:
            return username
        channel_id = self.TELEGRAM_CHANNEL_ID.strip()
        if channel_id.startswith("@"):
            return channel_id.lstrip("@")
        return ""

    def channel_message_url(self, message_id: int) -> str | None:
        """Best-effort public link to a published channel message."""
        username = self.channel_handle
        if username:
            return f"https://t.me/{username}/{message_id}"
        if CHANNEL_ID_RE.match(self.TELEGRAM_CHANNEL_ID):
            return f"https://t.me/c/{self.TELEGRAM_CHANNEL_ID[4:]}/{message_id}"
        return None

    # ------------------------------------------------------- Startup validation
    def startup_problems(self) -> list[str]:
        """Blocking problems that make live operation impossible.

        Returned as plain strings so the caller can print them without ever
        touching the underlying secret values.
        """
        problems: list[str] = []
        token = self.TELEGRAM_BOT_TOKEN.get_secret_value().strip()
        if not token:
            problems.append("TELEGRAM_BOT_TOKEN is not set (get one from @BotFather).")
        elif not TOKEN_RE.match(token):
            problems.append(
                "TELEGRAM_BOT_TOKEN is malformed; it must look like '123456789:AA...' "
                "(value withheld from this message)."
            )

        if not self.TELEGRAM_CHANNEL_ID:
            problems.append("TELEGRAM_CHANNEL_ID is not set.")
        elif not (
            CHANNEL_ID_RE.match(self.TELEGRAM_CHANNEL_ID)
            or CHANNEL_USERNAME_RE.match(self.TELEGRAM_CHANNEL_ID)
        ):
            problems.append(
                f"TELEGRAM_CHANNEL_ID={self.TELEGRAM_CHANNEL_ID!r} is not a valid channel "
                "reference; expected '-100...' or '@channelusername'."
            )

        if not self.ADMIN_USER_IDS:
            problems.append(
                "ADMIN_USER_IDS is empty, so nobody could use the bot. "
                "Add your Telegram user ID (see @userinfobot)."
            )

        if self.TWEET_PROVIDER is TweetProviderName.X_API and not self.TWEET_API_KEY.get_secret_value():
            problems.append("TWEET_PROVIDER=x_api requires TWEET_API_KEY (an X API Bearer token).")
        if (
            self.TWEET_PROVIDER is TweetProviderName.GENERIC_HTTP
            and not self.TWEET_API_KEY.get_secret_value()
        ):
            problems.append("TWEET_PROVIDER=generic_http requires TWEET_API_KEY.")
        # `syndication` is deliberately absent: it needs no credential, which is
        # exactly why it is the default.

        return problems

    def startup_warnings(self) -> list[str]:
        """Non-blocking advisories worth logging once at startup."""
        warnings: list[str] = []
        if self.TWEET_PROVIDER is TweetProviderName.MOCK:
            warnings.append(
                "TWEET_PROVIDER=mock — no real tweet is retrieved; fixture text is "
                "published instead. Set TWEET_PROVIDER=syndication for live operation."
            )
        if self.TWEET_PROVIDER is TweetProviderName.SYNDICATION:
            warnings.append(
                "TWEET_PROVIDER=syndication uses X's public embed endpoint "
                "(cdn.syndication.twimg.com). It needs no credential, but it is an "
                "internal endpoint X owes you no stability on. If retrieval starts "
                "failing, run the curl shown in the error message before changing "
                "anything else."
            )
        if not self.redis_enabled:
            warnings.append(
                "REDIS_URL is empty, which is the intended configuration: every piece "
                "of durable state is in PostgreSQL and nothing in this build connects "
                "to Redis. Setting it changes nothing."
            )
        if not self.TELEGRAM_CHANNEL_NAME:
            warnings.append(
                "TELEGRAM_CHANNEL_NAME is empty, so the footer of every published post "
                "will show only the channel username. Set it to the channel's display "
                "name for a complete footer."
            )
        if not self.channel_handle:
            warnings.append(
                "The channel has no public username (TELEGRAM_CHANNEL_ID is numeric and "
                "TELEGRAM_CHANNEL_USERNAME is empty), so published posts carry only the "
                "channel name in their footer. Set TELEGRAM_CHANNEL_USERNAME to include "
                "the @handle."
            )
        if self.PUBLISH_COOLDOWN < 3.0:
            warnings.append(
                f"PUBLISH_COOLDOWN={self.PUBLISH_COOLDOWN}s is below the ~20 messages/minute "
                "Telegram guideline for channels; flood-control errors are likely."
            )
        if self.ALLOW_PRIVATE_MEDIA_HOSTS:
            warnings.append(
                "ALLOW_PRIVATE_MEDIA_HOSTS=true disables SSRF protection for media "
                "downloads. Never enable this in production."
            )
        return warnings

    def safe_dump(self) -> dict[str, object]:
        """Configuration snapshot with every secret replaced by a marker."""
        data: dict[str, object] = {}
        for name, value in self.model_dump().items():
            if isinstance(value, SecretStr) or isinstance(getattr(self, name), SecretStr):
                raw = getattr(self, name).get_secret_value()
                data[name] = f"<set:{len(raw)} chars>" if raw else "<unset>"
            elif name == "DATABASE_URL":
                data[name] = redact_url_password(str(value))
            elif name == "REDIS_URL":
                data[name] = redact_url_password(str(value)) if value else "<unset>"
            elif isinstance(value, (frozenset, set)):
                data[name] = sorted(value)
            elif isinstance(value, Path):
                data[name] = str(value)
            else:
                data[name] = value
        return data


def redact_url_password(url: str) -> str:
    """`postgresql://u:secret@h/db` -> `postgresql://u:***@h/db`."""
    return re.sub(r"(?<=://)([^:/@]+):([^@]*)(?=@)", r"\1:***", url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def load_settings_or_report(**overrides: object) -> Settings:
    """Build `Settings`, converting pydantic errors into a readable report.

    Never echoes field *values*, so a malformed token cannot leak into logs.
    """
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        lines = ["Invalid configuration:"]
        for err in exc.errors():
            field = ".".join(str(p) for p in err["loc"]) or "<root>"
            lines.append(f"  - {field}: {err['msg']}")
        lines.append("Check your .env file against .env.example.")
        raise ConfigurationError("\n".join(lines)) from None
