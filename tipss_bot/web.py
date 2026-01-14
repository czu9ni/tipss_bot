from flask import Flask, render_template_string, request, redirect, url_for
from soccer_bot.config import load_config
from soccer_bot.db import connect
from soccer_bot.repo import get_team_stats, list_matches
from soccer_bot.offline_stats import build_team_summary
from soccer_bot.scoring import match_points, table
import html
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
import math
import requests

app = Flask(__name__)

config = load_config()

RSS_FEEDS = [
    {"url": "http://feeds.bbci.co.uk/sport/football/rss.xml", "weight": 0.9, "label": "BBC Football"},
    {"url": "https://www.skysports.com/rss/12040", "weight": 0.8, "label": "Sky Sports Football"},
    {"url": "https://www.espn.com/espn/rss/soccer/news", "weight": 0.8, "label": "ESPN Soccer"},
    {"url": "https://www.theguardian.com/football/rss", "weight": 0.8, "label": "Guardian Football"},
]
RSS_CACHE_TTL_SECONDS = 600
_RSS_CACHE: dict[str, object] = {"fetched_at": 0.0, "items": []}
_GEOCODE_CACHE: dict[str, dict] = {}
_WEATHER_CACHE: dict[str, dict] = {}
_WEIGHTS_CACHE: dict[str, float] | None = None
_RIVALRIES_CACHE: list[tuple[str, str]] | None = None
_TEAM_LOCATION_OVERRIDES: dict[str, dict] | None = None
_SPORTS_CACHE: dict[str, object] = {"fetched_at": 0.0, "keys": []}
_ODDS_LAST_ERROR: str | None = None
_CACHE_KEY = "latest_picks"
SPORTS_CACHE_TTL_SECONDS = 3600
_ELO_CACHE: dict[str, float] = {}
_TEAM_ID_MAP: dict[str, int] | None = None
_FORM_CACHE: dict[int, dict[str, float]] = {}
_FORM_CACHE_TTL_SECONDS = 3600
_STANDINGS_CACHE: dict[str, list[dict]] = {}
_ODDS_MARKETS_DEFAULT = "h2h,totals,btts,team_totals,spreads,draw_no_bet,double_chance,alternate_totals,alternate_team_totals,alternate_spreads"
_PICK_MIN_ODDS = float(os.environ.get("PICK_MIN_ODDS", "0"))
_PICK_MAX_ODDS = float(os.environ.get("PICK_MAX_ODDS", "0"))

_TEMPLATE_PATHS = [
    os.path.join(os.path.dirname(__file__), "data", "ui_template.html"),
    os.path.join(os.path.dirname(__file__), "..", "data", "ui_template.html"),
]


def _load_template() -> str:
    for path in _TEMPLATE_PATHS:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return handle.read()
            except Exception:
                pass
    return "<html><body>Template missing.</body></html>"


TEMPLATE = _load_template()



def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(text or ""))


def _parse_rss_items(xml_text: str) -> list[dict]:
    items = []
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_text)
        for item in root.findall(".//item"):
            title = _strip_html(item.findtext("title", default=""))
            description = _strip_html(item.findtext("description", default=""))
            link = _strip_html(item.findtext("link", default=""))
            items.append({"title": title, "summary": description, "link": link})
        if items:
            return items
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", ns):
            title = _strip_html(entry.findtext("atom:title", default="", namespaces=ns))
            summary = _strip_html(entry.findtext("atom:summary", default="", namespaces=ns))
            link = ""
            link_node = entry.find("atom:link", ns)
            if link_node is not None:
                link = link_node.attrib.get("href", "")
            items.append({"title": title, "summary": summary, "link": link})
    except Exception:
        return []
    return items


def _load_rss_sources() -> list[dict]:
    path = os.path.join("data", "rss_sources.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                if isinstance(data, list):
                    max_sources = int(os.environ.get("RSS_MAX_SOURCES", "0"))
                    if max_sources > 0:
                        return data[:max_sources]
                    return data
        except Exception:
            return RSS_FEEDS
    return RSS_FEEDS


def _load_weights() -> dict[str, float]:
    global _WEIGHTS_CACHE
    if _WEIGHTS_CACHE is not None:
        return _WEIGHTS_CACHE
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
    _WEIGHTS_CACHE = defaults
    return defaults


def _normalize_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]", "", name.lower())
    cleaned = cleaned.replace("fc", "").replace("cf", "").replace("afc", "")
    return cleaned


def _team_id_map() -> dict[str, int]:
    global _TEAM_ID_MAP
    if _TEAM_ID_MAP is not None:
        return _TEAM_ID_MAP
    _TEAM_ID_MAP = {}
    try:
        headers = {"X-Auth-Token": config.football_data_token}
        response = requests.get("https://api.football-data.org/v4/competitions", headers=headers, timeout=15)
        if response.status_code != 200:
            return _TEAM_ID_MAP
        competitions = response.json().get("competitions", [])
        max_comp = int(os.environ.get("FD_TEAMMAP_MAX_COMP", "10"))
        for comp in competitions[:max_comp]:
            comp_code = comp.get("code")
            if not comp_code:
                continue
            teams_resp = requests.get(
                f"https://api.football-data.org/v4/competitions/{comp_code}/teams",
                headers=headers,
                timeout=15,
            )
            if teams_resp.status_code != 200:
                continue
            for team in teams_resp.json().get("teams", []):
                team_id = team.get("id")
                if not team_id:
                    continue
                for key in ("name", "shortName", "tla"):
                    value = team.get(key) or ""
                    norm = _normalize_name(value)
                    if norm:
                        _TEAM_ID_MAP[norm] = int(team_id)
    except Exception:
        return _TEAM_ID_MAP
    return _TEAM_ID_MAP


def _compute_form_from_matches(matches: list[dict], team_id: int, window: int = 6) -> dict[str, float]:
    if not matches:
        return {
            "ppg_norm": 0.5,
            "gd_avg": 0.0,
            "gf_avg": 1.2,
            "ga_avg": 1.2,
            "over25_rate": 0.5,
            "btts_rate": 0.5,
        }
    matches_sorted = sorted(matches, key=lambda m: m.get("utcDate", ""), reverse=True)[:window]
    pts_sum = 0.0
    gd_sum = 0.0
    gf_sum = 0.0
    ga_sum = 0.0
    over_sum = 0.0
    btts_sum = 0.0
    weight_sum = 0.0
    for idx, match in enumerate(matches_sorted):
        weight = math.exp(-idx / 3.0)
        score = match.get("score", {}).get("fullTime", {})
        home_score = score.get("home")
        away_score = score.get("away")
        if home_score is None or away_score is None:
            continue
        home_id = int(match.get("homeTeam", {}).get("id", -1))
        away_id = int(match.get("awayTeam", {}).get("id", -1))
        if home_score == away_score:
            pts = 1
        else:
            winner_home = home_score > away_score
            if team_id == home_id:
                pts = 3 if winner_home else 0
            elif team_id == away_id:
                pts = 3 if not winner_home else 0
            else:
                pts = 0
        if team_id == home_id:
            gf = home_score
            ga = away_score
        elif team_id == away_id:
            gf = away_score
            ga = home_score
        else:
            gf = 0
            ga = 0
        gd = gf - ga
        total_goals = home_score + away_score
        pts_sum += weight * pts
        gd_sum += weight * gd
        gf_sum += weight * gf
        ga_sum += weight * ga
        over_sum += weight * (1.0 if total_goals >= 3 else 0.0)
        btts_sum += weight * (1.0 if home_score > 0 and away_score > 0 else 0.0)
        weight_sum += weight
    if weight_sum <= 0:
        return {
            "ppg_norm": 0.5,
            "gd_avg": 0.0,
            "gf_avg": 1.2,
            "ga_avg": 1.2,
            "over25_rate": 0.5,
            "btts_rate": 0.5,
        }
    ppg = pts_sum / weight_sum
    return {
        "ppg_norm": ppg / 3.0,
        "gd_avg": gd_sum / weight_sum,
        "gf_avg": gf_sum / weight_sum,
        "ga_avg": ga_sum / weight_sum,
        "over25_rate": over_sum / weight_sum,
        "btts_rate": btts_sum / weight_sum,
    }


def _team_form_stats(team_name: str, fallback: dict[str, float]) -> dict[str, float]:
    norm = _normalize_name(team_name)
    team_id = _team_id_map().get(norm)
    fallback_ppg = fallback.get(team_name, 0.5)
    if not team_id:
        return {
            "ppg_norm": fallback_ppg,
            "gd_avg": 0.0,
            "gf_avg": 1.2,
            "ga_avg": 1.2,
            "over25_rate": 0.5,
            "btts_rate": 0.5,
        }
    if team_id in _FORM_CACHE:
        stats = _FORM_CACHE[team_id]
        stats["ppg_norm"] = stats.get("ppg_norm", fallback_ppg)
        return stats
    try:
        headers = {"X-Auth-Token": config.football_data_token}
        response = requests.get(
            f"https://api.football-data.org/v4/teams/{team_id}/matches",
            headers=headers,
            params={"status": "FINISHED", "limit": "6"},
            timeout=15,
        )
        if response.status_code != 200:
            return {
                "ppg_norm": fallback_ppg,
                "gd_avg": 0.0,
                "gf_avg": 1.2,
                "ga_avg": 1.2,
                "over25_rate": 0.5,
                "btts_rate": 0.5,
            }
        matches = response.json().get("matches", [])
        stats = _compute_form_from_matches(matches, team_id)
        _FORM_CACHE[team_id] = stats
        return stats
    except Exception:
        return {
            "ppg_norm": fallback_ppg,
            "gd_avg": 0.0,
            "gf_avg": 1.2,
            "ga_avg": 1.2,
            "over25_rate": 0.5,
            "btts_rate": 0.5,
        }


