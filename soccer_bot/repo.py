from dataclasses import dataclass

from soccer_bot.db import Database


@dataclass(frozen=True)
class Match:
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    date: str  # ISO format date


def add_match(db: Database, match: Match) -> int:
    cursor = db.connection.execute(
        "INSERT OR IGNORE INTO matches (home_team, away_team, home_score, away_score, date) VALUES (?, ?, ?, ?, ?)",
        (match.home_team, match.away_team, match.home_score, match.away_score, match.date),
    )
    db.connection.commit()
    return int(cursor.lastrowid)


def list_matches(db: Database) -> list[Match]:
    cursor = db.connection.execute(
        "SELECT home_team, away_team, home_score, away_score, date FROM matches ORDER BY id"
    )
    return [Match(*row) for row in cursor.fetchall()]


def get_team_stats(db: Database, team: str, matches: list[Match] | None = None) -> dict[str, float]:
    """Calculate team statistics: win rate, avg goals scored/conceded."""
    total_matches = 0
    wins = 0
    goals_scored = 0
    goals_conceded = 0
    for match in (matches if matches is not None else list_matches(db)):
        is_home = match.home_team == team
        is_away = match.away_team == team
        if not (is_home or is_away):
            continue
        total_matches += 1
        if is_home:
            if match.home_score > match.away_score:
                wins += 1
            goals_scored += match.home_score
            goals_conceded += match.away_score
        else:
            if match.away_score > match.home_score:
                wins += 1
            goals_scored += match.away_score
            goals_conceded += match.home_score

    if total_matches == 0:
        return {"win_rate": 0.0, "avg_goals_scored": 0.0, "avg_goals_conceded": 0.0}

    return {
        "win_rate": wins / total_matches,
        "avg_goals_scored": goals_scored / total_matches,
        "avg_goals_conceded": goals_conceded / total_matches,
    }
