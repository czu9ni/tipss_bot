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
    api_config = ApiConfig(base_url=config.api_base_url, api_key=config.api_key)
    client = SoccerApiClient(api_config)
    print("API client initialized.")
    print(f"ODDS_API_KEY loaded: {config.odds_api_key[:4]}...")
    print(f"FOOTBALL_DATA_TOKEN loaded: {config.football_data_token[:4]}...")
    print(f"WEATHER_API_KEY loaded: {config.weather_api_key[:4]}...")
    print(f"NEWS_API_KEY loaded: {config.news_api_key[:4]}...")

    # Actual API call demo: Fetch competitions from Football Data API
    try:
        headers = {"X-Auth-Token": config.football_data_token}
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

    # Add sample matches
    matches = [
        ("Team A", "Team B", 2, 1),
        ("Team A", "Team C", 1, 1),
        ("Team B", "Team C", 0, 3),
    ]
    for home, away, h_score, a_score in matches:
        match = Match(home_team=home, away_team=away, home_score=h_score, away_score=a_score)
        add_match(db, match)

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