def _team_form(team_name: str, fallback: dict[str, float]) -> float:
    return _team_form_stats(team_name, fallback).get("ppg_norm", 0.5)


def _fetch_clubelo(team_name: str) -> float:
    norm = team_name.replace(" FC", "").replace("AFC ", "").strip().replace(" ", "_")
    if norm in _ELO_CACHE:
        return _ELO_CACHE[norm]
    try:
        response = requests.get(f"http://api.clubelo.com/{norm}", timeout=15)
        if response.status_code != 200:
            return 1500.0
        lines = [ln.strip() for ln in response.text.splitlines() if ln.strip() and not ln.lower().startswith("date")]
        if not lines:
            return 1500.0
        last = lines[-1]
        parts = [p.strip() for p in last.split(",")]
        for part in parts[::-1]:
            try:
                value = float(part)
                if 800 <= value <= 2600:
                    _ELO_CACHE[norm] = value
                    return value
            except Exception:
                continue
    except Exception:
        return 1500.0
    return 1500.0


def _model_probs(elo_diff: float, form_diff: float) -> tuple[float, float, float]:
    elo_norm = max(-1.0, min(1.0, elo_diff))
    form_norm = max(-1.0, min(1.0, form_diff))
    z = 0.7 * elo_norm + 0.3 * form_norm
    p_home = 1 / (1 + math.exp(-2.0 * z))
    eq = 1.0 - abs(elo_norm)
    p_draw = max(0.12, min(0.32, 0.18 + 0.12 * eq))
    rem = 1.0 - p_draw
    p_home = max(0.05, min(0.95, p_home))
    p_away = rem * (1.0 - p_home)
    p_home = rem * p_home
    return (p_home, p_draw, p_away)


def _injury_index(items: list[dict], team_name: str) -> float:
    tokens = _team_tokens(team_name)
    if not tokens:
        return 0.0
    negative_words = ["injury", "suspension", "ruled out", "ban", "doubt", "illness"]
    count = 0
    for item in items:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        if not any(token in text for token in tokens):
            continue
        if any(word in text for word in negative_words):
            count += 1
    return min(1.0, count / 3.0)


def _load_rivalries() -> list[tuple[str, str]]:
    global _RIVALRIES_CACHE
    if _RIVALRIES_CACHE is not None:
        return _RIVALRIES_CACHE
    default_pairs = [
        ("Barcelona", "Real Madrid"),
        ("Atletico Madrid", "Real Madrid"),
        ("Barcelona", "Espanyol"),
        ("AC Milan", "Inter"),
        ("Juventus", "Inter"),
        ("Roma", "Lazio"),
        ("Napoli", "Roma"),
        ("Bayern Munich", "Borussia Dortmund"),
        ("Schalke", "Borussia Dortmund"),
        ("Arsenal", "Tottenham"),
        ("Manchester United", "Manchester City"),
        ("Liverpool", "Everton"),
        ("Manchester United", "Liverpool"),
        ("Chelsea", "Arsenal"),
        ("Celtic", "Rangers"),
        ("PSG", "Marseille"),
    ]
    path = os.path.join("data", "rivalries.json")
    pairs: list[tuple[str, str]] = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, list) and len(item) == 2:
                            pairs.append((str(item[0]), str(item[1])))
        except Exception:
            pairs = []
    if not pairs:
        pairs = default_pairs
    _RIVALRIES_CACHE = pairs
    return pairs


def _normalize_team_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]", "", name.lower())
    cleaned = cleaned.replace("fc", "").replace("cf", "").replace("afc", "")
    return cleaned


def _sport_key_to_comp(sport_key: str) -> str | None:
    mapping = {
        "soccer_epl": "PL",
        "soccer_spain_la_liga": "PD",
        "soccer_italy_serie_a": "SA",
        "soccer_germany_bundesliga": "BL1",
        "soccer_france_ligue_one": "FL1",
        "soccer_netherlands_eredivisie": "DED",
        "soccer_portugal_primeira_liga": "PPL",
        "soccer_scotland_premiership": "SPL",
        "soccer_belgium_first_div": "BSA",
        "soccer_austria_bundesliga": "ABL",
        "soccer_sweden_allsvenskan": "ASL",
        "soccer_denmark_superliga": "DSL",
        "soccer_norway_eliteserien": "EL1",
        "soccer_turkey_super_league": "TSL",
        "soccer_brazil_campeonato": "BSA",
        "soccer_usa_mls": "MLS",
    }
    if sport_key in mapping.values():
        return sport_key
    return mapping.get(sport_key)


def _fetch_standings(comp_code: str) -> list[dict]:
    if not comp_code:
        return []
    if comp_code in _STANDINGS_CACHE:
        return _STANDINGS_CACHE[comp_code]
    try:
        headers = {"X-Auth-Token": config.football_data_token}
        response = requests.get(
            f"https://api.football-data.org/v4/competitions/{comp_code}/standings",
            headers=headers,
            timeout=12,
        )
        if response.status_code != 200:
            return []
        data = response.json()
        tables = data.get("standings", [])
        total = None
        for table_item in tables:
            if table_item.get("type") == "TOTAL":
                total = table_item
                break
        total = total or (tables[0] if tables else None)
        if not total:
            return []
        rows = []
        for row in total.get("table", [])[:20]:
            team = row.get("team", {}).get("name")
            points = row.get("points")
            position = row.get("position")
            if team and points is not None and position is not None:
                rows.append({"position": position, "team": team, "points": points})
        _STANDINGS_CACHE[comp_code] = rows
        return rows
    except Exception:
        return []


def _summary_dict(team_name: str) -> dict:
    summary = build_team_summary(team_name, limit=5)
    matches = []
    for row in summary.matches:
        home = row.home
        away = row.away
        is_home = _normalize_team_name(home) == _normalize_team_name(team_name)
        gf = row.home_goals if is_home else row.away_goals
        ga = row.away_goals if is_home else row.home_goals
        opponent = away if is_home else home
        matches.append(
            {
                "date": row.date,
                "opponent": opponent,
                "score": f"{gf}-{ga}",
                "home": is_home,
            }
        )
    return {
        "team": summary.team,
        "win_rate": summary.win_rate,
        "goals_for_avg": summary.goals_for_avg,
        "corners_avg": summary.corners_avg,
        "cards_avg": summary.cards_avg,
        "source": summary.source,
        "matches": matches,
    }


def _standings_highlight(standings: list[dict], team_name: str) -> dict:
    key = _normalize_team_name(team_name)
    for row in standings:
        if _normalize_team_name(row.get("team", "")) == key:
            return row
    return {}


def _enrich_pick(pick: dict) -> dict:
    if not pick:
        return pick
    pick["home_summary"] = _summary_dict(pick.get("home_team", ""))
    pick["away_summary"] = _summary_dict(pick.get("away_team", ""))
    comp_code = _sport_key_to_comp(pick.get("sport_key", ""))
    standings = _fetch_standings(comp_code)
    pick["standings"] = standings
    pick["home_standing"] = _standings_highlight(standings, pick.get("home_team", ""))
    pick["away_standing"] = _standings_highlight(standings, pick.get("away_team", ""))
    return pick


def _is_rivalry(home_team: str, away_team: str) -> bool:
    home_norm = _normalize_team_name(home_team)
    away_norm = _normalize_team_name(away_team)
    for left, right in _load_rivalries():
        left_norm = _normalize_team_name(left)
        right_norm = _normalize_team_name(right)
        if not left_norm or not right_norm:
            continue
        if (left_norm in home_norm and right_norm in away_norm) or (
            left_norm in away_norm and right_norm in home_norm
        ):
            return True
    return False


def _within_hours(match: dict, hours: int) -> bool:
    commence = match.get("commence_time")
    if not commence or not isinstance(commence, str):
        return False
    try:
        if commence.endswith("Z"):
            commence_dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        else:
            commence_dt = datetime.fromisoformat(commence)
        if commence_dt.tzinfo is None:
            commence_dt = commence_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = commence_dt - now
        return 0 <= delta.total_seconds() <= hours * 3600
    except Exception:
        return False


