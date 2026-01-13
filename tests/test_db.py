import sqlite3

from soccer_bot import db


def test_connect_and_schema() -> None:
    database = db.connect("sqlite:///:memory:")
    database.ensure_schema()
    cursor = database.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='matches'"
    )
    assert cursor.fetchone() == ("matches",)
    database.connection.close()