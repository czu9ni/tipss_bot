from soccer_bot.providers.base import OddsProvider, StatsProvider
from soccer_bot.providers.api_football import ApiFootballStatsProvider, ApiFootballOddsProvider
from soccer_bot.providers.the_odds_api import TheOddsApiProvider
from soccer_bot.providers.sportradar import SportradarStatsProvider


def _assert_unique_names(providers: dict[str, type]) -> None:
    names: dict[str, str] = {}
    for key, provider in providers.items():
        name = getattr(provider, "name", key)
        if name in names:
            raise RuntimeError(f"Duplicate provider name: {name}")
        names[name] = key


def build_stats_provider(name: str, **kwargs) -> StatsProvider:
    providers = {
        "api_football": ApiFootballStatsProvider,
        "sportradar": SportradarStatsProvider,
    }
    _assert_unique_names(providers)
    if name not in providers:
        raise ValueError(f"Unknown stats provider: {name}")
    return providers[name](**kwargs)


def build_odds_provider(name: str, **kwargs) -> OddsProvider:
    providers = {
        "the_odds_api": TheOddsApiProvider,
        "api_football": ApiFootballOddsProvider,
    }
    _assert_unique_names(providers)
    if name not in providers:
        raise ValueError(f"Unknown odds provider: {name}")
    return providers[name](**kwargs)