def _fetch_rss_items() -> list[dict]:
    now = time.time()
    cached_at = float(_RSS_CACHE.get("fetched_at", 0.0))
    if now - cached_at < RSS_CACHE_TTL_SECONDS:
        return list(_RSS_CACHE.get("items", []))

    items: list[dict] = []
    for source in _load_rss_sources():
        url = source.get("url", "")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        weight = float(source.get("weight", 0.5))
        try:
            response = requests.get(url, timeout=8)
            if response.status_code == 200:
                for item in _parse_rss_items(response.text):
                    item["weight"] = weight
                    item["source"] = source.get("label", url)
                    items.append(item)
        except Exception:
            pass
    max_items = int(os.environ.get("RSS_MAX_ITEMS", "0"))
    if max_items > 0:
        items = items[:max_items]

    _RSS_CACHE["fetched_at"] = now
    _RSS_CACHE["items"] = items
    return items


def _team_tokens(team_name: str) -> list[str]:
    lowered = team_name.lower()
    tokens = [lowered]
    tokens.append(re.sub(r"\b(fc|cf|afc)\b", "", lowered).strip())
    return [token for token in tokens if token]


def _news_factor(items: list[dict], home_team: str, away_team: str) -> float:
    negative_words = ["injury", "suspension", "ruled out", "ban", "doubt", "illness"]
    positive_words = ["return", "back", "fit", "boost", "available"]
    tokens = _team_tokens(home_team) + _team_tokens(away_team)
    if not tokens:
        return 0.0

    score = 0.0
    matches = 0
    for item in items:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        if not any(token in text for token in tokens):
            continue
        matches += 1
        weight = float(item.get("weight", 0.5))
        if any(word in text for word in negative_words):
            score -= 0.05 * weight
        if any(word in text for word in positive_words):
            score += 0.03 * weight
    if matches == 0:
        return 0.0
    return max(-0.2, min(0.2, score))


def _load_team_location_overrides() -> dict[str, dict]:
    global _TEAM_LOCATION_OVERRIDES
    if _TEAM_LOCATION_OVERRIDES is not None:
        return _TEAM_LOCATION_OVERRIDES
    path = os.path.join("data", "team_locations.json")
    _TEAM_LOCATION_OVERRIDES = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                if isinstance(data, dict):
                    _TEAM_LOCATION_OVERRIDES = data
        except Exception:
            _TEAM_LOCATION_OVERRIDES = {}
    return _TEAM_LOCATION_OVERRIDES


def _geocode(name: str) -> dict | None:
    if not name:
        return None
    if name in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[name]
    try:
        response = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": 1, "language": "en", "format": "json"},
            timeout=8,
        )
        if response.status_code == 200:
            data = response.json()
            results = data.get("results", [])
            if results:
                _GEOCODE_CACHE[name] = results[0]
                return results[0]
    except Exception:
        pass
    return None


def _weather_factor(home_team: str) -> float:
    if not home_team:
        return 0.0
    if home_team in _WEATHER_CACHE:
        return float(_WEATHER_CACHE[home_team]["factor"])
    overrides = _load_team_location_overrides()
    if home_team in overrides:
        override = overrides[home_team]
        if "latitude" in override and "longitude" in override:
            result = override
        elif "lat" in override and "lon" in override:
            result = {"latitude": override["lat"], "longitude": override["lon"]}
        else:
            result = None
    else:
        result = _geocode(f"{home_team} stadium") or _geocode(f"{home_team} football club") or _geocode(home_team)
    if not result:
        _WEATHER_CACHE[home_team] = {"factor": 0.0}
        return 0.0
    try:
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": result["latitude"],
                "longitude": result["longitude"],
                "hourly": "precipitation_probability,precipitation",
                "forecast_days": 1,
            },
            timeout=8,
        )
        if response.status_code == 200:
            data = response.json()
            probabilities = data.get("hourly", {}).get("precipitation_probability", [])
            if probabilities:
                high_rain = any(value >= 50 for value in probabilities)
                factor = -0.1 if high_rain else 0.0
                _WEATHER_CACHE[home_team] = {"factor": factor}
                return factor
    except Exception:
        pass
    _WEATHER_CACHE[home_team] = {"factor": 0.0}
    return 0.0


def _score_pick(
    match: dict,
    market_key: str,
    outcome: dict,
    target: float,
    news_items: list[dict],
    db,
    form_scores: dict[str, float],
    table_scores: dict[str, float],
) -> dict | None:
    home_team = match.get("home_team", "")
    away_team = match.get("away_team", "")
    if not home_team or not away_team:
        return None
    price = outcome.get("price")
    if not isinstance(price, (int, float)):
        return None
    price = float(price)
    if _PICK_MIN_ODDS > 0 and price < _PICK_MIN_ODDS:
        return None
    if _PICK_MAX_ODDS > 0 and price > _PICK_MAX_ODDS:
        return None
    implied_prob = 1 / price
    weather_factor = min(1.0, max(0.0, abs(_weather_factor(home_team)) * 10.0))
    news_score = _news_factor(news_items, home_team, away_team)
    injury_home = _injury_index(news_items, home_team)
    injury_away = _injury_index(news_items, away_team)
    elo_home = _fetch_clubelo(home_team)
    elo_away = _fetch_clubelo(away_team)
    elo_diff = max(-1.0, min(1.0, (elo_home - elo_away) / 400.0))
    home_stats = _team_form_stats(home_team, form_scores)
    away_stats = _team_form_stats(away_team, form_scores)
    form_home = home_stats.get("ppg_norm", 0.5)
    form_away = away_stats.get("ppg_norm", 0.5)
    form_diff = max(-1.0, min(1.0, (form_home - form_away)))
    table_home = table_scores.get(home_team, 0.5)
    table_away = table_scores.get(away_team, 0.5)
    table_diff = max(-1.0, min(1.0, table_home - table_away))
    exp_home, exp_away, exp_total = _expected_goals(home_stats, away_stats)

    outcome_name = str(outcome.get("name") or "")
    point_val = outcome.get("point")
    point = float(point_val) if isinstance(point_val, (int, float)) else None
    description = str(outcome.get("description") or "")

    model_prob = 0.0
    selection_label = outcome_name
    elo_strength = 0.5
    form_strength = 0.5
    table_strength = 0.5
    injury_index = (injury_home + injury_away) / 2.0

    if market_key == "h2h":
        p_home, p_draw, p_away = _model_probs(elo_diff, form_diff)
        if outcome_name == "Draw":
            model_prob = p_draw
            selection_label = "Donto"
            table_strength = 0.5
        elif outcome_name == home_team:
            model_prob = p_home
            selection_label = "Hazai gyozelem"
            elo_strength = (elo_diff + 1.0) / 2.0
            form_strength = (form_diff + 1.0) / 2.0
            table_strength = (table_diff + 1.0) / 2.0
            injury_index = injury_home
        elif outcome_name == away_team:
            model_prob = p_away
            selection_label = "Vendeg gyozelem"
            elo_strength = ((-elo_diff) + 1.0) / 2.0
            form_strength = ((-form_diff) + 1.0) / 2.0
            table_strength = ((-table_diff) + 1.0) / 2.0
            injury_index = injury_away
        else:
            return None
    elif market_key == "double_chance":
        p_home, p_draw, p_away = _model_probs(elo_diff, form_diff)
        lowered = outcome_name.lower()
        if lowered in {"1x", "1-x", "home or draw"} or ("draw" in lowered and home_team.lower() in lowered):
            model_prob = p_home + p_draw
            selection_label = "1X"
            table_strength = (table_diff + 1.0) / 2.0
            injury_index = injury_home
        elif lowered in {"x2", "x-2", "draw or away"} or ("draw" in lowered and away_team.lower() in lowered):
            model_prob = p_away + p_draw
            selection_label = "X2"
            table_strength = ((-table_diff) + 1.0) / 2.0
            injury_index = injury_away
        elif lowered in {"12", "1-2", "home or away"} or (home_team.lower() in lowered and away_team.lower() in lowered):
            model_prob = p_home + p_away
            selection_label = "12"
            table_strength = 0.5
        else:
            return None
        form_strength = model_prob
    elif market_key == "draw_no_bet":
        p_home, _, p_away = _model_probs(elo_diff, form_diff)
        denom = max(0.01, p_home + p_away)
        if outcome_name == home_team:
            model_prob = p_home / denom
            selection_label = "Hazai DNB"
            elo_strength = (elo_diff + 1.0) / 2.0
            form_strength = (form_diff + 1.0) / 2.0
            table_strength = (table_diff + 1.0) / 2.0
            injury_index = injury_home
        elif outcome_name == away_team:
            model_prob = p_away / denom
            selection_label = "Vendeg DNB"
            elo_strength = ((-elo_diff) + 1.0) / 2.0
            form_strength = ((-form_diff) + 1.0) / 2.0
            table_strength = ((-table_diff) + 1.0) / 2.0
            injury_index = injury_away
        else:
            return None
    elif market_key in {"totals", "alternate_totals"}:
        if point is None:
            return None
        floor_line = int(math.floor(point))
        if outcome_name.lower() == "over":
            model_prob = 1.0 - _poisson_cdf(exp_total, floor_line)
            selection_label = f"Over {point:.1f}"
        elif outcome_name.lower() == "under":
            model_prob = _poisson_cdf(exp_total, floor_line)
            selection_label = f"Under {point:.1f}"
        else:
            return None
        form_strength = model_prob
        table_strength = 0.5
    elif market_key in {"team_totals", "alternate_team_totals"}:
        if point is None:
            return None
        team_name = description or outcome_name
        team_norm = _normalize_team_name(team_name)
        home_norm = _normalize_team_name(home_team)
        away_norm = _normalize_team_name(away_team)
        if team_norm in home_norm:
            team_lambda = exp_home
            selection_label_prefix = "Hazai golok"
        elif team_norm in away_norm:
            team_lambda = exp_away
            selection_label_prefix = "Vendeg golok"
        else:
            return None
        floor_line = int(math.floor(point))
        if outcome_name.lower() == "over":
            model_prob = 1.0 - _poisson_cdf(team_lambda, floor_line)
            selection_label = f"{selection_label_prefix} Over {point:.1f}"
        elif outcome_name.lower() == "under":
            model_prob = _poisson_cdf(team_lambda, floor_line)
            selection_label = f"{selection_label_prefix} Under {point:.1f}"
        else:
            return None
        form_strength = model_prob
        table_strength = 0.5
    elif market_key == "btts":
        p_yes = (1.0 - math.exp(-exp_home)) * (1.0 - math.exp(-exp_away))
        if outcome_name.lower() == "yes":
            model_prob = p_yes
            selection_label = "GG - Igen"
        elif outcome_name.lower() == "no":
            model_prob = 1.0 - p_yes
            selection_label = "GG - Nem"
        else:
            return None
        form_strength = model_prob
        table_strength = 0.5
    elif market_key in {"spreads", "alternate_spreads"}:
        if point is None:
            return None
        mean = exp_home - exp_away
        sigma = 1.25
        if outcome_name == home_team or outcome_name.lower() == "home":
            threshold = -point
            model_prob = 1.0 - _normal_cdf(threshold, mean, sigma)
            selection_label = f"Hazai {point:+.1f}"
            elo_strength = (elo_diff + 1.0) / 2.0
            form_strength = (form_diff + 1.0) / 2.0
            table_strength = (table_diff + 1.0) / 2.0
            injury_index = injury_home
        elif outcome_name == away_team or outcome_name.lower() == "away":
            threshold = point
            model_prob = _normal_cdf(threshold, mean, sigma)
            selection_label = f"Vendeg {point:+.1f}"
            elo_strength = ((-elo_diff) + 1.0) / 2.0
            form_strength = ((-form_diff) + 1.0) / 2.0
            table_strength = ((-table_diff) + 1.0) / 2.0
            injury_index = injury_away
        else:
            return None
    else:
        return None

    form_strength = 0.7 * form_strength + 0.3 * table_strength
    market_diff = max(0.0, model_prob - implied_prob)
    weights = _load_weights()
    total = (
        elo_strength * weights["elo"]
        + form_strength * weights["form"]
        + market_diff * weights["market"]
        + (1.0 - injury_index) * weights["injury"]
        + (1.0 - weather_factor) * weights["weather"]
    )
    return {
        "match_key": _match_key(match),
        "home_team": home_team,
        "away_team": away_team,
        "sport_key": str(match.get("sport_key") or ""),
        "commence_time": str(match.get("commence_time") or ""),
        "market_key": market_key,
        "market_label": _market_label(market_key),
        "outcome": selection_label,
        "line": point,
        "odds": price,
        "distance": abs(price - target),
        "score": total,
        "elo_strength": elo_strength,
        "form_strength": form_strength,
        "market_diff": market_diff,
        "injury_index": injury_index,
        "weather_factor": weather_factor,
        "news_score": news_score,
        "model_prob": model_prob,
        "implied_prob": implied_prob,
    }


