import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    db_url: str
    api_base_url: str
    api_key: str
    odds_api_key: str
    football_data_token: str
    weather_api_key: str
    news_api_key: str
    log_level: str


REQUIRED_ENV_VARS = ("SOCCER_DB_URL", "SOCCER_API_BASE_URL", "SOCCER_API_KEY", "ODDS_API_KEY", "FOOTBALL_DATA_TOKEN", "WEATHER_API_KEY", "NEWS_API_KEY")


def load_config(environ: dict[str, str] | None = None) -> AppConfig:
    env = environ or os.environ
    missing = [key for key in REQUIRED_ENV_VARS if not env.get(key)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    return AppConfig(
        db_url=env["SOCCER_DB_URL"],
        api_base_url=env["SOCCER_API_BASE_URL"],
        api_key=env["SOCCER_API_KEY"],
        odds_api_key=env["ODDS_API_KEY"],
        football_data_token=env["FOOTBALL_DATA_TOKEN"],
        weather_api_key=env["WEATHER_API_KEY"],
        news_api_key=env["NEWS_API_KEY"],
        log_level=env.get("SOCCER_LOG_LEVEL", "INFO"),
    )