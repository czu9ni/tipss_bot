import os
from pydantic import BaseModel, Field, ValidationError


class AppConfig(BaseModel):
    db_url: str = Field(..., description="Database URL, must be SQLite")
    api_base_url: str = Field(default="", description="Base URL for API (optional)")
    api_key: str = Field(default="", description="API key for general API (optional)")
    odds_api_key: str = Field(..., min_length=10, description="Odds API key")
    football_data_token: str = Field(..., min_length=10, description="Football Data token")
    log_level: str = Field(default="INFO", regex="^(DEBUG|INFO|WARNING|ERROR)$", description="Logging level")


REQUIRED_ENV_VARS = ("SOCCER_DB_URL", "ODDS_API_KEY", "FOOTBALL_DATA_TOKEN")


def load_config(environ: dict[str, str] | None = None) -> AppConfig:
    env = environ or os.environ
    missing = [key for key in REQUIRED_ENV_VARS if not env.get(key)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    try:
        return AppConfig(
            db_url=env["SOCCER_DB_URL"],
            api_base_url=env.get("SOCCER_API_BASE_URL", ""),
            api_key=env.get("SOCCER_API_KEY", ""),
            odds_api_key=env["ODDS_API_KEY"],
            football_data_token=env["FOOTBALL_DATA_TOKEN"],
            weather_api_key=env.get("WEATHER_API_KEY", ""),
            news_api_key=env.get("NEWS_API_KEY", ""),
            log_level=env.get("SOCCER_LOG_LEVEL", "INFO"),
        )
    except ValidationError as e:
        raise ValueError(f"Configuration validation error: {e}")