def _match_reasons(pick: dict) -> list[str]:
    reasons = []
    market_key = pick.get("market_key", "")
    if pick.get("elo_strength", 0) >= 0.65:
        reasons.append("eros csapateroseg")
    if pick.get("form_strength", 0) >= 0.6:
        reasons.append("friss forma eros")
    if pick.get("market_diff", 0) >= 0.02:
        reasons.append("ertek a piaci oddsban")
    if market_key in {"totals", "alternate_totals"}:
        if str(pick.get("outcome", "")).lower().startswith("over"):
            reasons.append("varhato golszam magas")
        if str(pick.get("outcome", "")).lower().startswith("under"):
            reasons.append("varhato golszam alacsony")
    if market_key in {"team_totals", "alternate_team_totals"}:
        reasons.append("csapatgolos minta")
    if market_key == "btts":
        reasons.append("gg tendencia")
    if market_key == "double_chance":
        reasons.append("biztonsagosabb piac")
    if market_key == "draw_no_bet":
        reasons.append("donto nelkul opcio")
    if market_key in {"spreads", "alternate_spreads"}:
        reasons.append("hendikep irany")
    if pick.get("injury_index", 0) >= 0.4:
        reasons.append("serules kockazat")
    if pick.get("weather_factor", 0) >= 0.4:
        reasons.append("kedvezotlen idojaras")
    if pick.get("news_score", 0) >= 0.05:
        reasons.append("pozitiv hirek")
    if not reasons:
        reasons.append("kiegyensulyozott jelek")
    return reasons


def _build_picks_for_match(
    match: dict,
    target: float,
    news_items: list[dict],
    db,
    form_scores: dict[str, float],
    table_scores: dict[str, float],
) -> list[dict]:
    picks: list[dict] = []
    for market_key in _market_keys():
        outcomes = _average_market_outcomes(match, market_key)
        if not outcomes:
            continue
        for outcome in outcomes:
            scored = _score_pick(match, market_key, outcome, target, news_items, db, form_scores, table_scores)
            if scored:
                picks.append(scored)
    return picks


def _primary_competition(competitions: list[dict]) -> str | None:
    preferred = os.environ.get("PRIMARY_COMP", "PL")
    for comp in competitions:
        if comp.get("code") == preferred:
            return preferred
    if competitions:
        return competitions[0].get("code")
    return None


def _fetch_recent_matches_fd(token: str, comp_code: str, limit: int = 6) -> list[dict]:
    if not token or not comp_code:
        return []
    try:
        response = requests.get(
            f"https://api.football-data.org/v4/competitions/{comp_code}/matches",
            headers={"X-Auth-Token": token},
            params={"status": "FINISHED", "limit": str(limit)},
            timeout=12,
        )
        if response.status_code != 200:
            return []
        matches = []
        for match in response.json().get("matches", []):
            home = match.get("homeTeam", {}).get("name")
            away = match.get("awayTeam", {}).get("name")
            score = match.get("score", {}).get("fullTime", {})
            home_score = score.get("home")
            away_score = score.get("away")
            if home and away and home_score is not None and away_score is not None:
                matches.append(
                    {
                        "home_team": home,
                        "away_team": away,
                        "score": f"{home_score}-{away_score}",
                    }
                )
        return matches[:limit]
    except Exception:
        return []






def _fetch_upcoming_fixtures_fd_all(token: str, hours: int = 24, limit: int = 40) -> list[dict]:
    if not token:
        return []
    try:
        now = datetime.now(timezone.utc)
        date_from = now.date().isoformat()
        days_ahead = max(1, int(math.ceil(hours / 24)))
        date_to = (now + timedelta(days=days_ahead)).date().isoformat()
        response = requests.get(
            "https://api.football-data.org/v4/matches",
            headers={"X-Auth-Token": token},
            params={"status": "SCHEDULED", "dateFrom": date_from, "dateTo": date_to},
            timeout=12,
        )
        if response.status_code != 200:
            return []
        fixtures = []
        for match in response.json().get("matches", []):
            utc_date = match.get("utcDate")
            if not utc_date:
                continue
            try:
                commence_dt = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
            except Exception:
                continue
            delta = commence_dt - now
            if delta.total_seconds() < 0 or delta.total_seconds() > hours * 3600:
                continue
            home = match.get("homeTeam", {})
            away = match.get("awayTeam", {})
            home_name = home.get("name")
            away_name = away.get("name")
            comp = match.get("competition", {})
            comp_code = comp.get("code") or comp.get("id") or ""
            if not home_name or not away_name:
                continue
            fixtures.append(
                {
                    "id": match.get("id"),
                    "sport_key": comp_code,
                    "comp_code": comp_code,
                    "commence_time": utc_date,
                    "home_team": home_name,
                    "away_team": away_name,
                    "home_id": home.get("id"),
                    "away_id": away.get("id"),
                }
            )
        return fixtures[:limit]
    except Exception:
        return []


