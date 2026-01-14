import sqlite3
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class Database:
    connection: sqlite3.Connection

    def ensure_schema(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                home_score INTEGER NOT NULL,
                away_score INTEGER NOT NULL,
                date TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            DELETE FROM matches
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM matches
                GROUP BY home_team, away_team, date
            )
            """
        )
        self.connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS matches_unique
            ON matches (home_team, away_team, date)
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS odds_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport_key TEXT NOT NULL,
                commence_time TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                outcome TEXT NOT NULL,
                odds REAL NOT NULL,
                captured_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS odds_snapshots_unique
            ON odds_snapshots (sport_key, commence_time, home_team, away_team, outcome)
            """
        )
        self.connection.commit()


def connect(db_url: str) -> Database:
    parsed = urlparse(db_url)
    if parsed.scheme != "sqlite":
        raise ValueError("Only sqlite URLs are supported")
    path = parsed.path if parsed.path else ":memory:"
    if path.startswith("/"):
        path = path[1:]
    connection = sqlite3.connect(path)
    return Database(connection)
