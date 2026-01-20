from soccer_bot.api import ApiConfig, SoccerApiClient
from soccer_bot.config import load_config
from soccer_bot.db import connect
from soccer_bot.logging import configure_logging
from soccer_bot.repo import Match, add_match, list_matches
from soccer_bot.scoring import table

import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def _build_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "tipss_bot/1.0"})
    return session


def main() -> None:
    config = load_config()
    configure_logging(config.log_level)
    demo_mode = os.environ.get("SOCCER_DEMO", "0") == "1"
    session = _build_session()
    if config.api_base_url and config.api_key:
        api_config = ApiConfig(base_url=config.api_base_url, api_key=config.api_key)
        SoccerApiClient(api_config)
        print("API client initialized.")
    else:
        print("API client not configured (SOCCER_API_BASE_URL/SOCCER_API_KEY missing).")
    print("ODDS_API_KEY loaded.")
    print("FOOTBALL_DATA_TOKEN loaded.")
    if config.weather_api_key:
        print(f"WEATHER_API_KEY loaded: {config.weather_api_key[:4]}...")
    else:
        print("WEATHER: Open-Meteo (no key)")
    if config.news_api_key:
        print(f"NEWS_API_KEY loaded: {config.news_api_key[:4]}...")
    else:
        print("NEWS: RSS feeds (no key)")

    if not demo_mode:
        return

    headers = {"X-Auth-Token": config.football_data_token}
    try:
        response = session.get("https://api.football-data.org/v4/competitions", headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            print(f"\nFetched {len(data.get('competitions', []))} competitions from Football Data API.")
            for comp in data.get("competitions", [])[:3]:
                print(f"- {comp.get('name', '?')} ({comp.get('code', '?')})")
        else:
            print(f"\nFootball Data API error: {response.status_code}")
    except Exception as e:
        print(f"\nFootball Data API call failed: {e}")

    try:
        odds_url = "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
        params = {"apiKey": config.odds_api_key, "regions": "eu", "markets": "h2h"}
        response = session.get(odds_url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data:
                match = data[0]
                home_team = match.get("home_team", "?")
                away_team = match.get("away_team", "?")
                bookmakers = match.get("bookmakers") or []
                markets = bookmakers[0].get("markets") if bookmakers else []
                outcomes = markets[0].get("outcomes") if markets else []
                if not outcomes:
                    print("\nOdds data missing outcomes.")
                else:
                    home_odds = next((o.get("price") for o in outcomes if o.get("name") == home_team), None)
                    away_odds = next((o.get("price") for o in outcomes if o.get("name") == away_team), None)
                    draw_odds = next((o.get("price") for o in outcomes if o.get("name") == "Draw"), None)
                    print(
                        f"\nOdds for {home_team} vs {away_team}: Home {home_odds}, Draw {draw_odds}, Away {away_odds}"
                    )
                    if isinstance(home_odds, (int, float)) and home_odds < 2.0:
                        print("Tip: Bet on Home win (low odds indicate favorite).")
                    else:
                        print("Tip: Consider Draw or Away (home not favored).")
            else:
                print("\nNo odds data available.")
        else:
            print(f"\nOdds API error: {response.status_code}")
    except Exception as e:
        print(f"\nOdds API call failed: {e}")

    db = connect(config.db_url)
    db.ensure_schema()
    sample_matches = [
        Match("Team A", "Team B", 2, 1, "2023-01-01"),
        Match("Team A", "Team C", 1, 1, "2023-01-02"),
        Match("Team B", "Team C", 0, 3, "2023-01-03"),
        Match("Team A", "Team B", 3, 0, "2023-01-04"),
        Match("Team C", "Team A", 2, 2, "2023-01-05"),
        Match("Team B", "Team C", 1, 1, "2023-01-06"),
    ]
    for match in sample_matches:
        try:
            add_match(db, match)
        except Exception:
            pass
    all_matches = list_matches(db)
    print("Matches:")
    for match in all_matches:
        print(f"{match.home_team} {match.home_score}-{match.away_score} {match.away_team}")
    points_table = table(all_matches)
    print("\nPoints Table:")
    for team, points in sorted(points_table.items(), key=lambda x: x[1], reverse=True):
        print(f"{team}: {points} points")


if __name__ == "__main__":
    main()
