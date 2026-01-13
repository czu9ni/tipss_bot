from soccer_bot.api import ApiConfig, SoccerApiClient
from soccer_bot.config import load_config
from soccer_bot.db import connect
from soccer_bot.logging import configure_logging
from soccer_bot.repo import Match, add_match, list_matches
from soccer_bot.scoring import table

import requests


def main() -> None:
    config = load_config()
    configure_logging(config.log_level)
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

    # Actual API call demo: Fetch competitions from Football Data API
    headers = {"X-Auth-Token": config.football_data_token}
    try:
        response = requests.get("https://api.football-data.org/v4/competitions", headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            print(f"\nFetched {len(data.get('competitions', []))} competitions from Football Data API.")
            for comp in data.get('competitions', [])[:3]:  # Show first 3
                print(f"- {comp['name']} ({comp['code']})")
        else:
            print(f"\nFootball Data API error: {response.status_code}")
    except Exception as e:
        print(f"\nFootball Data API call failed: {e}")

    # Actual tip generation: Fetch odds and give a simple tip
    try:
        odds_url = "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
        params = {"apiKey": config.odds_api_key, "regions": "eu", "markets": "h2h"}
        response = requests.get(odds_url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data:
                match = data[0]  # First match
                home_team = match['home_team']
                away_team = match['away_team']
                odds = match['bookmakers'][0]['markets'][0]['outcomes']
                home_odds = next(o['price'] for o in odds if o['name'] == home_team)
                away_odds = next(o['price'] for o in odds if o['name'] == away_team)
                draw_odds = next(o['price'] for o in odds if o['name'] == 'Draw')
                print(f"\nOdds for {home_team} vs {away_team}: Home {home_odds}, Draw {draw_odds}, Away {away_odds}")
                # Simple tip: Bet on home if odds < 2.0
                if home_odds < 2.0:
                    print("Tip: Bet on Home win (low odds indicate favorite).")
                else:
                    print("Tip: Consider Draw or Away (home not favored).")
            else:
                print("\nNo odds data available.")
        else:
            print(f"\nOdds API error: {response.status_code}")
    except Exception as e:
        print(f"\nOdds API call failed: {e}")

    # Stress test: Multiple API calls
    print("\nStarting stress test: 5 quick API calls...")
    for i in range(5):
        try:
            response = requests.get("https://api.football-data.org/v4/competitions", headers=headers, timeout=5)
            print(f"Call {i+1}: Status {response.status_code}")
        except Exception as e:
            print(f"Call {i+1}: Failed - {e}")

    # Demo: Database operations
    db = connect(config.db_url)
    db.ensure_schema()

    # Add sample matches with dates
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
            pass  # Already exists

    # List and display matches
    all_matches = list_matches(db)
    print("Matches:")
    for match in all_matches:
        print(f"{match.home_team} {match.home_score}-{match.away_score} {match.away_team}")

    # Compute and display table
    points_table = table(all_matches)
    print("\nPoints Table:")
    for team, points in sorted(points_table.items(), key=lambda x: x[1], reverse=True):
        print(f"{team}: {points} points")


if __name__ == "__main__":
    main()
