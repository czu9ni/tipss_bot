import sqlite3
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class Database:
    connection: sqlite3.Connection

    def ensure_schema(self) -> None:
        global _SCHEMA_READY
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
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_picks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                commence_time TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                market_key TEXT NOT NULL,
                outcome TEXT NOT NULL,
                line REAL,
                odds REAL NOT NULL,
                score REAL NOT NULL,
                risk TEXT NOT NULL,
                status TEXT NOT NULL,
                settled_at TEXT,
                result TEXT
            )
            """
        )
        self._ensure_columns(
            "saved_picks",
            {
                "source": "TEXT",
                "has_odds": "INTEGER",
                "day_key": "TEXT",
            },
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS saved_picks_status
            ON saved_picks (status, commence_time)
            """
        )
        self.connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS saved_picks_unique
            ON saved_picks (sport_key, commence_time, home_team, away_team, market_key, outcome, source)
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS saved_picks_day
            ON saved_picks (day_key)
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS cached_picks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pick_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                day_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                valid INTEGER NOT NULL,
                issues TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS pick_runs_created_at
            ON pick_runs (created_at DESC)
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS pick_runs_day_key
            ON pick_runs (day_key)
            """
        )
        if not _SCHEMA_READY:
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
            _SCHEMA_READY = True
        self.connection.commit()

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}
        for name, col_type in columns.items():
            if name in existing:
                continue
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


def connect(db_url: str) -> Database:
    parsed = urlparse(db_url)
    if parsed.scheme != "sqlite":
        raise ValueError("Only sqlite URLs are supported")
    path = parsed.path if parsed.path else ":memory:"
    if path.startswith("/"):
        path = path[1:]
    connection = sqlite3.connect(path)
    return Database(connection)


_SCHEMA_READY = False
