from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict

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
        "INSERT INTO matches (home_team, away_team, home_score, away_score, date) VALUES (?, ?, ?, ?, ?)",
        (match.home_team, match.away_team, match.home_score, match.away_score, match.date),
    )
    db.connection.commit()
    return int(cursor.lastrowid)


def list_matches(db: Database) -> list[Match]:
    cursor = db.connection.execute(
        "SELECT home_team, away_team, home_score, away_score, date FROM matches ORDER BY id"
    )
    return [Match(*row) for row in cursor.fetchall()]


def get_team_stats(db: Database, team: str) -> Dict[str, float]:
    """Calculate team statistics: win rate, avg goals scored/conceded."""
    matches = list_matches(db)
    home_matches = [m for m in matches if m.home_team == team]
    away_matches = [m for m in matches if m.away_team == team]
    total_matches = len(home_matches) + len(away_matches)
    if total_matches == 0:
        return {"win_rate": 0.0, "avg_goals_scored": 0.0, "avg_goals_conceded": 0.0}

    wins = 0
    goals_scored = 0
    goals_conceded = 0

    for m in home_matches:
        if m.home_score > m.away_score:
            wins += 1
        goals_scored += m.home_score
        goals_conceded += m.away_score

    for m in away_matches:
        if m.away_score > m.home_score:
            wins += 1
        goals_scored += m.away_score
        goals_conceded += m.home_score

    return {
        "win_rate": wins / total_matches,
        "avg_goals_scored": goals_scored / total_matches,
        "avg_goals_conceded": goals_conceded / total_matches,
    }