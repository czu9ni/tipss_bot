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
    model_prob: float | None = None
    implied_prob: float | None = None
    ev: float | None = None
    value: float | None = None


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
    return max(0.0, min(1.0, model_prob * 0.75 + odds_prob * 0.25))


def _data_coverage(
    *,
    standings_ok: bool,
    odds_ok: bool,
    stats_ok: bool,
    events_ok: bool,
) -> float:
    coverage = 0.5
    if standings_ok:
        coverage += 0.15
    if stats_ok:
        coverage += 0.15
    if events_ok:
        coverage += 0.1
    if odds_ok:
        coverage += 0.1
    return max(0.5, min(0.95, coverage))


def _calibrate_prob(prob: float, coverage: float) -> float:
    # Shrink toward 0.5 when data coverage is weak.
    return max(0.02, min(0.98, 0.5 + (prob - 0.5) * coverage))


def _ev_score(ev: float | None) -> float:
    if ev is None:
        return 0.0
    # Map a small negative EV to 0 and strong positive EV toward 1.
    return max(0.0, min(1.0, (ev + 0.05) / 0.25))


def _select_score(model_prob: float, implied_prob: float | None, odds: float | None) -> tuple[float, float | None]:
    if not isinstance(odds, (int, float)) or odds <= 1.0:
        return model_prob, None
    blended = _blend_prob(model_prob, implied_prob)
    ev = blended * float(odds) - 1.0
    score = max(0.0, min(1.0, blended * 0.65 + _ev_score(ev) * 0.35))
    return score, ev