def _fetch_upcoming_fixtures_fd(token: str, comp_code: str, hours: int = 24, limit: int = 10) -> list[dict]:
    if not token or not comp_code:
        return []
    try:
        now = datetime.now(timezone.utc)
        date_from = now.date().isoformat()
        days_ahead = max(1, int(math.ceil(hours / 24)))
        date_to = (now + timedelta(days=days_ahead)).date().isoformat()
        response = requests.get(
            f"https://api.football-data.org/v4/competitions/{comp_code}/matches",
            headers={"X-Auth-Token": token},
            params={"status": "SCHEDULED", "dateFrom": date_from, "dateTo": date_to},
            timeout=12,
        )
        if response.status_code != 200:
            return []
        fixtures = []
        for match in response.json().get("matches", []):
            utc_date = match.get("utcDate")
            if not utc_date:
                continue
            try:
                commence_dt = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
            except Exception:
                continue
            delta = commence_dt - now
            if delta.total_seconds() < 0 or delta.total_seconds() > hours * 3600:
                continue
            home = match.get("homeTeam", {})
            away = match.get("awayTeam", {})
            home_name = home.get("name")
            away_name = away.get("name")
            if not home_name or not away_name:
                continue
            fixtures.append(
                {
                    "id": match.get("id"),
                    "sport_key": comp_code,
                    "comp_code": comp_code,
                    "commence_time": utc_date,
                    "home_team": home_name,
                    "away_team": away_name,
                    "home_id": home.get("id"),
                    "away_id": away.get("id"),
                }
            )
        return fixtures[:limit]
    except Exception:
        return []


def _team_ppg_fd(token: str, team_id: int | None, limit: int = 6) -> float:
    if not token or not team_id:
        return 1.5
    try:
        response = requests.get(
            f"https://api.football-data.org/v4/teams/{team_id}/matches",
            headers={"X-Auth-Token": token},
            params={"status": "FINISHED", "limit": str(limit)},
            timeout=12,
        )
        if response.status_code != 200:
            return 1.5
        total_points = 0
        count = 0
        for match in response.json().get("matches", []):
            score = match.get("score", {}).get("fullTime", {})
            home_score = score.get("home")
            away_score = score.get("away")
            if home_score is None or away_score is None:
                continue
            home_id = match.get("homeTeam", {}).get("id")
            away_id = match.get("awayTeam", {}).get("id")
            if team_id == home_id:
                if home_score > away_score:
                    points = 3
                elif home_score == away_score:
                    points = 1
                else:
                    points = 0
            elif team_id == away_id:
                if away_score > home_score:
                    points = 3
                elif away_score == home_score:
                    points = 1
                else:
                    points = 0
            else:
                continue
            total_points += points
            count += 1
        if count == 0:
            return 1.5
        return total_points / count
    except Exception:
        return 1.5


def _table_scores_from_standings(standings: list[dict]) -> dict[str, float]:
    if not standings:
        return {}
    max_points = max(row.get("points", 0) for row in standings) or 1
    return {row.get("team", ""): (row.get("points", 0) / max_points) for row in standings}


def _build_stat_only_picks(
    fixtures: list[dict],
    standings_by_comp: dict[str, list[dict]],
    news_items: list[dict],
) -> list[dict]:
    weights = _load_weights()
    picks: list[dict] = []
    for match in fixtures:
        comp_code = match.get("comp_code") or match.get("sport_key", "")
        comp_standings = standings_by_comp.get(comp_code, [])
        table_scores = _table_scores_from_standings(comp_standings)
        home_team = match.get("home_team", "")
        away_team = match.get("away_team", "")
        if not home_team or not away_team:
            continue
        if _is_rivalry(home_team, away_team):
            continue
        elo_home = _fetch_clubelo(home_team)
        elo_away = _fetch_clubelo(away_team)
        elo_diff = max(-1.0, min(1.0, (elo_home - elo_away) / 400.0))
        form_home_ppg = _team_ppg_fd(config.football_data_token, match.get("home_id"))
        form_away_ppg = _team_ppg_fd(config.football_data_token, match.get("away_id"))
        form_diff = max(-1.0, min(1.0, (form_home_ppg - form_away_ppg) / 3.0))
        table_home = table_scores.get(home_team, 0.5)
        table_away = table_scores.get(away_team, 0.5)
        table_diff = max(-1.0, min(1.0, table_home - table_away))

        p_home, p_draw, p_away = _model_probs(elo_diff, form_diff)
        if p_home >= p_draw and p_home >= p_away:
            selection_label = "Hazai gyozelem"
            model_prob = p_home
            elo_strength = (elo_diff + 1.0) / 2.0
            form_strength = (form_diff + 1.0) / 2.0
            table_strength = (table_diff + 1.0) / 2.0
            injury_index = _injury_index(news_items, home_team)
        elif p_away >= p_home and p_away >= p_draw:
            selection_label = "Vendeg gyozelem"
            model_prob = p_away
            elo_strength = ((-elo_diff) + 1.0) / 2.0
            form_strength = ((-form_diff) + 1.0) / 2.0
            table_strength = ((-table_diff) + 1.0) / 2.0
            injury_index = _injury_index(news_items, away_team)
        else:
            selection_label = "Donto"
            model_prob = p_draw
            elo_strength = 0.5
            form_strength = 0.5
            table_strength = 0.5
            injury_index = (_injury_index(news_items, home_team) + _injury_index(news_items, away_team)) / 2.0

        weather_factor = min(1.0, max(0.0, abs(_weather_factor(home_team)) * 10.0))
        news_score = _news_factor(news_items, home_team, away_team)
        form_strength = 0.7 * form_strength + 0.3 * table_strength
        total = (
            elo_strength * weights["elo"]
            + form_strength * weights["form"]
            + (1.0 - injury_index) * weights["injury"]
            + (1.0 - weather_factor) * weights["weather"]
        )

        pick = {
            "match_key": _match_key(match),
            "home_team": home_team,
            "away_team": away_team,
            "sport_key": match.get("sport_key", ""),
            "commence_time": match.get("commence_time", ""),
            "market_key": "h2h",
            "market_label": "1X2 (odds nelkul)",
            "outcome": selection_label,
            "line": None,
            "odds": None,
            "distance": 0.0,
            "score": total,
            "elo_strength": elo_strength,
            "form_strength": form_strength,
            "market_diff": 0.0,
            "injury_index": injury_index,
            "weather_factor": weather_factor,
            "news_score": news_score,
            "model_prob": model_prob,
            "implied_prob": None,
        }
        picks.append(pick)
    picks.sort(key=lambda item: item["score"], reverse=True)
    return picks


def _fetch_standings_fd(token: str, comp_code: str, limit: int = 10) -> list[dict]:
    if not token or not comp_code:
        return []
    try:
        response = requests.get(
            f"https://api.football-data.org/v4/competitions/{comp_code}/standings",
            headers={"X-Auth-Token": token},
            timeout=12,
        )
        if response.status_code != 200:
            return []
        data = response.json()
        tables = data.get("standings", [])
        total = None
        for table_item in tables:
            if table_item.get("type") == "TOTAL":
                total = table_item
                break
        total = total or (tables[0] if tables else None)
        if not total:
            return []
        rows = []
        for row in total.get("table", [])[:limit]:
            team = row.get("team", {}).get("name")
            points = row.get("points")
            position = row.get("position")
            if team and points is not None and position is not None:
                rows.append({"position": position, "team": team, "points": points})
        return rows
    except Exception:
        return []


def _build_form_scores(matches: list[Match], window: int = 5) -> dict[str, float]:
    recent: dict[str, list[int]] = {}
    for match in reversed(matches):
        points = match_points(match)
        for team, score in points.items():
            if team not in recent:
                recent[team] = []
            if len(recent[team]) < window:
                recent[team].append(score)
    form_scores: dict[str, float] = {}
    max_points = 3 * window
    for team, scores in recent.items():
        form_scores[team] = sum(scores) / max_points if max_points else 0.0
    return form_scores


def _build_table_scores(points_table: dict[str, int]) -> dict[str, float]:
    if not points_table:
        return {}
    max_points = max(points_table.values()) or 1
    return {team: points / max_points for team, points in points_table.items()}


def _combine_score(pick_a: dict, pick_b: dict, target: float) -> tuple[float, float]:
    combined_odds = float(pick_a["odds"]) * float(pick_b["odds"])
    base = (float(pick_a["score"]) + float(pick_b["score"])) / 2
    distance_factor = max(0.0, 1.0 - abs(combined_odds - target) / 0.3)
    combo_score = base * 0.8 + distance_factor * 0.2
    return combo_score, combined_odds


def _risk_label(score: float) -> str:
    if score >= 0.85:
        return "green"
    if score >= 0.69:
        return "yellow"
    return "red"



def _stake_from_score(score: float | None) -> float | None:
    if score is None:
        return None
    base = max(0.0, score - 0.6)
    stake = min(2.0, max(0.5, base * 5.0))
    return round(stake, 2)



