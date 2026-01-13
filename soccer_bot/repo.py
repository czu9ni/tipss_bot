from dataclasses import dataclass

from soccer_bot.db import Database


@dataclass(frozen=True)
class Match:
    home_team: str
    away_team: str
    home_score: int
    away_score: int


def add_match(db: Database, match: Match) -> int:
    cursor = db.connection.execute(
        "INSERT INTO matches (home_team, away_team, home_score, away_score) VALUES (?, ?, ?, ?)",
        (match.home_team, match.away_team, match.home_score, match.away_score),
    )
    db.connection.commit()
    return int(cursor.lastrowid)


def list_matches(db: Database) -> list[Match]:
    cursor = db.connection.execute(
        "SELECT home_team, away_team, home_score, away_score FROM matches ORDER BY id"
    )
    return [Match(*row) for row in cursor.fetchall()]