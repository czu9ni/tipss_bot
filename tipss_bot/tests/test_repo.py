from soccer_bot import db, repo


def test_add_and_list_matches() -> None:
    database = db.connect("sqlite:///:memory:")
    database.ensure_schema()
    match = repo.Match(home_team="A", away_team="B", home_score=2, away_score=1, date="2023-01-01")
    match_id = repo.add_match(database, match)
    assert match_id == 1
    assert repo.list_matches(database) == [match]
    database.connection.close()
