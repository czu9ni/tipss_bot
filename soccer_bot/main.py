from soccer_bot.api import ApiConfig, SoccerApiClient
from soccer_bot.config import load_config
from soccer_bot.logging import configure_logging


def main() -> None:
    config = load_config()
    configure_logging(config.log_level)
    api_config = ApiConfig(base_url=config.api_base_url, api_key=config.api_key)
    SoccerApiClient(api_config)


if __name__ == "__main__":
    main()