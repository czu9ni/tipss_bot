import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from soccer_bot.config import load_config
from soccer_bot.db import connect


TARGET_ODDS = 2.0
COMBO_MIN = 1.85
COMBO_MAX = 2.15


@dataclass(frozen=True)
class Pick:
    commence_time: str
    home_team: str
    away_team: str
    outcome: str
    odds: float
    score: float
    result: str


def _load_weights() -> dict[str, float]:
    defaults = {
        "odds_distance": 0.45,
        "implied_prob": 0.2,
        "news": 0.2,
        "weather": 0.05,
        "stats": 0.05,
        "form": 0.03,
        "table": 0.02,
    }
    path = os.path.join("data", "weights.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                if isinstance(data, dict):
                    defaults.update({k: float(v) for k, v in data.items() if k in defaults})
        except Exception:
            pass
    return defaults


def _odds_distance(odds: float) -> float:
    return max(0.0, 1.0 - abs(odds - TARGET_ODDS) / 0.3)


def _score_outcome(odds: float) -> float:
    weights = _load_weights()
    implied_prob = 1 / odds
    odds_score = _odds_distance(odds)
    return odds_score * weights["odds_distance"] + implied_prob * weights["implied_prob"]


def _fetch_sports(api_key: str) -> list[str]:
    response = requests.get(
        "https://api.the-odds-api.com/v4/sports",
        params={"apiKey": api_key},
        timeout=10,
    )
    if response.status_code != 200:
        return []
    data = response.json()
    return [sport["key"] for sport in data if sport.get("active") and sport.get("key", "").startswith("soccer_")]


def _fetch_odds(api_key: str, sport_key: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
        params={"apiKey": api_key, "regions": "eu", "markets": "h2h"},
        timeout=10,
    )
    if response.status_code != 200:
        return []
    return response.json()


def _fetch_scores(api_key: str, sport_key: str, days_from: int) -> list[dict[str, Any]]:
    days_from = min(max(days_from, 1), 3)
    response = requests.get(
        f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores",
        params={"apiKey": api_key, "daysFrom": days_from},
        timeout=10,
    )
    if response.status_code != 200:
        return []
    return response.json()


def _snapshot_odds(db_url: str, api_key: str, max_sports: int) -> int:
    db = connect(db_url)
    db.ensure_schema()
    sports = _fetch_sports(api_key)
    if max_sports > 0:
        sports = sports[:max_sports]
    captured_at = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for key in sports:
        for match in _fetch_odds(api_key, key):
            home_team = match.get("home_team")
            away_team = match.get("away_team")
            commence_time = match.get("commence_time")
            if not home_team or not away_team or not commence_time:
                continue
            outcomes = []
            for bookmaker in match.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market.get("key") == "h2h":
                        outcomes = market.get("outcomes", [])
                        break
                if outcomes:
                    break
            for outcome in outcomes:
                name = outcome.get("name")
                price = outcome.get("price")
                if name and isinstance(price, (int, float)):
                    db.connection.execute(
                        """
                        INSERT OR IGNORE INTO odds_snapshots
                        (sport_key, commence_time, home_team, away_team, outcome, odds, captured_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (key, commence_time, home_team, away_team, name, float(price), captured_at),
                    )
                    inserted += 1
    db.connection.commit()
    return inserted


def _result_from_scores(match: dict[str, Any]) -> str | None:
    scores = match.get("scores") or []
    if len(scores) < 2:
        return None
    home = scores[0].get("score")
    away = scores[1].get("score")
    if home is None or away is None:
        return None
    try:
        home_val = int(home)
        away_val = int(away)
    except Exception:
        return None
    if home_val > away_val:
        return "H"
    if home_val < away_val:
        return "A"
    return "D"


def _load_snapshot_picks(db_url: str, days_from: int, api_key: str, max_sports: int) -> list[Pick]:
    db = connect(db_url)
    db.ensure_schema()
    sports = _fetch_sports(api_key)
    if max_sports > 0:
        sports = sports[:max_sports]
    result_map: dict[tuple[str, str, str], str] = {}
    for key in sports:
        for match in _fetch_scores(api_key, key, days_from):
            commence_time = match.get("commence_time")
            home_team = match.get("home_team")
            away_team = match.get("away_team")
            completed = match.get("completed")
            if not completed or not commence_time or not home_team or not away_team:
                continue
            result = _result_from_scores(match)
            if result:
                result_map[(commence_time, home_team, away_team)] = result

    cursor = db.connection.execute(
        """
        SELECT commence_time, home_team, away_team, outcome, odds
        FROM odds_snapshots
        """
    )
    rows = cursor.fetchall()
    picks: list[Pick] = []
    for commence_time, home_team, away_team, outcome, odds in rows:
        result = result_map.get((commence_time, home_team, away_team))
        if not result:
            continue
        score = _score_outcome(float(odds))
        mapped = "D" if outcome == "Draw" else ("H" if outcome == home_team else "A")
        picks.append(
            Pick(
                commence_time=commence_time,
                home_team=home_team,
                away_team=away_team,
                outcome=mapped,
                odds=float(odds),
                score=score,
                result=result,
            )
        )
    return picks


def _group_by_date(picks: list[Pick]) -> dict[str, list[Pick]]:
    grouped: dict[str, list[Pick]] = {}
    for pick in picks:
        grouped.setdefault(pick.commence_time[:10], []).append(pick)
    return grouped


def _combo_score(pick_a: Pick, pick_b: Pick) -> tuple[float, float]:
    combined_odds = pick_a.odds * pick_b.odds
    base = (pick_a.score + pick_b.score) / 2
    distance = max(0.0, 1.0 - abs(combined_odds - TARGET_ODDS) / 0.3)
    combo_score = base * 0.8 + distance * 0.2
    return combo_score, combined_odds


def _best_combo_for_date(picks: list[Pick]) -> tuple[Pick, Pick] | None:
    best = None
    for i in range(len(picks)):
        for j in range(i + 1, len(picks)):
            score, combined_odds = _combo_score(picks[i], picks[j])
            if not (COMBO_MIN <= combined_odds <= COMBO_MAX):
                continue
            if best is None or score > best[0]:
                best = (score, picks[i], picks[j])
    if not best:
        return None
    return (best[1], best[2])


def _roi_for_picks(picks: list[Pick]) -> tuple[float, float, int, int]:
    stake = 1.0
    profit = 0.0
    wins = 0
    for pick in picks:
        if pick.outcome == pick.result:
            profit += (pick.odds - 1.0) * stake
            wins += 1
        else:
            profit -= stake
    total = len(picks)
    roi = (profit / (total * stake)) if total else 0.0
    return roi, profit, wins, total


def _roi_for_combos(combos: list[tuple[Pick, Pick]]) -> tuple[float, float, int, int]:
    stake = 1.0
    profit = 0.0
    wins = 0
    for pick_a, pick_b in combos:
        if pick_a.outcome == pick_a.result and pick_b.outcome == pick_b.result:
            profit += (pick_a.odds * pick_b.odds - 1.0) * stake
            wins += 1
        else:
            profit -= stake
    total = len(combos)
    roi = (profit / (total * stake)) if total else 0.0
    return roi, profit, wins, total


def backtest_live(days_from: int, max_sports: int) -> None:
    config = load_config()
    inserted = _snapshot_odds(config.db_url, config.odds_api_key, max_sports)
    print(f"Odds snapshot mentve: {inserted} sor")

    picks = _load_snapshot_picks(config.db_url, days_from, config.odds_api_key, max_sports)
    roi, profit, wins, total = _roi_for_picks(picks)

    grouped = _group_by_date(picks)
    combos: list[tuple[Pick, Pick]] = []
    for _, items in grouped.items():
        combo = _best_combo_for_date(sorted(items, key=lambda p: p.score, reverse=True))
        if combo:
            combos.append(combo)

    combo_roi, combo_profit, combo_wins, combo_total = _roi_for_combos(combos)

    print("Egyszeru tipp backtest (elo odds + eredmenyek)")
    print(f"Meccsek: {total}, Talalat: {wins}, ROI: {roi:.3f}, Profit: {profit:.2f}")
    print("Kombi backtest (2 meccs, 2.00 koruli)")
    print(
        f"Kombik: {combo_total}, Talalat: {combo_wins}, ROI: {combo_roi:.3f}, Profit: {combo_profit:.2f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Elo backtest odds snapshot es eredmenyek alapjan.")
    parser.add_argument("--days", type=int, default=3, help="Hany napra visszamenoleg keres eredmenyeket.")
    parser.add_argument("--max-sports", type=int, default=0, help="Sportok max szama (0 = nincs limit).")
    args = parser.parse_args()
    backtest_live(args.days, args.max_sports)


if __name__ == "__main__":
    main()
