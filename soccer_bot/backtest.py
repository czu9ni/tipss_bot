import argparse
import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime


TARGET_ODDS = 2.0
COMBO_MIN = 1.85
COMBO_MAX = 2.15


@dataclass(frozen=True)
class MatchRow:
    date: str
    home_team: str
    away_team: str
    home_odds: float
    draw_odds: float
    away_odds: float
    result: str  # H/D/A
    elo_strength: float = 0.5
    form_strength: float = 0.5
    market_prob: float = 0.0
    injury_index: float = 0.0
    weather_factor: float = 0.0


@dataclass(frozen=True)
class Pick:
    date: str
    home_team: str
    away_team: str
    outcome: str  # H/D/A
    odds: float
    score: float
    result: str


def _load_weights() -> dict[str, float]:
    defaults = {
        "elo": 0.5,
        "form": 0.2,
        "market": 0.15,
        "injury": 0.1,
        "weather": 0.05,
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


def _score_outcome(row: MatchRow, odds: float) -> float:
    weights = _load_weights()
    implied_prob = 1 / odds
    market_diff = max(0.0, row.market_prob - implied_prob)
    return (
        row.elo_strength * weights["elo"]
        + row.form_strength * weights["form"]
        + market_diff * weights["market"]
        + (1.0 - row.injury_index) * weights["injury"]
        + (1.0 - row.weather_factor) * weights["weather"]
    )


def _read_csv(path: str) -> list[MatchRow]:
    rows: list[MatchRow] = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for item in reader:
            rows.append(
                MatchRow(
                    date=item["date"],
                    home_team=item["home_team"],
                    away_team=item["away_team"],
                    home_odds=float(item["home_odds"]),
                    draw_odds=float(item["draw_odds"]),
                    away_odds=float(item["away_odds"]),
                    result=item["result"].upper(),
                    elo_strength=float(item.get("elo_strength", 0.5) or 0.5),
                    form_strength=float(item.get("form_strength", 0.5) or 0.5),
                    market_prob=float(item.get("market_prob", 0.0) or 0.0),
                    injury_index=float(item.get("injury_index", 0.0) or 0.0),
                    weather_factor=float(item.get("weather_factor", 0.0) or 0.0),
                )
            )
    return rows


def _best_pick(row: MatchRow) -> Pick:
    scores = {
        "H": _score_outcome(row, row.home_odds),
        "D": _score_outcome(row, row.draw_odds),
        "A": _score_outcome(row, row.away_odds),
    }
    outcome = max(scores.items(), key=lambda item: item[1])[0]
    odds = {
        "H": row.home_odds,
        "D": row.draw_odds,
        "A": row.away_odds,
    }[outcome]
    return Pick(
        date=row.date,
        home_team=row.home_team,
        away_team=row.away_team,
        outcome=outcome,
        odds=odds,
        score=scores[outcome],
        result=row.result,
    )


def _group_by_date(picks: list[Pick]) -> dict[str, list[Pick]]:
    grouped: dict[str, list[Pick]] = {}
    for pick in picks:
        grouped.setdefault(pick.date, []).append(pick)
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


def backtest(path: str) -> None:
    rows = _read_csv(path)
    picks = [_best_pick(row) for row in rows]
    roi, profit, wins, total = _roi_for_picks(picks)

    grouped = _group_by_date(picks)
    combos: list[tuple[Pick, Pick]] = []
    for _, items in grouped.items():
        combo = _best_combo_for_date(sorted(items, key=lambda p: p.score, reverse=True))
        if combo:
            combos.append(combo)

    combo_roi, combo_profit, combo_wins, combo_total = _roi_for_combos(combos)

    print("Egyszeru tipp backtest")
    print(f"Meccsek: {total}, Talalat: {wins}, ROI: {roi:.3f}, Profit: {profit:.2f}")
    print("Kombi backtest (2 meccs, 2.00 koruli)")
    print(
        f"Kombik: {combo_total}, Talalat: {combo_wins}, ROI: {combo_roi:.3f}, Profit: {combo_profit:.2f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest a sulyozott modellhez.")
    parser.add_argument("--csv", required=True, help="CSV fajl eleresi ut (kolumnak: date,home_team,away_team,home_odds,draw_odds,away_odds,result)")
    args = parser.parse_args()
    backtest(args.csv)


if __name__ == "__main__":
    main()
