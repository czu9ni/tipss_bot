from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


DATA_ROOT = Path("data") / "externals"
STATSBOMB_ROOT = DATA_ROOT / "statsbomb-open-data" / "data"
DATASETS_ROOT = DATA_ROOT / "datasets-football-datasets" / "datasets"
INTERNATIONAL_RESULTS = DATA_ROOT / "international-results" / "results.csv"

_STATS_CACHE: dict[str, object] = {}


def _normalize(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


@dataclass(frozen=True)
class MatchRow:
    date: str
    home: str
    away: str
    home_goals: int
    away_goals: int
    source: str
    match_id: Optional[int] = None


@dataclass(frozen=True)
class TeamSummary:
    team: str
    matches: list[MatchRow]
    win_rate: float
    goals_for_avg: float
    corners_avg: Optional[float]
    cards_avg: Optional[float]
    source: str


def _parse_date(value: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt)
        except Exception:
            continue
    return None


def _load_statsbomb_matches() -> Dict[str, List[MatchRow]]:
    cache = _STATS_CACHE.get("statsbomb")
    if isinstance(cache, dict):
        return cache
    index: Dict[str, List[MatchRow]] = {}
    matches_dir = STATSBOMB_ROOT / "matches"
    if not matches_dir.exists():
        _STATS_CACHE["statsbomb"] = index
        return index
    for path in matches_dir.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for match in data:
            home = match.get("home_team", {}).get("home_team_name")
            away = match.get("away_team", {}).get("away_team_name")
            if not home or not away:
                continue
            home_goals = match.get("home_score")
            away_goals = match.get("away_score")
            date_val = match.get("match_date")
            match_id = match.get("match_id")
            if home_goals is None or away_goals is None or not date_val:
                continue
            row = MatchRow(
                date=date_val,
                home=home,
                away=away,
                home_goals=int(home_goals),
                away_goals=int(away_goals),
                source="statsbomb",
                match_id=int(match_id) if match_id else None,
            )
            for team in (home, away):
                key = _normalize(team)
                index.setdefault(key, []).append(row)
    for key, rows in index.items():
        rows.sort(key=lambda r: r.date, reverse=True)
        index[key] = rows[:20]
    _STATS_CACHE["statsbomb"] = index
    return index


def _load_datasets_matches() -> Dict[str, List[MatchRow]]:
    cache = _STATS_CACHE.get("datasets")
    if isinstance(cache, dict):
        return cache
    index: Dict[str, List[MatchRow]] = {}
    if not DATASETS_ROOT.exists():
        _STATS_CACHE["datasets"] = index
        return index
    for league_dir in DATASETS_ROOT.iterdir():
        if not league_dir.is_dir():
            continue
        for path in league_dir.glob("season-*.csv"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    reader = csv.DictReader(handle)
                    for row in reader:
                        home = row.get("HomeTeam") or row.get("home_team") or row.get("Home")
                        away = row.get("AwayTeam") or row.get("away_team") or row.get("Away")
                        date_val = row.get("Date") or row.get("date")
                        home_goals = row.get("FTHG") or row.get("home_score") or row.get("HG")
                        away_goals = row.get("FTAG") or row.get("away_score") or row.get("AG")
                        if not home or not away or not date_val:
                            continue
                        try:
                            hg = int(home_goals)
                            ag = int(away_goals)
                        except Exception:
                            continue
                        parsed = _parse_date(date_val)
                        date_str = parsed.strftime("%Y-%m-%d") if parsed else date_val
                        row_item = MatchRow(
                            date=date_str,
                            home=home,
                            away=away,
                            home_goals=hg,
                            away_goals=ag,
                            source="datasets",
                        )
                        for team in (home, away):
                            key = _normalize(team)
                            index.setdefault(key, []).append(row_item)
            except Exception:
                continue
    for key, rows in index.items():
        rows.sort(key=lambda r: r.date, reverse=True)
        index[key] = rows[:20]
    _STATS_CACHE["datasets"] = index
    return index


def _load_international_matches() -> Dict[str, List[MatchRow]]:
    cache = _STATS_CACHE.get("international")
    if isinstance(cache, dict):
        return cache
    index: Dict[str, List[MatchRow]] = {}
    if not INTERNATIONAL_RESULTS.exists():
        _STATS_CACHE["international"] = index
        return index
    try:
        with INTERNATIONAL_RESULTS.open("r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                home = row.get("home_team")
                away = row.get("away_team")
                date_val = row.get("date")
                home_goals = row.get("home_score")
                away_goals = row.get("away_score")
                if not home or not away or not date_val:
                    continue
                try:
                    hg = int(home_goals)
                    ag = int(away_goals)
                except Exception:
                    continue
                row_item = MatchRow(
                    date=date_val,
                    home=home,
                    away=away,
                    home_goals=hg,
                    away_goals=ag,
                    source="international",
                )
                for team in (home, away):
                    key = _normalize(team)
                    index.setdefault(key, []).append(row_item)
    except Exception:
        pass
    for key, rows in index.items():
        rows.sort(key=lambda r: r.date, reverse=True)
        index[key] = rows[:20]
    _STATS_CACHE["international"] = index
    return index


def _load_statsbomb_events(match_id: int) -> list[dict]:
    path = STATSBOMB_ROOT / "events" / f"{match_id}.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _corners_cards_from_events(match_id: int, team_name: str) -> tuple[Optional[int], Optional[int]]:
    events = _load_statsbomb_events(match_id)
    if not events:
        return (None, None)
    corners = 0
    cards = 0
    target = _normalize(team_name)
    for event in events:
        team = event.get("team", {}).get("name")
        if not team or _normalize(team) != target:
            continue
        if event.get("type", {}).get("name") == "Pass":
            pass_type = event.get("pass", {}).get("type", {}).get("name")
            if pass_type == "Corner":
                corners += 1
        card_name = (
            event.get("foul_committed", {}).get("card", {}).get("name")
            or event.get("card", {}).get("name")
        )
        if card_name in {"Yellow Card", "Second Yellow", "Red Card"}:
            cards += 1
    return (corners, cards)


def _match_rows_for_team(team_name: str) -> list[MatchRow]:
    key = _normalize(team_name)
    statsbomb = _load_statsbomb_matches()
    if key in statsbomb:
        return statsbomb[key]
    international = _load_international_matches()
    if key in international:
        return international[key]
    datasets = _load_datasets_matches()
    return datasets.get(key, [])


def build_team_summary(team_name: str, limit: int = 5) -> TeamSummary:
    rows = _match_rows_for_team(team_name)[:limit]
    wins = 0
    goals_for = 0
    corners_vals: list[int] = []
    cards_vals: list[int] = []
    for row in rows:
        is_home = _normalize(row.home) == _normalize(team_name)
        gf = row.home_goals if is_home else row.away_goals
        ga = row.away_goals if is_home else row.home_goals
        goals_for += gf
        if gf > ga:
            wins += 1
        if row.source == "statsbomb" and row.match_id:
            corners, cards = _corners_cards_from_events(row.match_id, team_name)
            if corners is not None:
                corners_vals.append(corners)
            if cards is not None:
                cards_vals.append(cards)
    total = max(1, len(rows))
    corners_avg = sum(corners_vals) / len(corners_vals) if corners_vals else None
    cards_avg = sum(cards_vals) / len(cards_vals) if cards_vals else None
    source = rows[0].source if rows else "n/a"
    return TeamSummary(
        team=team_name,
        matches=rows,
        win_rate=wins / total,
        goals_for_avg=goals_for / total,
        corners_avg=corners_avg,
        cards_avg=cards_avg,
        source=source,
    )