def _build_best_combo(picks: list[dict], target: float) -> dict | None:
    picks = sorted(picks, key=lambda item: item["score"], reverse=True)[:30]
    best = None
    for i in range(len(picks)):
        for j in range(i + 1, len(picks)):
            if picks[i].get("match_key") == picks[j].get("match_key"):
                continue
            score, combined_odds = _combine_score(picks[i], picks[j], target)
            if not (1.85 <= combined_odds <= 2.15):
                continue
            combo = {
                "matches": [picks[i], picks[j]],
                "combined_odds": combined_odds,
                "score": score,
                "risk": _risk_label(score),
                "forced": False,
            }
            if best is None or combo["score"] > best["score"]:
                best = combo
    if best:
        return best
    fallback = []
    used = set()
    for pick in picks:
        key = pick.get("match_key")
        if key in used:
            continue
        fallback.append(pick)
        used.add(key)
        if len(fallback) == 2:
            break
    if len(fallback) == 2:
        score, combined_odds = _combine_score(fallback[0], fallback[1], target)
        return {
            "matches": fallback,
            "combined_odds": combined_odds,
            "score": score,
            "risk": _risk_label(score),
            "forced": True,
        }
    return None
def _extract_h2h_outcomes(match: dict) -> list[dict]:
    return _average_market_outcomes(match, "h2h")


def _find_price(outcomes: list[dict], name: str) -> float | None:
    for outcome in outcomes:
        if outcome.get("name") == name:
            price = outcome.get("price")
            if isinstance(price, (int, float)):
                return float(price)
    return None


def _best_outcome(outcomes: list[dict], target: float) -> dict | None:
    best = None
    for outcome in outcomes:
        price = outcome.get("price")
        if not isinstance(price, (int, float)):
            continue
        distance = abs(float(price) - target)
        if best is None or distance < best["distance"]:
            best = {"name": outcome.get("name"), "price": float(price), "distance": distance}
    return best


def _market_keys() -> list[str]:
    raw = os.environ.get("ODDS_MARKETS", _ODDS_MARKETS_DEFAULT)
    return [key.strip() for key in raw.split(",") if key.strip()]


def _average_market_outcomes(match: dict, market_key: str) -> list[dict]:
    buckets: dict[tuple[str, float | None, str], list[float]] = {}
    for bookmaker in match.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != market_key:
                continue
            for outcome in market.get("outcomes", []):
                name = str(outcome.get("name") or "")
                point = outcome.get("point")
                point_val = float(point) if isinstance(point, (int, float)) else None
                desc = str(outcome.get("description") or "")
                price = outcome.get("price")
                if not isinstance(price, (int, float)):
                    continue
                key = (name, point_val, desc)
                buckets.setdefault(key, []).append(float(price))
    results: list[dict] = []
    for (name, point, desc), prices in buckets.items():
        if not prices:
            continue
        results.append(
            {
                "name": name,
                "point": point,
                "description": desc,
                "price": sum(prices) / len(prices),
            }
        )
    return results


def _poisson_prob(lmbd: float, k: int) -> float:
    return math.exp(-lmbd) * (lmbd**k) / math.factorial(k)


def _poisson_cdf(lmbd: float, k: int) -> float:
    return sum(_poisson_prob(lmbd, i) for i in range(0, k + 1))


def _normal_cdf(x: float, mean: float, sigma: float) -> float:
    if sigma <= 0:
        return 0.5
    z = (x - mean) / (sigma * math.sqrt(2.0))
    return 0.5 * (1 + math.erf(z))


def _expected_goals(home_stats: dict[str, float], away_stats: dict[str, float]) -> tuple[float, float, float]:
    home_attack = home_stats.get("gf_avg", 1.2)
    home_def = home_stats.get("ga_avg", 1.2)
    away_attack = away_stats.get("gf_avg", 1.2)
    away_def = away_stats.get("ga_avg", 1.2)
    exp_home = max(0.2, (home_attack + away_def) / 2.0)
    exp_away = max(0.2, (away_attack + home_def) / 2.0)
    return exp_home, exp_away, exp_home + exp_away


def _market_label(market_key: str) -> str:
    labels = {
        "h2h": "1X2 (vegeredmeny)",
        "double_chance": "Dupla esely",
        "draw_no_bet": "Donto nelkul",
        "totals": "Osszes gol",
        "alternate_totals": "Osszes gol (alternativ)",
        "team_totals": "Csapatgolok",
        "alternate_team_totals": "Csapatgolok (alternativ)",
        "btts": "Mindket csapat golt szerez",
        "spreads": "Hendikep",
        "alternate_spreads": "Hendikep (alternativ)",
    }
    return labels.get(market_key, market_key)


def _match_key(match: dict) -> str:
    match_id = match.get("id")
    if match_id:
        return str(match_id)
    home_team = match.get("home_team", "")
    away_team = match.get("away_team", "")
    commence = match.get("commence_time", "")
    return f"{home_team}-{away_team}-{commence}"


def _fetch_scores(api_key: str, sport_key: str, days_from: int = 3) -> list[dict]:
    days_from = min(max(days_from, 1), 3)
    try:
        response = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores",
            params={"apiKey": api_key, "daysFrom": days_from},
            timeout=10,
        )
        if response.status_code == 200:
            return response.json()
    except Exception:
        return []
    return []


def _result_from_scores(match: dict) -> tuple[int, int] | None:
    scores = match.get("scores") or []
    if len(scores) < 2:
        return None
    home = scores[0].get("score")
    away = scores[1].get("score")
    if home is None or away is None:
        return None
    try:
        return (int(home), int(away))
    except Exception:
        return None


def _parse_line_from_outcome(outcome: str) -> float | None:
    parts = outcome.replace("+", " +").replace("-", " -").split()
    for part in reversed(parts):
        try:
            return float(part)
        except Exception:
            continue
    return None


def _evaluate_pick(market_key: str, outcome: str, line: float | None, home_goals: int, away_goals: int) -> str:
    total_goals = home_goals + away_goals
    outcome_lower = outcome.lower()
    if market_key == "h2h":
        if outcome_lower.startswith("hazai"):
            return "win" if home_goals > away_goals else "lose"
        if outcome_lower.startswith("vendeg"):
            return "win" if away_goals > home_goals else "lose"
        return "win" if home_goals == away_goals else "lose"
    if market_key == "double_chance":
        if outcome == "1X":
            return "win" if home_goals >= away_goals else "lose"
        if outcome == "X2":
            return "win" if away_goals >= home_goals else "lose"
        if outcome == "12":
            return "win" if home_goals != away_goals else "lose"
    if market_key == "draw_no_bet":
        if home_goals == away_goals:
            return "push"
        if outcome_lower.startswith("hazai"):
            return "win" if home_goals > away_goals else "lose"
        if outcome_lower.startswith("vendeg"):
            return "win" if away_goals > home_goals else "lose"
    if market_key in {"totals", "alternate_totals"}:
        line_val = line if line is not None else _parse_line_from_outcome(outcome)
        if line_val is None:
            return "lose"
        if outcome_lower.startswith("over"):
            return "win" if total_goals > line_val else ("push" if total_goals == line_val else "lose")
        if outcome_lower.startswith("under"):
            return "win" if total_goals < line_val else ("push" if total_goals == line_val else "lose")
    if market_key in {"team_totals", "alternate_team_totals"}:
        line_val = line if line is not None else _parse_line_from_outcome(outcome)
        if line_val is None:
            return "lose"
        if outcome_lower.startswith("hazai"):
            team_goals = home_goals
        else:
            team_goals = away_goals
        if "over" in outcome_lower:
            return "win" if team_goals > line_val else ("push" if team_goals == line_val else "lose")
        if "under" in outcome_lower:
            return "win" if team_goals < line_val else ("push" if team_goals == line_val else "lose")
    if market_key == "btts":
        both = home_goals > 0 and away_goals > 0
        if "igen" in outcome_lower:
            return "win" if both else "lose"
        if "nem" in outcome_lower:
            return "win" if not both else "lose"
    if market_key in {"spreads", "alternate_spreads"}:
        line_val = line if line is not None else _parse_line_from_outcome(outcome)
        if line_val is None:
            return "lose"
        if outcome_lower.startswith("hazai"):
            adjusted = home_goals + line_val
            if adjusted > away_goals:
                return "win"
            if adjusted == away_goals:
                return "push"
            return "lose"
        if outcome_lower.startswith("vendeg"):
            adjusted = away_goals + line_val
            if adjusted > home_goals:
                return "win"
            if adjusted == home_goals:
                return "push"
            return "lose"
    return "lose"


