from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from soccer_bot.cache import DiskCache
from soccer_bot.db import connect
from soccer_bot.providers import build_odds_provider, build_stats_provider
from soccer_bot.providers.base import Fixture, MatchStats
from soccer_bot.providers.odds_therundown import TheRundownClient
from soccer_bot.scoring import Pick, ScoreBreakdown, choose_best_picks, score_fixture
from soccer_bot.utils import (
    build_http_client,
    configure_logger,
    get_logger,
    load_env_file,
    normalize_team,
    normalize_team_name,
)


def _parse_date(value: str | None, tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    if value:
        return value
    return datetime.now(tz).strftime("%Y-%m-%d")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Soccer Bot CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("fetch", "Fetch and cache API data"),
        ("pick", "Pick the best two matches"),
        ("report", "Print report from cached picks"),
        ("run", "Fetch + pick + report"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--date", help="YYYY-MM-DD")
        cmd.add_argument("--no-cache", action="store_true", help="Force refresh (ignore cache)")
        cmd.add_argument("--log-level", default="INFO", help="Logging level")
    save_cmd = sub.add_parser("save", help="Save a manual pick into the database")
    save_cmd.add_argument("--sport-key", required=True)
    save_cmd.add_argument("--commence-time", required=True, help="ISO time, e.g. 2026-01-26T20:00:00Z")
    save_cmd.add_argument("--home-team", required=True)
    save_cmd.add_argument("--away-team", required=True)
    save_cmd.add_argument("--market-key", required=True, help="h2h|totals|btts|double_chance")
    save_cmd.add_argument("--outcome", required=True)
    save_cmd.add_argument("--line", type=float)
    save_cmd.add_argument("--odds", type=float, default=0.0)
    save_cmd.add_argument("--score", type=float, default=0.0)
    save_cmd.add_argument("--risk", default="yellow")
    settle_cmd = sub.add_parser("settle", help="Manually settle a saved pick")
    settle_cmd.add_argument("--id", type=int, required=True)
    settle_cmd.add_argument("--result", required=True, choices=["win", "lose", "push"])
    odds_parser = sub.add_parser("odds", help="TheRundown odds tools")
    odds_sub = odds_parser.add_subparsers(dest="odds_command", required=True)
    for name in ("openers", "closing", "delta"):
        cmd = odds_sub.add_parser(name, help=f"Fetch {name} snapshot")
        cmd.add_argument("--date", required=True, help="YYYY-MM-DD")
        cmd.add_argument("--no-cache", action="store_true", help="Force refresh (ignore cache)")
        cmd.add_argument("--log-level", default="INFO", help="Logging level")
    lines_cmd = odds_sub.add_parser("lines", help="Fetch historical lines by line_id")
    lines_cmd.add_argument("--event-id", required=True, help="Line ID for historical odds")
    lines_cmd.add_argument("--date", help="YYYY-MM-DD")
    lines_cmd.add_argument("--no-cache", action="store_true", help="Force refresh (ignore cache)")
    lines_cmd.add_argument("--log-level", default="INFO", help="Logging level")
    return parser


def _load_env() -> None:
    load_env_file(".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _market_key_from_pick(value: str) -> str:
    val = value.strip().lower()
    if "1x2" in val:
        return "h2h"
    if "over/under" in val or "over" in val or "under" in val:
        return "totals"
    if "btts" in val:
        return "btts"
    if "dupla" in val:
        return "double_chance"
    return val


def _passes_value_filters(pick: Pick) -> bool:
    implied = pick.implied_prob
    if not isinstance(implied, (int, float)) or implied <= 0:
        return True
    odds = 1.0 / implied if implied > 0 else None
    odds_min = float(_env("ODDS_MIN", "1.6") or 0)
    odds_max = float(_env("ODDS_MAX", "0") or 0)
    if odds is not None:
        if odds_min > 0 and odds < odds_min:
            return False
        if odds_max > 0 and odds > odds_max:
            return False
    allowlist = {item.strip() for item in _env("MARKET_ALLOWLIST", "").split(",") if item.strip()}
    if allowlist and _market_key_from_pick(pick.market) not in allowlist:
        return False
    min_value = float(_env("MIN_VALUE_EDGE", "0.02") or 0)
    min_ev = float(_env("MIN_EV", "0.0") or 0)
    min_model = float(_env("MIN_MODEL_PROB", "0.35") or 0)
    if isinstance(pick.model_prob, (int, float)) and pick.model_prob < min_model:
        return False
    if isinstance(pick.value, (int, float)) and pick.value < min_value:
        return False
    if isinstance(pick.ev, (int, float)) and pick.ev < min_ev:
        return False
    return True


def _combo_score(pick_a: Pick, pick_b: Pick, target: float) -> tuple[float, float]:
    odds_a = 1.0 / pick_a.implied_prob if pick_a.implied_prob else 0.0
    odds_b = 1.0 / pick_b.implied_prob if pick_b.implied_prob else 0.0
    combined = odds_a * odds_b
    base = (pick_a.score + pick_b.score) / 2.0
    distance = max(0.0, 1.0 - abs(combined - target) / 0.3)
    score = base * 0.8 + distance * 0.2
    return score, combined


def _best_combo(picks: list[Pick]) -> tuple[list[Pick], float] | None:
    target = float(_env("COMBO_TARGET", "2.0") or 2.0)
    min_val = float(_env("COMBO_MIN", "1.85") or 1.85)
    max_val = float(_env("COMBO_MAX", "2.15") or 2.15)
    eligible = [p for p in picks if isinstance(p.implied_prob, (int, float)) and p.implied_prob > 0]
    eligible = sorted(eligible, key=lambda p: p.score, reverse=True)[:30]
    best = None
    nearest = None
    for i in range(len(eligible)):
        for j in range(i + 1, len(eligible)):
            score, combined = _combo_score(eligible[i], eligible[j], target)
            if not (min_val <= combined <= max_val):
                distance = abs(combined - target)
                if nearest is None or distance < nearest[0]:
                    nearest = (distance, combined, score, eligible[i], eligible[j])
                continue
            if best is None or score > best[0]:
                best = (score, combined, [eligible[i], eligible[j]])
    if best:
        return best[2], best[1]
    if nearest:
        _, combined, _, left, right = nearest
        return [left, right], combined
    return None


def _require_stats_key(provider: str, api_key: str) -> None:
    if not api_key:
        raise RuntimeError(f"Missing API key for stats provider: {provider}")


def _build_stats_provider(provider: str, client) -> Any:
    if provider == "api_football":
        key = _env("API_FOOTBALL_KEY")
        _require_stats_key(provider, key)
        return build_stats_provider(provider, api_key=key, client=client)
    if provider == "sportradar":
        key = _env("SPORTRADAR_API_KEY")
        base = _env("SPORTRADAR_API_BASE")
        _require_stats_key(provider, key)
        if not base:
            raise RuntimeError("Missing SPORTRADAR_API_BASE")
        return build_stats_provider(provider, api_key=key, api_base=base, client=client)
    raise RuntimeError(f"Unknown stats provider: {provider}")


def _build_odds_provider(provider: str, client) -> Any | None:
    if provider == "the_odds_api":
        key = _env("THE_ODDS_API_KEY", _env("ODDS_API_KEY"))
        if not key:
            return None
        return build_odds_provider(provider, api_key=key, client=client)
    if provider == "api_football":
        key = _env("API_FOOTBALL_KEY")
        if not key:
            return None
        return build_odds_provider(provider, api_key=key, client=client)
    return None


def _therundown_enabled() -> bool:
    return bool(_env("RAPIDAPI_KEY") and _env("THERUNDOWN_BASE_URL") and _env("RAPIDAPI_HOST"))


def _therundown_client(client) -> TheRundownClient:
    return TheRundownClient(
        base_url=_env("THERUNDOWN_BASE_URL"),
        api_key=_env("RAPIDAPI_KEY"),
        api_host=_env("RAPIDAPI_HOST"),
        client=client,
    )


def _therundown_sport_ids() -> list[str]:
    raw = _env("THERUNDOWN_SPORT_IDS", _env("THERUNDOWN_SPORT_ID_SOCCER", "10,11,12,13,14,15,17"))
    parts = [item.strip() for item in raw.split(",") if item.strip()]
    return parts or ["10", "11", "12", "13", "14", "15", "17"]


def _iso_date(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    return value.split("T", 1)[0]


def _event_teams(event: dict) -> tuple[str, str] | None:
    teams = event.get("teams") or []
    home = next((t for t in teams if t.get("is_home")), None)
    away = next((t for t in teams if t.get("is_away")), None)
    if home and away:
        return str(home.get("name") or ""), str(away.get("name") or "")
    return None


def _event_line_id(event: dict) -> str | None:
    lines = event.get("lines") or {}
    for _, item in lines.items():
        line_id = item.get("line_id")
        if line_id:
            return str(line_id)
    return None


def _american_to_decimal(value: float | int | None) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    val = float(value)
    if val == 0:
        return None
    if abs(val) >= 100:
        if val > 0:
            return round((val / 100.0) + 1.0, 3)
        return round((100.0 / abs(val)) + 1.0, 3)
    if val > 1:
        return round(val, 3)
    return 1.01


def _markets_from_event(event: dict) -> dict[str, dict]:
    markets: dict[str, dict] = {}
    lines = event.get("lines") or {}
    for _, item in lines.items():
        moneyline = item.get("moneyline") or {}
        totals = item.get("total") or item.get("totals") or {}
        spread = item.get("spread") or {}
        ml_home = _american_to_decimal(moneyline.get("moneyline_home"))
        ml_away = _american_to_decimal(moneyline.get("moneyline_away"))
        ml_draw = _american_to_decimal(moneyline.get("moneyline_draw"))
        if ml_home or ml_away or ml_draw:
            markets["1x2"] = {"home": ml_home, "away": ml_away, "draw": ml_draw}
        total_over = _american_to_decimal(totals.get("total_over_money"))
        total_under = _american_to_decimal(totals.get("total_under_money"))
        if total_over or total_under:
            markets["over_under"] = {"over_2.5": total_over, "under_2.5": total_under}
        spread_home = _american_to_decimal(spread.get("point_spread_home_money"))
        spread_away = _american_to_decimal(spread.get("point_spread_away_money"))
        if spread_home or spread_away:
            markets["spread"] = {"home": spread_home, "away": spread_away}
        if markets:
            break
    return markets


def _markets_from_moneyline(data: dict) -> dict[str, dict]:
    ml = (data.get("moneyline_periods") or {}).get("period_full_game") or []
    if not ml:
        return {}
    row = ml[0]
    return {
        "1x2": {
            "home": _american_to_decimal(row.get("moneyline_home")),
            "away": _american_to_decimal(row.get("moneyline_away")),
            "draw": _american_to_decimal(row.get("moneyline_draw")),
        }
    }


def _markets_from_totals(data: dict) -> dict[str, dict]:
    totals = (data.get("total_periods") or {}).get("period_full_game") or []
    if not totals:
        return {}
    row = totals[0]
    return {
        "over_under": {
            "over_2.5": _american_to_decimal(row.get("total_over_money")),
            "under_2.5": _american_to_decimal(row.get("total_under_money")),
        }
    }


def _markets_from_spread(data: dict) -> dict[str, dict]:
    spread = (data.get("spread_periods") or {}).get("period_full_game") or []
    if not spread:
        return {}
    row = spread[0]
    return {
        "spread": {
            "home": _american_to_decimal(row.get("point_spread_home_money")),
            "away": _american_to_decimal(row.get("point_spread_away_money")),
        }
    }


def _serialize_fixtures(fixtures: list[Fixture]) -> list[dict]:
    return [asdict(item) for item in fixtures]


def _deserialize_fixtures(data: list[dict]) -> list[Fixture]:
    return [Fixture(**item) for item in data]


def _serialize_stats(stats: MatchStats) -> dict:
    return asdict(stats)


def _deserialize_stats(data: dict) -> MatchStats:
    return MatchStats(**data)


def _odds_key(home: str, away: str) -> str:
    return f"{normalize_team(home)}|{normalize_team(away)}"


def _event_key(date_str: str, home: str, away: str) -> str:
    return f"{date_str}|{normalize_team_name(home)}|{normalize_team_name(away)}"


def _token_set(name: str) -> set[str]:
    return {tok for tok in normalize_team_name(name).split(" ") if tok}


def _similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _find_therundown_event(
    date_str: str, home: str, away: str, candidates: list[dict], threshold: float = 0.55
) -> dict:
    home_tokens = _token_set(home)
    away_tokens = _token_set(away)
    best = None
    best_score = 0.0
    for item in candidates:
        if item.get("date") != date_str:
            continue
        cand_home = set(item.get("home_tokens") or [])
        cand_away = set(item.get("away_tokens") or [])
        score_same = (_similarity(home_tokens, cand_home) + _similarity(away_tokens, cand_away)) / 2.0
        score_swap = (_similarity(home_tokens, cand_away) + _similarity(away_tokens, cand_home)) / 2.0
        score = max(score_same, score_swap)
        if score > best_score:
            best_score = score
            best = item
    if best and best_score >= threshold:
        return best
    return {}


def _load_cached(cache: DiskCache, date_str: str, name: str, no_cache: bool) -> Any | None:
    if no_cache:
        return None
    return cache.get(date_str, name)


def _fetch_data(date_str: str, cache: DiskCache, no_cache: bool, client) -> dict[str, Any]:
    logger = get_logger("cli")
    cache.assert_single_dir(date_str)

    fixtures_raw = _load_cached(cache, date_str, "fixtures", no_cache)
    fixtures: list[Fixture] = []
    stats_provider = None
    provider_name = _env("STATS_PROVIDER", "api_football")
    if fixtures_raw is None:
        primary = _env("STATS_PROVIDER", "api_football")
        if no_cache:
            cached_fixtures = cache.get(date_str, "fixtures")
            if cached_fixtures is not None:
                fixtures = _deserialize_fixtures(cached_fixtures)
                cached_provider = cache.get(date_str, "stats_provider") or {}
                provider_name = str(cached_provider.get("name") or primary)
                cache.set(date_str, "stats_provider", {"name": provider_name})
        if not fixtures:
            stats_provider = _build_stats_provider(primary, client)
            try:
                fixtures = stats_provider.fetch_fixtures(date_str)
            except RuntimeError as exc:
                logger.warning("Primary stats provider failed: %s", exc)
                cached_fixtures = cache.get(date_str, "fixtures")
                if cached_fixtures is not None:
                    fixtures = _deserialize_fixtures(cached_fixtures)
                    cached_provider = cache.get(date_str, "stats_provider") or {}
                    provider_name = str(cached_provider.get("name") or primary)
                    cache.set(date_str, "stats_provider", {"name": provider_name})
                else:
                    raise
            if not fixtures:
                fallback_provider = _env("STATS_PROVIDER_FALLBACK", "sportradar")
                if fallback_provider != primary:
                    stats_provider = _build_stats_provider(fallback_provider, client)
                    try:
                        fixtures = stats_provider.fetch_fixtures(date_str)
                        provider_name = fallback_provider
                    except RuntimeError as exc:
                        logger.warning("Fallback stats provider failed: %s", exc)
                        cached_fixtures = cache.get(date_str, "fixtures")
                        if cached_fixtures is not None:
                            fixtures = _deserialize_fixtures(cached_fixtures)
                            cached_provider = cache.get(date_str, "stats_provider") or {}
                            provider_name = str(cached_provider.get("name") or fallback_provider)
                            cache.set(date_str, "stats_provider", {"name": provider_name})
                        else:
                            raise
                else:
                    provider_name = primary
            else:
                provider_name = primary
            cache.set(date_str, "fixtures", _serialize_fixtures(fixtures))
            cache.set(date_str, "stats_provider", {"name": provider_name})
    else:
        fixtures = _deserialize_fixtures(fixtures_raw)

    therundown_map: dict[str, dict] = {}
    therundown_candidates: list[dict] = []
    therundown_events: list[dict] = []
    if _therundown_enabled():
        td_client = _therundown_client(client)
        therundown_ok = True
        cached_td_dates = cache.get(date_str, "therundown_dates") if no_cache else None
        cached_td_events = cache.get(date_str, "therundown_events") if no_cache else None
        if no_cache and cached_td_events is not None:
            dates_cache = cached_td_dates or {}
            events_cache = cached_td_events
            therundown_ok = False
        else:
            dates_cache = _load_cached(cache, date_str, "therundown_dates", no_cache)
            if dates_cache is None:
                dates_cache = {}
                for sport_id in _therundown_sport_ids():
                    try:
                        dates_cache[sport_id] = td_client.dates_with_odds(sport_id)
                    except RuntimeError as exc:
                        logger.warning("TheRundown dates failed: %s", exc)
                        therundown_ok = False
                        break
                if not therundown_ok:
                    cached_dates = cache.get(date_str, "therundown_dates")
                    if cached_dates is not None:
                        dates_cache = cached_dates
                        therundown_ok = True
                cache.set(date_str, "therundown_dates", dates_cache)

            events_cache = _load_cached(cache, date_str, "therundown_events", no_cache)
            if events_cache is None:
                events_cache = []
                if therundown_ok:
                    for sport_id in _therundown_sport_ids():
                        try:
                            events_cache.extend(td_client.events_for_date(sport_id, date_str))
                        except RuntimeError as exc:
                            logger.warning("TheRundown events failed: %s", exc)
                            therundown_ok = False
                            break
                if not therundown_ok:
                    cached_events = cache.get(date_str, "therundown_events")
                    if cached_events is not None:
                        events_cache = cached_events
                        therundown_ok = True
                cache.set(date_str, "therundown_events", events_cache)
        therundown_events = list(events_cache)

        for event in therundown_events:
            event_date = _iso_date(event.get("event_date"), date_str)
            teams = _event_teams(event)
            if not teams:
                continue
            home, away = teams
            line_id = _event_line_id(event)
            if not line_id:
                continue
            payload = {
                "event_id": event.get("event_id"),
                "line_id": line_id,
                "home": home,
                "away": away,
                "date": event_date,
                "markets": _markets_from_event(event),
            }
            therundown_map[_event_key(event_date, home, away)] = payload
            if event_date != date_str:
                therundown_map[_event_key(date_str, home, away)] = payload
            home_tokens = set(_token_set(home))
            away_tokens = set(_token_set(away))
            for item in event.get("teams_normalized") or []:
                name = str(item.get("name") or "")
                if not name:
                    continue
                if item.get("is_home"):
                    home_tokens |= _token_set(name)
                if item.get("is_away"):
                    away_tokens |= _token_set(name)
            payload["home_tokens"] = sorted(home_tokens)
            payload["away_tokens"] = sorted(away_tokens)
            therundown_candidates.append(payload)
        cache.set(date_str, "therundown_event_map", therundown_map)

        if no_cache and therundown_ok:
            last_id = "1"
            cached_delta = _load_cached(cache, date_str, "therundown_delta", no_cache=False)
            if isinstance(cached_delta, dict):
                last_id = str(cached_delta.get("last_id") or cached_delta.get("meta", {}).get("last_id") or last_id)
            try:
                delta_data = td_client.delta_changed_events(last_id)
            except RuntimeError as exc:
                delta_data = {"error": str(exc), "last_id": last_id}
            cache.set(date_str, "therundown_delta", delta_data)

        if therundown_map:
            filtered = []
            for fixture in fixtures:
                fixture_date = _iso_date(fixture.commence_time, date_str)
                key = _event_key(fixture_date, fixture.home_team, fixture.away_team)
                match = therundown_map.get(key)
                if not match:
                    match = _find_therundown_event(fixture_date, fixture.home_team, fixture.away_team, therundown_candidates)
                if match:
                    therundown_map[key] = match
                    filtered.append(fixture)
            if filtered:
                fixtures = filtered
        else:
            logger.warning("TheRundown configured but no odds-eligible events matched.")

    odds_raw = _load_cached(cache, date_str, "odds", no_cache)
    odds_map: dict[str, dict] = {}
    if odds_raw is None:
        if not _therundown_enabled() or not therundown_map:
            odds_provider = _build_odds_provider(_env("ODDS_PROVIDER", "the_odds_api"), client)
            if odds_provider is None:
                logger.warning("ODDS provider missing or API key not set. Continuing without odds.")
            else:
                try:
                    odds_map = odds_provider.fetch_odds(date_str)
                    cache.set(date_str, "odds", odds_map)
                except RuntimeError as exc:
                    logger.warning("ODDS fetch failed: %s", exc)
                    if not no_cache:
                        cache.set(date_str, "odds", {})
    else:
        odds_map = odds_raw

    return {
        "fixtures": fixtures,
        "odds": odds_map,
        "stats_provider": provider_name,
        "therundown": therundown_map,
        "therundown_events": therundown_events,
    }


def _standing_index(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        team = row.get("team")
        if not team:
            continue
        out[normalize_team(team)] = row
    return out


def _pick_best(
    fixtures: list[Fixture],
    standings: dict[str, list[dict]],
    odds_map: dict[str, dict],
    cache: DiskCache,
    date_str: str,
    no_cache: bool,
    client,
    provider_name: str,
    therundown_map: dict[str, dict],
) -> list[Pick]:
    if len(fixtures) < 2:
        raise RuntimeError("Not enough fixtures to pick 2 matches.")
    logger = get_logger("cli")
    stats_provider = None
    standings_raw = _load_cached(cache, date_str, "standings", no_cache)
    if standings_raw is None and no_cache:
        standings_raw = cache.get(date_str, "standings")
    standings_map: dict[str, list[dict]] = dict(standings_raw or {})

    cached_provider = _load_cached(cache, date_str, "stats_provider", no_cache)
    if isinstance(cached_provider, dict) and cached_provider.get("name"):
        provider_name = str(cached_provider.get("name"))

    def fetch_standings(comp_id: str, season: str) -> list[dict]:
        key = f"{comp_id}:{season}"
        if key in standings_map:
            return standings_map[key]
        if no_cache:
            return []
        if stats_provider is None:
            return []
        try:
            rows = stats_provider.fetch_standings(comp_id, season)
        except RuntimeError as exc:
            logger.warning("Standings fetch failed for %s: %s", key, exc)
            return []
        standings_map[key] = [asdict(row) for row in rows]
        return standings_map[key]

    fixtures_sorted = sorted(fixtures, key=lambda f: (f.commence_time, f.id))
    max_fixtures = int(os.environ.get("MAX_SCORING_FIXTURES", "50"))
    fixtures_limited = fixtures_sorted if max_fixtures <= 0 else fixtures_sorted[:max_fixtures]
    if stats_provider is None:
        stats_provider = _build_stats_provider(provider_name, client)

    pre_scored: list[tuple[Pick, Fixture]] = []
    for fixture in fixtures_limited:
        standings_idx: dict[str, dict] = {}
        if fixture.competition_id and fixture.season:
            comp_rows = fetch_standings(fixture.competition_id, fixture.season)
            standings_idx = _standing_index(comp_rows) if comp_rows else {}
        fixture_date = _iso_date(fixture.commence_time, date_str)
        event_key = _event_key(fixture_date, fixture.home_team, fixture.away_team)
        therundown_event = therundown_map.get(event_key, {})
        odds = {"markets": therundown_event.get("markets", {})} if therundown_event else None
        if not odds or not odds.get("markets"):
            odds = odds_map.get(fixture.id) or odds_map.get(_odds_key(fixture.home_team, fixture.away_team))
        picks = score_fixture(
            fixture_id=fixture.id,
            home_team=fixture.home_team,
            away_team=fixture.away_team,
            standings=standings_idx,
            odds=odds,
            stats=None,
            events=None,
        )
        picks = [item for item in picks if _passes_value_filters(item)]
        best = max(picks, key=lambda p: p.score)
        pre_scored.append((best, fixture))

    if not pre_scored:
        raise RuntimeError("No fixtures available.")

    pre_scored.sort(key=lambda item: (-item[0].score, item[1].id))
    combo_candidates = [item[0] for item in pre_scored]
    combo = _best_combo(combo_candidates)
    if combo:
        combo_picks, _ = combo
        match_ids = {p.fixture_id for p in combo_picks}
        top_two = [item[1] for item in pre_scored if item[1].id in match_ids][:2]
    else:
        top_two = [item[1] for item in pre_scored[:2]]

    events_raw = _load_cached(cache, date_str, "events", no_cache)
    if events_raw is None and no_cache:
        events_raw = cache.get(date_str, "events")
    stats_raw = _load_cached(cache, date_str, "stats", no_cache)
    if stats_raw is None and no_cache:
        stats_raw = cache.get(date_str, "stats")
    events_raw = events_raw or {}
    stats_raw = stats_raw or {}
    events_map: dict[str, list[dict]] = dict(events_raw)
    stats_map: dict[str, dict] = dict(stats_raw)

    for fixture in top_two:
        fixture_date = _iso_date(fixture.commence_time, date_str)
        event_key = _event_key(fixture_date, fixture.home_team, fixture.away_team)
        therundown_event = therundown_map.get(event_key, {})
        if _therundown_enabled() and therundown_event.get("line_id"):
            td_client = _therundown_client(client)
            line_id = therundown_event["line_id"]
            markets = {}
            try:
                markets.update(_markets_from_moneyline(td_client.moneyline(line_id)))
            except RuntimeError as exc:
                logger.warning("TheRundown moneyline failed: %s", exc)
            try:
                markets.update(_markets_from_totals(td_client.totals(line_id)))
            except RuntimeError as exc:
                logger.warning("TheRundown totals failed: %s", exc)
            try:
                markets.update(_markets_from_spread(td_client.spread(line_id)))
            except RuntimeError as exc:
                logger.warning("TheRundown spread failed: %s", exc)
            if markets:
                odds_map[fixture.id] = {"markets": markets}

        if fixture.id not in events_map:
            try:
                events = stats_provider.fetch_match_events(fixture.id)
                events_map[fixture.id] = events
            except RuntimeError as exc:
                cached_events = cache.get(date_str, "events") or {}
                cached = cached_events.get(fixture.id)
                if cached is not None:
                    events_map[fixture.id] = cached
                else:
                    logger.warning("Events fetch failed for %s: %s", fixture.id, exc)
                    events_map[fixture.id] = []
        if fixture.id not in stats_map:
            try:
                stats = stats_provider.fetch_match_stats(fixture.id)
                stats_map[fixture.id] = _serialize_stats(stats)
            except RuntimeError as exc:
                cached_stats = cache.get(date_str, "stats") or {}
                cached = cached_stats.get(fixture.id)
                if cached is not None:
                    stats_map[fixture.id] = cached
                else:
                    logger.warning("Stats fetch failed for %s: %s", fixture.id, exc)
                    stats_map[fixture.id] = _serialize_stats(MatchStats(None, None, None, None))

    cache.set(date_str, "events", events_map)
    cache.set(date_str, "stats", stats_map)
    cache.set(date_str, "standings", standings_map)
    cache.set(date_str, "odds", odds_map)
    if _therundown_enabled():
        cache.set(date_str, "therundown_odds", odds_map)

    final_picks: list[Pick] = []
    for fixture in top_two:
        comp_rows = standings_map.get(f"{fixture.competition_id}:{fixture.season}", [])
        standings_idx = _standing_index(comp_rows)
        odds = odds_map.get(fixture.id) or odds_map.get(_odds_key(fixture.home_team, fixture.away_team))
        stats = _deserialize_stats(stats_map.get(fixture.id)) if fixture.id in stats_map else None
        events = events_map.get(fixture.id)
        picks = score_fixture(
            fixture_id=fixture.id,
            home_team=fixture.home_team,
            away_team=fixture.away_team,
            standings=standings_idx,
            odds=odds,
            stats=asdict(stats) if stats else None,
            events=events,
        )
        picks = [item for item in picks if _passes_value_filters(item)]
        best = max(picks, key=lambda p: p.score)
        final_picks.append(best)

    combo_final = _best_combo(final_picks)
    if combo_final:
        best, _ = combo_final
    else:
        best = choose_best_picks(final_picks, limit=2)
    if len(best) < 2:
        raise RuntimeError("Not enough scored matches to return 2 picks.")
    return best


def _save_picks(cache: DiskCache, date_str: str, picks: list[Pick]) -> None:
    payload = [asdict(pick) for pick in picks]
    cache.set(date_str, "picks", payload)
    db_url = _env("SOCCER_DB_URL")
    if not db_url:
        return
    db = connect(db_url)
    db.ensure_schema()
    now = datetime.utcnow().isoformat(timespec="seconds")
    data = json.dumps(payload, ensure_ascii=False)
    db.connection.execute(
        "INSERT OR REPLACE INTO cached_picks (key, payload, updated_at) VALUES (?, ?, ?)",
        ("cli_daily", data, now),
    )
    for pick in picks:
        implied = pick.implied_prob
        odds_val = 0.0
        if isinstance(implied, (int, float)) and implied > 0:
            odds_val = 1.0 / implied
        has_odds = 1 if odds_val > 1.01 else 0
        source = "odds" if has_odds else "no_odds"
        db.connection.execute(
            """
            INSERT OR IGNORE INTO saved_picks
            (created_at, sport_key, commence_time, home_team, away_team, market_key, outcome, line, odds, score, risk, status, source, has_odds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                "",
                date_str,
                pick.home_team,
                pick.away_team,
                _market_key_from_pick(pick.market),
                pick.outcome,
                None,
                float(odds_val),
                float(pick.score),
                "yellow",
                "pending",
                source,
                has_odds,
            ),
        )
    db.connection.commit()


def _load_picks(cache: DiskCache, date_str: str) -> list[Pick]:
    cached = cache.get(date_str, "picks")
    if not cached:
        return []
    picks: list[Pick] = []
    for item in cached:
        breakdown = item.get("breakdown")
        if isinstance(breakdown, dict):
            item["breakdown"] = ScoreBreakdown(**breakdown)
        picks.append(Pick(**item))
    return picks


def _print_breakdown(pick: Pick) -> None:
    breakdown = pick.breakdown
    print("Komponens | Ertek")
    print(f"Alap | {breakdown.base:.2f}")
    print(f"Tabella | {breakdown.standings:.2f}")
    print(f"Forma | {breakdown.form:.2f}")
    print(f"Gol | {breakdown.goals:.2f}")
    print(f"Statisztika | {breakdown.stats:.2f}")
    print(f"Esemenyek | {breakdown.events:.2f}")
    print(f"Odds | {breakdown.odds:.2f}")
    print(f"Osszesen | {breakdown.total:.2f}")


def _report(picks: list[Pick]) -> None:
    for index, pick in enumerate(picks, start=1):
        print(f"\n#{index} {pick.home_team} vs {pick.away_team}")
        print(f"Piac: {pick.market} | Kimenetel: {pick.outcome} | Pont: {pick.score:.2f}")
        print(f"Magyarazat: {pick.explanation_hu}")
        _print_breakdown(pick)


def _run_odds_command(args, cache: DiskCache, client) -> None:
    logger = get_logger("cli")
    if not _therundown_enabled():
        raise RuntimeError("RAPIDAPI_KEY/THERUNDOWN_BASE_URL/RAPIDAPI_HOST missing")
    td_client = _therundown_client(client)
    if args.odds_command in {"openers", "closing", "delta"}:
        date_str = args.date
        if args.odds_command == "openers":
            data = {}
            for sport_id in _therundown_sport_ids():
                try:
                    data[sport_id] = td_client.openers(sport_id, date_str)
                except RuntimeError as exc:
                    logger.warning("Openers failed: %s", exc)
            cache.set(date_str, "therundown_openers", data)
            print(cache._path(date_str, "therundown_openers"))
            return
        if args.odds_command == "closing":
            data = {}
            for sport_id in _therundown_sport_ids():
                try:
                    data[sport_id] = td_client.closing(sport_id, date_str)
                except RuntimeError as exc:
                    logger.warning("Closing failed: %s", exc)
            cache.set(date_str, "therundown_closing", data)
            print(cache._path(date_str, "therundown_closing"))
            return
        if args.odds_command == "delta":
            last_id = "1"
            cached = _load_cached(cache, date_str, "therundown_delta", args.no_cache)
            if isinstance(cached, dict):
                last_id = str(cached.get("last_id") or cached.get("meta", {}).get("last_id") or last_id)
            try:
                data = td_client.delta_changed_events(last_id)
            except RuntimeError as exc:
                data = {"error": str(exc), "last_id": last_id}
            cache.set(date_str, "therundown_delta", data)
            print(cache._path(date_str, "therundown_delta"))
            return
    if args.odds_command == "lines":
        line_id = args.event_id
        data = td_client.lines_historical(line_id)
        date_str = args.date or datetime.now(ZoneInfo(_env("TIMEZONE", "Europe/Budapest"))).strftime("%Y-%m-%d")
        cache.set(date_str, f"therundown_lines_{line_id}", data)
        print(cache._path(date_str, f"therundown_lines_{line_id}"))
        return
    raise RuntimeError(f"Unknown odds command: {args.odds_command}")


def main() -> None:
    _load_env()
    parser = _build_parser()
    args = parser.parse_args()
    configure_logger(args.log_level)
    logger = get_logger("cli")
    client = build_http_client(logger)
    tz = _env("TIMEZONE", "Europe/Budapest")
    date_str = _parse_date(args.date, tz)
    cache_dir = _env("CACHE_DIR", os.path.join("data", "cache"))
    cache = DiskCache(cache_dir)

    data: dict[str, Any] = {}
    if args.command in {"fetch", "run"}:
        data = _fetch_data(date_str, cache, args.no_cache, client)
    if args.command == "odds":
        _run_odds_command(args, cache, client)
        print(f"\nAPI_CALLS_TOTAL={client.calls} CACHE_HITS={cache.stats.hits} CACHE_MISSES={cache.stats.misses}")
        return
    if args.command == "save":
        db_url = _env("SOCCER_DB_URL")
        if not db_url:
            raise RuntimeError("SOCCER_DB_URL missing")
        db = connect(db_url)
        db.ensure_schema()
        now = datetime.utcnow().isoformat(timespec="seconds")
        odds_val = float(args.odds or 0.0)
        has_odds = 1 if odds_val > 1.01 else 0
        source = "odds" if has_odds else "no_odds"
        db.connection.execute(
            """
            INSERT OR IGNORE INTO saved_picks
            (created_at, sport_key, commence_time, home_team, away_team, market_key, outcome, line, odds, score, risk, status, source, has_odds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                args.sport_key,
                args.commence_time,
                args.home_team,
                args.away_team,
                args.market_key,
                args.outcome,
                args.line,
                odds_val,
                float(args.score or 0.0),
                args.risk,
                "pending",
                source,
                has_odds,
            ),
        )
        db.connection.commit()
        print("Saved manual pick.")
        return
    if args.command == "settle":
        db_url = _env("SOCCER_DB_URL")
        if not db_url:
            raise RuntimeError("SOCCER_DB_URL missing")
        db = connect(db_url)
        db.ensure_schema()
        now = datetime.utcnow().isoformat(timespec="seconds")
        db.connection.execute(
            """
            UPDATE saved_picks
            SET status = ?, settled_at = ?, result = ?
            WHERE id = ?
            """,
            ("settled", now, args.result, int(args.id)),
        )
        db.connection.commit()
        print("Settled pick.")
        return
    if args.command in {"pick", "run"}:
        if not data:
            data = _fetch_data(date_str, cache, args.no_cache, client)
        picks = _pick_best(
            data["fixtures"],
            data.get("standings", {}),
            data["odds"],
            cache,
            date_str,
            args.no_cache,
            client,
            data.get("stats_provider", _env("STATS_PROVIDER", "api_football")),
            data.get("therundown", {}),
        )
        _save_picks(cache, date_str, picks)
    if args.command in {"report", "run"}:
        picks = _load_picks(cache, date_str)
        if not picks:
            data = _fetch_data(date_str, cache, args.no_cache, client)
            picks = _pick_best(
                data["fixtures"],
                data.get("standings", {}),
                data["odds"],
                cache,
                date_str,
                args.no_cache,
                client,
                data.get("stats_provider", _env("STATS_PROVIDER", "api_football")),
                data.get("therundown", {}),
            )
            _save_picks(cache, date_str, picks)
        _report(picks)

    print(f"\nAPI_CALLS_TOTAL={client.calls} CACHE_HITS={cache.stats.hits} CACHE_MISSES={cache.stats.misses}")


if __name__ == "__main__":
    main()
