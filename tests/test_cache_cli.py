from soccer_bot.cache import DiskCache
from soccer_bot.scoring import choose_best_picks, score_fixture


def test_disk_cache_roundtrip(tmp_path) -> None:
    cache = DiskCache(str(tmp_path))
    payload = {"ok": True}
    cache.set("2026-01-21", "sample", payload)
    loaded = cache.get("2026-01-21", "sample")
    assert loaded == payload
    assert cache.stats.hits == 1
    assert cache.stats.misses == 0


def test_score_fixture_outputs_markets() -> None:
    standings = {
        "team a": {"points": 10, "played": 5, "position": 2, "goals_for": 9, "goals_against": 4},
        "team b": {"points": 6, "played": 5, "position": 6, "goals_for": 5, "goals_against": 8},
    }
    odds = {
        "markets": {
            "1x2": {"home": 1.9, "draw": 3.2, "away": 4.1},
            "over_under": {"over_2.5": 2.0, "under_2.5": 1.8},
            "btts": {"yes": 1.9, "no": 1.9},
            "double_chance": {"1x": 1.3, "x2": 1.7, "12": 1.5},
        }
    }
    picks = score_fixture(
        fixture_id="1",
        home_team="Team A",
        away_team="Team B",
        standings=standings,
        odds=odds,
        stats={"home_corners": 4, "away_corners": 5, "home_cards": 2, "away_cards": 1},
        events=[{"type": "goal"}],
    )
    markets = {pick.market for pick in picks}
    assert {"1X2", "Over/Under 2.5", "BTTS", "Dupla esely"} <= markets
    best = choose_best_picks(picks, limit=2)
    assert len(best) == 2