def _settle_saved_picks(db, api_key: str) -> None:
    if not api_key:
        return
    cursor = db.connection.execute(
        """
        SELECT id, sport_key, commence_time, home_team, away_team, market_key, outcome, line
        FROM saved_picks
        WHERE status = 'pending'
        """
    )
    rows = cursor.fetchall()
    if not rows:
        return
    now = datetime.now(timezone.utc)
    pending_by_sport: dict[str, list[tuple]] = {}
    for row in rows:
        pick_id, sport_key, commence_time, home_team, away_team, market_key, outcome, line = row
        try:
            commence_dt = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
        except Exception:
            continue
        if now < commence_dt + timedelta(hours=24):
            continue
        pending_by_sport.setdefault(sport_key, []).append(row)

    if not pending_by_sport:
        return

    for sport_key, picks in pending_by_sport.items():
        scores = _fetch_scores(api_key, sport_key, days_from=3)
        result_map: dict[tuple[str, str, str], tuple[int, int]] = {}
        for match in scores:
            if not match.get("completed"):
                continue
            commence_time = match.get("commence_time")
            home_team = match.get("home_team")
            away_team = match.get("away_team")
            if not commence_time or not home_team or not away_team:
                continue
            result = _result_from_scores(match)
            if result:
                result_map[(str(commence_time), str(home_team), str(away_team))] = result

        for row in picks:
            pick_id, _, commence_time, home_team, away_team, market_key, outcome, line = row
            result = result_map.get((str(commence_time), str(home_team), str(away_team)))
            if not result:
                continue
            home_goals, away_goals = result
            eval_result = _evaluate_pick(market_key, outcome, line, home_goals, away_goals)
            db.connection.execute(
                """
                UPDATE saved_picks
                SET status = ?, settled_at = ?, result = ?
                WHERE id = ?
                """,
                ("settled", now.isoformat(), eval_result, pick_id),
            )
    db.connection.commit()


