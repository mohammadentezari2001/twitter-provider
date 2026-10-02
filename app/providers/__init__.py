"""Provider selection.

`build_provider(settings)` is the only place that knows which concrete provider
exists. Everything downstream takes a `TweetProvider` and cannot tell them apart.

On the production default: `Settings.TWEET_PROVIDER` defaults to `syndication`,
X's own public embed endpoint, which needs no credential. It is the only provider
that works on a fresh install, so it is the only honest default for a relay bot —
the alternatives would make the documented quick start stop at a blocking
`startup_problems()` entry until a key was bought.

`mock` is never the default: a misconfigured deployment would publish fixture
text into a real channel, which is the worst possible failure mode for a relay
bot because it looks like it works.
"""

from __future__ import annotations

from collections.abc import Callable

from app.config import ConfigurationError, Settings, TweetProviderName
from app.providers.base import TweetProvider

__all__ = [
    "TweetProvider",
    "build_provider",
    "available_providers",
    "PROVIDER_NAMES",
]

PROVIDER_NAMES: tuple[str, ...] = tuple(name.value for name in TweetProviderName)


def _make_mock(settings: Settings) -> TweetProvider:
    from app.providers.mock import MockProvider

    return MockProvider()


def _make_syndication(settings: Settings) -> TweetProvider:
    from app.providers.syndication import SyndicationProvider

    return SyndicationProvider(
        token=settings.SYNDICATION_TOKEN.get_secret_value(),
        timeout=settings.TWEET_API_TIMEOUT,
        base_url=settings.SYNDICATION_URL,
    )


def _make_x_api(settings: Settings) -> TweetProvider:
    from app.providers.x_api import XApiProvider

    return XApiProvider(
        bearer_token=settings.TWEET_API_KEY.get_secret_value(),
        timeout=settings.TWEET_API_TIMEOUT,
    )


def _make_generic_http(settings: Settings) -> TweetProvider:
    from app.providers.generic_http import GenericHttpProvider

    return GenericHttpProvider(
        base_url=settings.TWEET_API_BASE_URL,
        tweet_path=settings.TWEET_API_TWEET_PATH,
        api_key=settings.TWEET_API_KEY.get_secret_value(),
        auth_header=settings.TWEET_API_AUTH_HEADER,
        auth_scheme=settings.TWEET_API_AUTH_SCHEME,
        response_root=settings.TWEET_API_RESPONSE_ROOT,
        timeout=settings.TWEET_API_TIMEOUT,
    )


_FACTORIES: dict[TweetProviderName, Callable[[Settings], TweetProvider]] = {
    TweetProviderName.SYNDICATION: _make_syndication,
    TweetProviderName.MOCK: _make_mock,
    TweetProviderName.X_API: _make_x_api,
    TweetProviderName.GENERIC_HTTP: _make_generic_http,
}


def build_provider(settings: Settings) -> TweetProvider:
    """Instantiate the configured provider.

    Imports are inside the factory functions so that selecting one provider does
    not import the others — `app.providers.mock` must be importable without a
    network stack, and a deployment that never uses `generic_http` should not pay
    for parsing its normaliser.
    """
    factory = _FACTORIES.get(settings.TWEET_PROVIDER)
    if factory is None:  # pragma: no cover - the enum makes this unreachable
        raise ConfigurationError(
            f"unknown TWEET_PROVIDER {settings.TWEET_PROVIDER!r}; "
            f"expected one of {', '.join(PROVIDER_NAMES)}"
        )
    return factory(settings)


def available_providers() -> tuple[str, ...]:
    return PROVIDER_NAMES
