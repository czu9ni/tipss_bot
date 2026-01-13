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
                away_score INTEGER NOT NULL
            )
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