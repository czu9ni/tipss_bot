from soccer_bot.repo import Match
from soccer_bot.scoring import match_points, table


def test_match_points_win_draw_loss() -> None:
    assert match_points(Match("A", "B", 1, 0)) == {"A": 3, "B": 0}
    assert match_points(Match("A", "B", 0, 2)) == {"A": 0, "B": 3}
    assert match_points(Match("A", "B", 1, 1)) == {"A": 1, "B": 1}


def test_table_accumulates_points() -> None:
    matches = [
        Match("A", "B", 1, 0),
        Match("A", "C", 1, 1),
        Match("B", "C", 0, 2),
    ]
    assert table(matches) == {"A": 4, "B": 0, "C": 4}