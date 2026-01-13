from soccer_bot.api import ApiConfig, SoccerApiClient
from soccer_bot.config import load_config
from soccer_bot.db import connect
from soccer_bot.logging import configure_logging
from soccer_bot.repo import add_match, list_matches
from soccer_bot.scoring import table


def main() -> None:
    config = load_config()
    configure_logging(config.log_level)
    api_config = ApiConfig(base_url=config.api_base_url, api_key=config.api_key)
    client = SoccerApiClient(api_config)
    print("API client initialized.")

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
        add_match(db, (home, away, h_score, a_score))

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