def _save_pick(db, payload: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db.connection.execute(
        """
        INSERT INTO saved_picks
        (created_at, sport_key, commence_time, home_team, away_team, market_key, outcome, line, odds, score, risk, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now,
            payload.get("sport_key", ""),
            payload.get("commence_time", ""),
            payload.get("home_team", ""),
            payload.get("away_team", ""),
            payload.get("market_key", ""),
            payload.get("outcome", ""),
            payload.get("line"),
            float(payload.get("odds", 0.0)),
            float(payload.get("score", 0.0)),
            payload.get("risk", "yellow"),
            "pending",
        ),
    )
    db.connection.commit()


def _list_saved_picks(db) -> list[dict]:
    cursor = db.connection.execute(
        """
        SELECT created_at, home_team, away_team, market_key, outcome, odds, status, result
        FROM saved_picks
        ORDER BY created_at DESC
        LIMIT 100
        """
    )
    rows = cursor.fetchall()
    results = []
    for created_at, home_team, away_team, market_key, outcome, odds, status, result in rows:
        if result == "win":
            result_label = "nyert"
        elif result == "lose":
            result_label = "vesztett"
        elif result == "push":
            result_label = "visszajaro"
        else:
            result_label = "-"
        results.append(
            {
                "created_at": str(created_at)[:16],
                "home_team": home_team,
                "away_team": away_team,
                "market_key": market_key,
                "market_label": _market_label(market_key),
                "outcome": outcome,
                "odds": float(odds),
                "status": status,
                "result_label": result_label,
            }
        )
    return results


def _store_cached_picks(db, payload: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db.connection.execute(
        """
        INSERT INTO cached_picks (key, payload, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
        """,
        (_CACHE_KEY, json.dumps(payload), now),
    )
    db.connection.commit()


def _load_cached_picks(db) -> dict | None:
    cursor = db.connection.execute(
        "SELECT payload, updated_at FROM cached_picks WHERE key = ?",
        (_CACHE_KEY,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    payload, updated_at = row
    try:
        data = json.loads(payload)
        data["updated_at"] = updated_at
        return data
    except Exception:
        return None


def _select_picks_near_odds(picks: list[dict], target: float, limit: int = 2) -> list[dict]:
    candidates = [pick for pick in picks if abs(pick.get("odds", 0.0) - target) <= 0.15]
    candidates.sort(key=lambda item: (item["distance"], -item["score"]))
    selected: list[dict] = []
    used_matches: set[str] = set()
    for pick in candidates:
        match_key = str(pick.get("match_key", ""))
        if match_key in used_matches:
            continue
        selected.append(pick)
        used_matches.add(match_key)
        if len(selected) >= limit:
            break
    return selected


def _fetch_sports_keys(api_key: str) -> list[str]:
    now = time.time()
    cached_at = float(_SPORTS_CACHE.get("fetched_at", 0.0))
    if now - cached_at < SPORTS_CACHE_TTL_SECONDS:
        return list(_SPORTS_CACHE.get("keys", []))
    keys: list[str] = []
    try:
        response = requests.get(
            "https://api.the-odds-api.com/v4/sports",
            params={"apiKey": api_key},
            timeout=10,
        )
        if response.status_code == 200:
            data = response.json()
            keys = [
                sport["key"]
                for sport in data
                if sport.get("active") and sport.get("key", "").startswith("soccer_")
            ]
    except Exception:
        keys = []
    _SPORTS_CACHE["fetched_at"] = now
    _SPORTS_CACHE["keys"] = keys
    return keys


def _fetch_odds_matches(api_key: str, keys: list[str] | None = None) -> tuple[list[dict], str | None]:
    global _ODDS_LAST_ERROR
    _ODDS_LAST_ERROR = None
    matches: list[dict] = []
    keys = keys or _fetch_sports_keys(api_key)
    max_sports = int(os.environ.get("ODDS_MAX_SPORTS", "0"))
    if max_sports > 0:
        keys = keys[:max_sports]
    markets = ",".join(_market_keys())
    for key in keys:
        try:
            response = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{key}/odds",
                params={"apiKey": api_key, "regions": "eu", "markets": markets},
                timeout=10,
            )
            if response.status_code == 200:
                matches.extend(response.json())
            else:
                try:
                    data = response.json()
                    if data.get("error_code") == "OUT_OF_USAGE_CREDITS":
                        _ODDS_LAST_ERROR = "Odds API kvota elfogyott"
                except Exception:
                    pass
                fallback = requests.get(
                    f"https://api.the-odds-api.com/v4/sports/{key}/odds",
                    params={"apiKey": api_key, "regions": "eu", "markets": "h2h"},
                    timeout=10,
                )
                if fallback.status_code == 200:
                    matches.extend(fallback.json())
                else:
                    try:
                        data = fallback.json()
                        if data.get("error_code") == "OUT_OF_USAGE_CREDITS":
                            _ODDS_LAST_ERROR = "Odds API kvota elfogyott"
                    except Exception:
                        pass
        except Exception:
            pass
    return matches, _ODDS_LAST_ERROR

# HTML template

@app.route('/')
def dashboard():
    active_tab = request.args.get("tab", "tips")
    refresh_requested = request.args.get("refresh") == "1"
    target_odds = 2.0
    window_hours = 24

    db = connect(config.db_url)
    db.ensure_schema()
    matches = list_matches(db)
    points_map = table(matches)
    form_scores = _build_form_scores(matches)
    table_scores = _build_table_scores(points_map)

    # Fetch competitions
    competitions = []
    recent_matches = []
    standings = []
    try:
        headers = {"X-Auth-Token": config.football_data_token}
        response = requests.get("https://api.football-data.org/v4/competitions", headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            competitions = [comp for comp in data.get('competitions', []) if comp.get('code')]
            primary = _primary_competition(competitions)
            if primary:
                recent_matches = _fetch_recent_matches_fd(config.football_data_token, primary)
                standings = _fetch_standings_fd(config.football_data_token, primary)
    except Exception:
        pass

    # Fetch odds and tip
    odds_data = None
    target_matches = []
    rss_items: list[dict] = []
    odds_count = 0
    odds_error = None
    best_pick = None
    best_combo = None
    cached_updated_at = None
    if refresh_requested:
        try:
            if not config.odds_api_key:
                odds_error = "Odds API kulcs hianyzik (odds nelkuli ajanlas)"
                data = []
                eligible = []
                rss_items = _fetch_rss_items()
                standings_by_comp = {}
                fixtures = []
                for comp in competitions:
                    code = comp.get("code")
                    if not code:
                        continue
                    standings_by_comp[code] = _fetch_standings_fd(config.football_data_token, code)
                    fixtures.extend(_fetch_upcoming_fixtures_fd(config.football_data_token, code, window_hours))
                if not fixtures:
                    fixtures = _fetch_upcoming_fixtures_fd_all(config.football_data_token, window_hours)
                picks = _build_stat_only_picks(fixtures, standings_by_comp, rss_items)
                if not picks:
                    fallback_hours = 168
                    fixtures = []
                    for comp in competitions:
                        code = comp.get("code")
                        if not code:
                            continue
                        fixtures.extend(_fetch_upcoming_fixtures_fd(config.football_data_token, code, fallback_hours))
                    if not fixtures:
                        fixtures = _fetch_upcoming_fixtures_fd_all(config.football_data_token, fallback_hours)
                    picks = _build_stat_only_picks(fixtures, standings_by_comp, rss_items)
                    if picks:
                        odds_error = "Odds API kulcs hianyzik (odds nelkuli ajanlas, 7 napos ablak)"
                if not picks:
                    odds_error = "Nincs elerheto meccs 7 napos ablakban"
                odds_count = len(picks)
                best_pick = _enrich_pick(picks[0]) if picks else None
                best_combo = None
                target_matches = [_enrich_pick(item) for item in picks[:2]] if picks else []
                _store_cached_picks(
                    db,
                    {
                        "odds_data": odds_data,
                        "best_pick": best_pick,
                        "best_combo": best_combo,
                        "target_matches": target_matches,
                        "odds_count": odds_count,
                        "odds_error": odds_error,
                        "rss_sources": ", ".join(sorted({item.get("source", "") for item in rss_items if item.get("source")})),
                    },
                )
            else:
                data, odds_error = _fetch_odds_matches(config.odds_api_key)
                eligible = []
                use_odds = bool(data) and not odds_error
                if not use_odds:
                    if not odds_error:
                        odds_error = "Odds API adat nem elerheto (odds nelkuli ajanlas)"
                    rss_items = _fetch_rss_items()
                    standings_by_comp = {}
                    fixtures = []
                    for comp in competitions:
                        code = comp.get("code")
                        if not code:
                            continue
                        standings_by_comp[code] = _fetch_standings_fd(config.football_data_token, code)
                        fixtures.extend(_fetch_upcoming_fixtures_fd(config.football_data_token, code, window_hours))
                    if not fixtures:
                        fixtures = _fetch_upcoming_fixtures_fd_all(config.football_data_token, window_hours)
                    picks = _build_stat_only_picks(fixtures, standings_by_comp, rss_items)
                    if not picks:
                        fallback_hours = 168
                        fixtures = []
                        for comp in competitions:
                            code = comp.get("code")
                            if not code:
                                continue
                            fixtures.extend(_fetch_upcoming_fixtures_fd(config.football_data_token, code, fallback_hours))
                        if not fixtures:
                            fixtures = _fetch_upcoming_fixtures_fd_all(config.football_data_token, fallback_hours)
                        picks = _build_stat_only_picks(fixtures, standings_by_comp, rss_items)
                        if picks:
                            odds_error = "Odds API adat nem elerheto (7 napos ablak)"
                    if not picks:
                        odds_error = "Nincs elerheto meccs 7 napos ablakban"
                    odds_count = len(picks)
                    best_pick = _enrich_pick(picks[0]) if picks else None
                    best_combo = None
                    target_matches = [_enrich_pick(item) for item in picks[:2]] if picks else []
                    _store_cached_picks(
                        db,
                        {
                            "odds_data": odds_data,
                            "best_pick": best_pick,
                            "best_combo": best_combo,
                            "target_matches": target_matches,
                            "odds_count": odds_count,
                            "odds_error": odds_error,
                            "rss_sources": ", ".join(sorted({item.get("source", "") for item in rss_items if item.get("source")})),
                        },
                    )
                if use_odds and data:

                    for match in data:
                        home_team = match.get("home_team", "")
                        away_team = match.get("away_team", "")
                        if not _within_hours(match, window_hours):
                            continue
                        if _is_rivalry(home_team, away_team):
                            continue
                        eligible.append(match)
            if eligible:
                odds_count = len(eligible)
                rss_items = _fetch_rss_items()
                picks: list[dict] = []
                for match in eligible:
                    picks.extend(_build_picks_for_match(match, target_odds, rss_items, db, form_scores, table_scores))
                if picks:
                    best_combo = _build_best_combo(picks, target_odds)
                    best_pick = max(picks, key=lambda item: item["score"])
                    best_pick = _enrich_pick(best_pick)

                odds_match = None
                for match in eligible:
                    outcomes = _extract_h2h_outcomes(match)
                    home_team = match.get("home_team", "")
                    away_team = match.get("away_team", "")
                    if home_team and away_team and outcomes:
                        odds_match = match
                        break
                if odds_match:
                    home_team = odds_match.get("home_team", "")
                    away_team = odds_match.get("away_team", "")
                    outcomes = _extract_h2h_outcomes(odds_match)
                    home_odds = _find_price(outcomes, home_team)
                    away_odds = _find_price(outcomes, away_team)
                    draw_odds = _find_price(outcomes, "Draw")
                    if home_odds is not None and away_odds is not None and draw_odds is not None:
                        home_stats = get_team_stats(db, home_team)
                        away_stats = get_team_stats(db, away_team)
                        stats_factor = (home_stats["win_rate"] - away_stats["win_rate"]) * 0.2

                        home_prob = 1 / home_odds
                        away_prob = 1 / away_odds
                        draw_prob = 1 / draw_odds
                        home_score = home_prob * 0.4 + (1 if home_odds < 2.5 else 0) * 0.1
                        away_score = away_prob * 0.4 + (1 if away_odds < 2.5 else 0) * 0.1
                        draw_score = draw_prob * 0.2

                        weather_factor = _weather_factor(home_team)
                        news_factor = _news_factor(rss_items, home_team, away_team)

                        home_final = home_score + stats_factor + weather_factor + news_factor
                        away_final = away_score + stats_factor + weather_factor + news_factor
                        draw_final = draw_score

                        if home_final > away_final and home_final > draw_final:
                            tip = (
                                "Hazai gyozelem (AI: valoszinuseg "
                                f"{home_prob:.2f}, stats {stats_factor:.2f}, "
                                f"idojaras {weather_factor:.2f}, hirek {news_factor:.2f})"
                            )
                        elif away_final > home_final and away_final > draw_final:
                            tip = (
                                "Vendeg gyozelem (AI: valoszinuseg "
                                f"{away_prob:.2f}, stats {stats_factor:.2f}, "
                                f"idojaras {weather_factor:.2f}, hirek {news_factor:.2f})"
                            )
                        else:
                            tip = (
                                "Donto (AI: valoszinuseg "
                                f"{draw_prob:.2f}, stats {stats_factor:.2f}, "
                                f"idojaras {weather_factor:.2f}, hirek {news_factor:.2f})"
                            )
                        odds_data = {
                            "home_team": home_team,
                            "away_team": away_team,
                            "home_odds": home_odds,
                            "draw_odds": draw_odds,
                            "away_odds": away_odds,
                            "home_prob": home_prob,
                            "draw_prob": draw_prob,
                            "away_prob": away_prob,
                            "tip": tip,
                        }

                if picks:
                    target_matches = _select_picks_near_odds(picks, target_odds, limit=2)
                    target_matches = [_enrich_pick(item) for item in target_matches]
                if best_combo and best_combo.get("matches"):
                    best_combo["matches"] = [_enrich_pick(item) for item in best_combo["matches"]]
            _store_cached_picks(
                db,
                {
                    "odds_data": odds_data,
                    "best_pick": best_pick,
                    "best_combo": best_combo,
                    "target_matches": target_matches,
                    "odds_count": odds_count,
                    "odds_error": odds_error,
                    "rss_sources": ", ".join(sorted({item.get("source", "") for item in rss_items if item.get("source")})),
                },
            )
        except Exception:
            pass
        _settle_saved_picks(db, config.odds_api_key)
    else:
        cached = _load_cached_picks(db)
        if cached:
            odds_data = cached.get("odds_data")
            best_pick = cached.get("best_pick")
            best_combo = cached.get("best_combo")
            target_matches = cached.get("target_matches", [])
            odds_count = cached.get("odds_count", 0)
            odds_error = cached.get("odds_error")
            cached_updated_at = cached.get("updated_at")
    saved_picks = _list_saved_picks(db)
    stake_pct = _stake_from_score(best_pick.get("score") if best_pick else None)

    rss_sources = ", ".join(
        sorted({item.get("source", "") for item in rss_items if item.get("source")})
    )
    if cached_updated_at and not rss_sources:
        rss_sources = cached.get("rss_sources", "") if cached else ""
    return render_template_string(TEMPLATE,
                                  odds_configured=bool(config.odds_api_key),
                                  football_configured=bool(config.football_data_token),
                                  competitions=competitions,
                                  odds=odds_data,
                                  best_pick=best_pick,
                                  best_combo=best_combo,
                                  odds_count=odds_count,
                                  target_matches=target_matches,
                                  recent_matches=recent_matches,
                                  standings=standings,
                                  _match_reasons=_match_reasons,
                                  rss_items=rss_items,
                                  rss_sources=rss_sources,
                                  saved_picks=saved_picks,
                                  stake_pct=stake_pct,
                                  cached_updated_at=cached_updated_at,
                                  odds_error=odds_error,
                                  active_tab=active_tab)


@app.route("/save_pick", methods=["POST"])
def save_pick():
    db = connect(config.db_url)
    db.ensure_schema()
    payload = {
        "sport_key": request.form.get("sport_key", ""),
        "commence_time": request.form.get("commence_time", ""),
        "home_team": request.form.get("home_team", ""),
        "away_team": request.form.get("away_team", ""),
        "market_key": request.form.get("market_key", ""),
        "outcome": request.form.get("outcome", ""),
        "line": request.form.get("line") or None,
        "odds": request.form.get("odds", "0"),
        "score": request.form.get("score", "0"),
        "risk": request.form.get("risk", "yellow"),
    }
    try:
        if payload["line"] is not None:
            payload["line"] = float(payload["line"])
    except Exception:
        payload["line"] = None
    _save_pick(db, payload)
    return redirect(url_for("dashboard", tab="saved"))

if __name__ == '__main__':
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug)
