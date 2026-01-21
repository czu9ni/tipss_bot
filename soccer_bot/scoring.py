from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import exp
from typing import Iterable

from soccer_bot.repo import Match
from soccer_bot.utils import normalize_team


def match_points(match: Match) -> dict[str, int]:
    if match.home_score > match.away_score:
        return {match.home_team: 3, match.away_team: 0}
    if match.home_score < match.away_score:
        return {match.home_team: 0, match.away_team: 3}
    return {match.home_team: 1, match.away_team: 1}


def table(matches: list[Match]) -> dict[str, int]:
    points: dict[str, int] = defaultdict(int)
    for match in matches:
        for team, score in match_points(match).items():
            points[team] += score
    return dict(points)


@dataclass
class ScoreBreakdown:
    base: float
    standings: float
    form: float
    goals: float
    odds: float
    stats: float
    events: float
    total: float


@dataclass
class Pick:
    fixture_id: str
    home_team: str
    away_team: str
    market: str
    outcome: str
    score: float
    breakdown: ScoreBreakdown
    explanation_hu: str


def _ppg(points: int | None, played: int | None) -> float:
    if not isinstance(points, int) or not isinstance(played, int) or played <= 0:
        return 0.5
    return min(1.0, max(0.0, points / (played * 3)))


def _goals_per_match(goals: int | None, played: int | None) -> float:
    if not isinstance(goals, int) or not isinstance(played, int) or played <= 0:
        return 1.2
    return max(0.2, goals / played)


def _poisson_over25_prob(lambda_total: float) -> float:
    # P(X >= 3) for Poisson
    p0 = exp(-lambda_total)
    p1 = p0 * lambda_total
    p2 = p1 * lambda_total / 2
    return max(0.0, min(1.0, 1 - (p0 + p1 + p2)))


def _implied_prob(odd: float | None) -> float | None:
    if not isinstance(odd, (int, float)) or odd <= 1.0:
        return None
    return min(0.95, max(0.05, 1.0 / float(odd)))


def _blend_prob(model_prob: float, odds_prob: float | None) -> float:
    if odds_prob is None:
        return model_prob
    return max(0.0, min(1.0, model_prob * 0.7 + odds_prob * 0.3))