def _value_edge(model_prob: float, implied_prob: float | None) -> float:
    if implied_prob is None:
        return 0.0
    return model_prob - implied_prob


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
    if stats:
        # Blend in richer team stats when available to avoid a single weak equation.
        if isinstance(stats.get("home_gf_avg"), (int, float)):
            home_gf = (home_gf * 0.6) + (float(stats["home_gf_avg"]) * 0.4)
        if isinstance(stats.get("away_gf_avg"), (int, float)):
            away_gf = (away_gf * 0.6) + (float(stats["away_gf_avg"]) * 0.4)
        if isinstance(stats.get("home_ga_avg"), (int, float)):
            home_ga = (home_ga * 0.6) + (float(stats["home_ga_avg"]) * 0.4)
        if isinstance(stats.get("away_ga_avg"), (int, float)):
            away_ga = (away_ga * 0.6) + (float(stats["away_ga_avg"]) * 0.4)
    expected_home = max(0.4, home_gf * (1.1 - 0.2 * away_ga))
    expected_away = max(0.4, away_gf * (1.1 - 0.2 * home_ga))
    goals_total = expected_home + expected_away
    if stats:
        over_vals = [
            float(val)
            for val in (stats.get("home_over25_rate"), stats.get("away_over25_rate"))
            if isinstance(val, (int, float))
        ]
        if over_vals:
            over_rate = sum(over_vals) / len(over_vals)
            # Stronger nudge: observed over-rate should materially move totals.
            goals_total *= 0.8 + (over_rate * 0.8)
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
    odds_ok = bool(odds and odds.get("markets"))
    if odds_ok:
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

    standings_ok = bool(home_row or away_row)
    stats_ok = bool(stats)
    events_ok = bool(events)
    coverage = _data_coverage(
        standings_ok=standings_ok,
        odds_ok=odds_ok,
        stats_ok=stats_ok,
        events_ok=events_ok,
    )
    markets = (odds or {}).get("markets", {}) if odds else {}
    picks: list[Pick] = []

    odds_1x2 = markets.get("1x2", {})
    home_odd = odds_1x2.get("home")
    draw_odd = odds_1x2.get("draw")
    away_odd = odds_1x2.get("away")
    home_imp = _implied_prob(home_odd)
    draw_imp = _implied_prob(draw_odd)
    away_imp = _implied_prob(away_odd)
    home_base = max(0.05, 0.45 + standings_score + form_score)
    draw_base = max(0.05, 0.2 - abs(standings_score))
    away_base = max(0.05, 0.45 - standings_score - form_score)
    total_base = home_base + draw_base + away_base
    home_model = _calibrate_prob(min(0.9, max(0.05, home_base / total_base)), coverage)
    draw_model = _calibrate_prob(min(0.8, max(0.05, draw_base / total_base)), coverage)
    away_model = _calibrate_prob(min(0.9, max(0.05, away_base / total_base)), coverage)
    home_score, home_ev = _select_score(home_model, home_imp, home_odd)
    draw_score, draw_ev = _select_score(draw_model, draw_imp, draw_odd)
    away_score, away_ev = _select_score(away_model, away_imp, away_odd)
    outcome_1x2 = "Hazai gyozelem" if home_score >= max(draw_score, away_score) else ("Dontetlen" if draw_score >= away_score else "Vendeg gyozelem")
    if outcome_1x2 == "Hazai gyozelem":
        pick_model, pick_imp, pick_ev = home_model, home_imp, home_ev
    elif outcome_1x2 == "Dontetlen":
        pick_model, pick_imp, pick_ev = draw_model, draw_imp, draw_ev
    else:
        pick_model, pick_imp, pick_ev = away_model, away_imp, away_ev
    picks.append(
        Pick(
            fixture_id=fixture_id,
            home_team=home_team,
            away_team=away_team,
            market="1X2",
            outcome=outcome_1x2,
            score=max(home_score, draw_score, away_score),
            breakdown=breakdown,
            explanation_hu=f"{base_explanation} Legvaloszinubb 1X2: {outcome_1x2}.",
            model_prob=pick_model,
            implied_prob=pick_imp,
            ev=pick_ev,
            value=_value_edge(pick_model, pick_imp),
        )
    )

    ou_market = markets.get("over_under", {})
    over_odd = ou_market.get("over_2.5")
    under_odd = ou_market.get("under_2.5")
    over_imp = _implied_prob(over_odd)
    under_imp = _implied_prob(under_odd)
    over_model_base = _poisson_over25_prob(goals_total)
    if stats:
        over_vals = [
            float(val)
            for val in (stats.get("home_over25_rate"), stats.get("away_over25_rate"))
            if isinstance(val, (int, float))
        ]
        if over_vals:
            over_rate = sum(over_vals) / len(over_vals)
            over_model_base = (over_model_base * 0.5) + (over_rate * 0.5)
    over_model = _calibrate_prob(over_model_base, coverage)
    under_model = _calibrate_prob(1 - over_model_base, coverage)
    over_score, over_ev = _select_score(over_model, over_imp, over_odd)
    under_score, under_ev = _select_score(under_model, under_imp, under_odd)
    outcome_ou = "Over 2.5" if over_score >= under_score else "Under 2.5"
    if outcome_ou.startswith("Over"):
        pick_model, pick_imp, pick_ev = over_model, over_imp, over_ev
    else:
        pick_model, pick_imp, pick_ev = under_model, under_imp, under_ev
    picks.append(
        Pick(
            fixture_id=fixture_id,
            home_team=home_team,
            away_team=away_team,
            market="Over/Under 2.5",
            outcome=outcome_ou,
            score=max(over_score, under_score),
            breakdown=breakdown,
            explanation_hu=f"{base_explanation} Golok: {outcome_ou} valoszinubb.",
            model_prob=pick_model,
            implied_prob=pick_imp,
            ev=pick_ev,
            value=_value_edge(pick_model, pick_imp),
        )
    )

    btts_market = markets.get("btts", {})
    btts_yes_odd = btts_market.get("yes")
    btts_no_odd = btts_market.get("no")
    btts_yes_imp = _implied_prob(btts_yes_odd)
    btts_no_imp = _implied_prob(btts_no_odd)
    btts_yes_base = min(0.9, expected_home * expected_away / 2.0)
    if stats:
        btts_vals = [
            float(val)
            for val in (stats.get("home_btts_rate"), stats.get("away_btts_rate"))
            if isinstance(val, (int, float))
        ]
        if btts_vals:
            btts_rate = sum(btts_vals) / len(btts_vals)
            btts_yes_base = (btts_yes_base * 0.7) + (btts_rate * 0.3)
    btts_yes_model = _calibrate_prob(btts_yes_base, coverage)
    btts_no_model = _calibrate_prob(1 - btts_yes_base, coverage)
    btts_yes_score, btts_yes_ev = _select_score(btts_yes_model, btts_yes_imp, btts_yes_odd)
    btts_no_score, btts_no_ev = _select_score(btts_no_model, btts_no_imp, btts_no_odd)
    outcome_btts = "GG - Igen" if btts_yes_score >= btts_no_score else "GG - Nem"
    if outcome_btts.endswith("Igen"):
        pick_model, pick_imp, pick_ev = btts_yes_model, btts_yes_imp, btts_yes_ev
    else:
        pick_model, pick_imp, pick_ev = btts_no_model, btts_no_imp, btts_no_ev
    picks.append(
        Pick(
            fixture_id=fixture_id,
            home_team=home_team,
            away_team=away_team,
            market="BTTS",
            outcome=outcome_btts,
            score=max(btts_yes_score, btts_no_score),
            breakdown=breakdown,
            explanation_hu=f"{base_explanation} GG becsles: {outcome_btts}.",
            model_prob=pick_model,
            implied_prob=pick_imp,
            ev=pick_ev,
            value=_value_edge(pick_model, pick_imp),
        )
    )

    dc_market = markets.get("double_chance", {})
    dc_1x_odd = dc_market.get("1x")
    dc_x2_odd = dc_market.get("x2")
    dc_12_odd = dc_market.get("12")
    dc_1x_imp = _implied_prob(dc_1x_odd)
    dc_x2_imp = _implied_prob(dc_x2_odd)
    dc_12_imp = _implied_prob(dc_12_odd)
    dc_1x_model = _calibrate_prob(min(0.95, home_model + draw_model), coverage)
    dc_x2_model = _calibrate_prob(min(0.95, away_model + draw_model), coverage)
    dc_12_model = _calibrate_prob(min(0.95, home_model + away_model), coverage)
    dc_1x_score, dc_1x_ev = _select_score(dc_1x_model, dc_1x_imp, dc_1x_odd)
    dc_x2_score, dc_x2_ev = _select_score(dc_x2_model, dc_x2_imp, dc_x2_odd)
    dc_12_score, dc_12_ev = _select_score(dc_12_model, dc_12_imp, dc_12_odd)
    outcome_dc = "1X" if dc_1x_score >= max(dc_x2_score, dc_12_score) else ("X2" if dc_x2_score >= dc_12_score else "12")
    if outcome_dc == "1X":
        pick_model, pick_imp, pick_ev = dc_1x_model, dc_1x_imp, dc_1x_ev
    elif outcome_dc == "X2":
        pick_model, pick_imp, pick_ev = dc_x2_model, dc_x2_imp, dc_x2_ev
    else:
        pick_model, pick_imp, pick_ev = dc_12_model, dc_12_imp, dc_12_ev
    picks.append(
        Pick(
            fixture_id=fixture_id,
            home_team=home_team,
            away_team=away_team,
            market="Dupla esely",
            outcome=outcome_dc,
            score=max(dc_1x_score, dc_x2_score, dc_12_score),
            breakdown=breakdown,
            explanation_hu=f"{base_explanation} Dupla esely: {outcome_dc}.",
            model_prob=pick_model,
            implied_prob=pick_imp,
            ev=pick_ev,
            value=_value_edge(pick_model, pick_imp),
        )
    )

    return picks


def choose_best_picks(picks: Iterable[Pick], limit: int = 2) -> list[Pick]:
    ordered = sorted(picks, key=lambda p: (-p.score, p.fixture_id))
    return ordered[:limit]