def _fmt(value: int | float | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def score_fixture(
    *,
    fixture_id: str,
    home_team: str,
    away_team: str,
    standings: dict[str, dict],
    odds: dict | None,
    stats: dict | None,
    events: list[dict] | None,
) -> list[Pick]:
    base = 0.4
    home_key = normalize_team(home_team)
    away_key = normalize_team(away_team)
    home_row = standings.get(home_key, {})
    away_row = standings.get(away_key, {})
    home_points = home_row.get("points")
    away_points = away_row.get("points")
    home_played = home_row.get("played")
    away_played = away_row.get("played")
    home_pos = home_row.get("position")
    away_pos = away_row.get("position")
    standings_score = 0.0
    if isinstance(home_points, int) and isinstance(away_points, int):
        diff = home_points - away_points
        standings_score = max(-0.2, min(0.2, diff / 40))
    elif isinstance(home_pos, int) and isinstance(away_pos, int):
        diff = away_pos - home_pos
        standings_score = max(-0.2, min(0.2, diff / 40))

    home_ppg = _ppg(home_points, home_played)
    away_ppg = _ppg(away_points, away_played)
    form_score = max(-0.2, min(0.2, home_ppg - away_ppg))

    home_gf = _goals_per_match(home_row.get("goals_for"), home_played)
    away_gf = _goals_per_match(away_row.get("goals_for"), away_played)
    home_ga = _goals_per_match(home_row.get("goals_against"), home_played)
    away_ga = _goals_per_match(away_row.get("goals_against"), away_played)
    expected_home = max(0.4, home_gf * (1.1 - 0.2 * away_ga))
    expected_away = max(0.4, away_gf * (1.1 - 0.2 * home_ga))
    goals_total = expected_home + expected_away
    goals_score = max(-0.1, min(0.1, (goals_total - 2.4) / 6))

    stats_score = 0.0
    if stats:
        home_corners = stats.get("home_corners")
        away_corners = stats.get("away_corners")
        home_cards = stats.get("home_cards")
        away_cards = stats.get("away_cards")
        corners = (home_corners or 0) + (away_corners or 0)
        cards = (home_cards or 0) + (away_cards or 0)
        stats_score = min(0.1, (corners * 0.02 + cards * 0.01))

    events_score = 0.0
    if events:
        events_score = min(0.05, len(events) / 200)

    odds_score = 0.0
    if odds and odds.get("markets"):
        odds_score = 0.08

    total = base + standings_score + form_score + goals_score + stats_score + odds_score + events_score
    total = max(0.0, min(1.0, total))

    home_pos = home_row.get("position")
    away_pos = away_row.get("position")
    home_pts = home_row.get("points")
    away_pts = away_row.get("points")
    corners_total = None
    cards_total = None
    if stats:
        home_corners = stats.get("home_corners")
        away_corners = stats.get("away_corners")
        home_cards = stats.get("home_cards")
        away_cards = stats.get("away_cards")
        if home_corners is not None or away_corners is not None:
            corners_total = (home_corners or 0) + (away_corners or 0)
        if home_cards is not None or away_cards is not None:
            cards_total = (home_cards or 0) + (away_cards or 0)
    events_count = len(events) if events else 0
    odds_note = "Odds: elerheto" if odds and odds.get("markets") else "Odds: n/a"
    base_explanation = (
        f"Tabella: {home_team} ({_fmt(home_pos)}. hely, {_fmt(home_pts)} pont) vs "
        f"{away_team} ({_fmt(away_pos)}. hely, {_fmt(away_pts)} pont). "
        f"Forma (PPG): {home_ppg:.2f} vs {away_ppg:.2f}. "
        f"Gol atlag: {home_gf:.2f}/{away_gf:.2f}. "
        f"Szoglet/Lap: {_fmt(corners_total)}/{_fmt(cards_total)}. "
        f"Esemenyek szama: {events_count}. {odds_note}."
    )

    breakdown = ScoreBreakdown(
        base=base,
        standings=standings_score,
        form=form_score,
        goals=goals_score,
        odds=odds_score,
        stats=stats_score,
        events=events_score,
        total=total,
    )

    markets = (odds or {}).get("markets", {}) if odds else {}
    picks: list[Pick] = []

    home_prob = _blend_prob(0.45 + standings_score + form_score, _implied_prob(markets.get("1x2", {}).get("home")))
    draw_prob = _blend_prob(0.2 - abs(standings_score), _implied_prob(markets.get("1x2", {}).get("draw")))
    away_prob = _blend_prob(0.45 - standings_score - form_score, _implied_prob(markets.get("1x2", {}).get("away")))
    outcome_1x2 = "Hazai gyozelem" if home_prob >= max(draw_prob, away_prob) else ("Döntetlen" if draw_prob >= away_prob else "Vendeg gyozelem")
    picks.append(
        Pick(
            fixture_id=fixture_id,
            home_team=home_team,
            away_team=away_team,
            market="1X2",
            outcome=outcome_1x2,
            score=total * 0.75 + max(home_prob, draw_prob, away_prob) * 0.25,
            breakdown=breakdown,
            explanation_hu=f"{base_explanation} Legvaloszinubb 1X2: {outcome_1x2}.",
        )
    )

    over_prob = _blend_prob(_poisson_over25_prob(goals_total), _implied_prob(markets.get("over_under", {}).get("over_2.5")))
    under_prob = _blend_prob(1 - _poisson_over25_prob(goals_total), _implied_prob(markets.get("over_under", {}).get("under_2.5")))
    outcome_ou = "Over 2.5" if over_prob >= under_prob else "Under 2.5"
    picks.append(
        Pick(
            fixture_id=fixture_id,
            home_team=home_team,
            away_team=away_team,
            market="Over/Under 2.5",
            outcome=outcome_ou,
            score=total * 0.7 + max(over_prob, under_prob) * 0.3,
            breakdown=breakdown,
            explanation_hu=f"{base_explanation} Golok: {outcome_ou} valoszinubb.",
        )
    )

    btts_yes = _blend_prob(min(0.9, expected_home * expected_away / 2.0), _implied_prob(markets.get("btts", {}).get("yes")))
    btts_no = _blend_prob(1 - btts_yes, _implied_prob(markets.get("btts", {}).get("no")))
    outcome_btts = "GG - Igen" if btts_yes >= btts_no else "GG - Nem"
    picks.append(
        Pick(
            fixture_id=fixture_id,
            home_team=home_team,
            away_team=away_team,
            market="BTTS",
            outcome=outcome_btts,
            score=total * 0.7 + max(btts_yes, btts_no) * 0.3,
            breakdown=breakdown,
            explanation_hu=f"{base_explanation} GG becsles: {outcome_btts}.",
        )
    )

    dc_market = markets.get("double_chance", {})
    dc_1x = _blend_prob(min(0.95, home_prob + draw_prob), _implied_prob(dc_market.get("1x")))
    dc_x2 = _blend_prob(min(0.95, away_prob + draw_prob), _implied_prob(dc_market.get("x2")))
    dc_12 = _blend_prob(min(0.95, home_prob + away_prob), _implied_prob(dc_market.get("12")))
    outcome_dc = "1X" if dc_1x >= max(dc_x2, dc_12) else ("X2" if dc_x2 >= dc_12 else "12")
    picks.append(
        Pick(
            fixture_id=fixture_id,
            home_team=home_team,
            away_team=away_team,
            market="Dupla esely",
            outcome=outcome_dc,
            score=total * 0.7 + max(dc_1x, dc_x2, dc_12) * 0.3,
            breakdown=breakdown,
            explanation_hu=f"{base_explanation} Dupla esely: {outcome_dc}.",
        )
    )

    return picks


def choose_best_picks(picks: Iterable[Pick], limit: int = 2) -> list[Pick]:
    ordered = sorted(picks, key=lambda p: (-p.score, p.fixture_id))
    return ordered[:limit]
