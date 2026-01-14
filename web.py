from flask import Flask, render_template_string, request
from soccer_bot.config import load_config
from soccer_bot.db import connect
from soccer_bot.repo import Match, add_match, get_team_stats, list_matches
from soccer_bot.scoring import match_points, table
import html
import json
import os
import re
import time
from datetime import datetime, timezone
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
SPORTS_CACHE_TTL_SECONDS = 3600
_ELO_CACHE: dict[str, float] = {}
_TEAM_ID_MAP: dict[str, int] | None = None
_FORM_CACHE: dict[int, tuple[float, float]] = {}
_FORM_CACHE_TTL_SECONDS = 3600


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


def _compute_form_from_matches(matches: list[dict], team_id: int, window: int = 6) -> tuple[float, float]:
    if not matches:
        return (0.5, 0.0)
    matches_sorted = sorted(matches, key=lambda m: m.get("utcDate", ""), reverse=True)[:window]
    pts_sum = 0.0
    gd_sum = 0.0
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
            gd = home_score - away_score
        elif team_id == away_id:
            gd = away_score - home_score
        else:
            gd = 0
        pts_sum += weight * pts
        gd_sum += weight * gd
        weight_sum += weight
    if weight_sum <= 0:
        return (0.5, 0.0)
    ppg = pts_sum / weight_sum
    return (ppg / 3.0, gd_sum / weight_sum)


def _team_form(team_name: str, fallback: dict[str, float]) -> float:
    norm = _normalize_name(team_name)
    team_id = _team_id_map().get(norm)
    if not team_id:
        return fallback.get(team_name, 0.5)
    if team_id in _FORM_CACHE:
        return _FORM_CACHE[team_id][0]
    try:
        headers = {"X-Auth-Token": config.football_data_token}
        response = requests.get(
            f"https://api.football-data.org/v4/teams/{team_id}/matches",
            headers=headers,
            params={"status": "FINISHED", "limit": "6"},
            timeout=15,
        )
        if response.status_code != 200:
            return fallback.get(team_name, 0.5)
        matches = response.json().get("matches", [])
        ppg_norm, gd = _compute_form_from_matches(matches, team_id)
        _FORM_CACHE[team_id] = (ppg_norm, gd)
        return ppg_norm
    except Exception:
        return fallback.get(team_name, 0.5)


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


def _score_match(
    match: dict,
    target: float,
    news_items: list[dict],
    db,
    form_scores: dict[str, float],
    table_scores: dict[str, float],
) -> dict | None:
    outcomes = _extract_h2h_outcomes(match)
    best = _best_outcome(outcomes, target)
    if not best:
        return None
    home_team = match.get("home_team", "")
    away_team = match.get("away_team", "")
    if not home_team or not away_team:
        return None
    implied_prob = 1 / best["price"]
    weather_factor = min(1.0, max(0.0, abs(_weather_factor(home_team)) * 10.0))
    news_score = _news_factor(news_items, home_team, away_team)
    injury_home = _injury_index(news_items, home_team)
    injury_away = _injury_index(news_items, away_team)
    elo_home = _fetch_clubelo(home_team)
    elo_away = _fetch_clubelo(away_team)
    elo_diff = max(-1.0, min(1.0, (elo_home - elo_away) / 400.0))
    form_home = _team_form(home_team, form_scores)
    form_away = _team_form(away_team, form_scores)
    form_diff = max(-1.0, min(1.0, (form_home - form_away)))
    outcome = best["name"]
    if outcome == "Draw":
        elo_strength = 0.5
        form_strength = 0.5
        injury_index = (injury_home + injury_away) / 2
        p_home, p_draw, p_away = _model_probs(elo_diff, form_diff)
        model_prob = p_draw
    elif outcome == home_team:
        elo_strength = (elo_diff + 1.0) / 2.0
        form_strength = (form_diff + 1.0) / 2.0
        injury_index = injury_home
        p_home, p_draw, p_away = _model_probs(elo_diff, form_diff)
        model_prob = p_home
    else:
        elo_strength = ((-elo_diff) + 1.0) / 2.0
        form_strength = ((-form_diff) + 1.0) / 2.0
        injury_index = injury_away
        p_home, p_draw, p_away = _model_probs(elo_diff, form_diff)
        model_prob = p_away
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
        "home_team": home_team,
        "away_team": away_team,
        "outcome": best["name"],
        "odds": best["price"],
        "distance": best["distance"],
        "score": total,
        "elo_strength": elo_strength,
        "form_strength": form_strength,
        "market_diff": market_diff,
        "injury_index": injury_index,
        "weather_factor": weather_factor,
        "news_score": news_score,
    }


def _match_reasons(pick: dict) -> list[str]:
    reasons = []
    if pick.get("elo_strength", 0) >= 0.65:
        reasons.append("eros csapateroseg")
    if pick.get("form_strength", 0) >= 0.6:
        reasons.append("friss forma eros")
    if pick.get("market_diff", 0) >= 0.02:
        reasons.append("ertek a piaci oddsban")
    if pick.get("injury_index", 0) >= 0.4:
        reasons.append("serules kockazat")
    if pick.get("weather_factor", 0) >= 0.4:
        reasons.append("kedvezotlen idojaras")
    if pick.get("news_score", 0) >= 0.05:
        reasons.append("pozitiv hirek")
    if not reasons:
        reasons.append("kiegyensulyozott jelek")
    return reasons


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


def _build_best_combo(picks: list[dict], target: float) -> dict | None:
    picks = sorted(picks, key=lambda item: item["score"], reverse=True)[:30]
    best = None
    for i in range(len(picks)):
        for j in range(i + 1, len(picks)):
            score, combined_odds = _combine_score(picks[i], picks[j], target)
            if not (1.85 <= combined_odds <= 2.15):
                continue
            combo = {
                "matches": [picks[i], picks[j]],
                "combined_odds": combined_odds,
                "score": score,
                "risk": _risk_label(score),
            }
            if best is None or combo["score"] > best["score"]:
                best = combo
    return best
def _extract_h2h_outcomes(match: dict) -> list[dict]:
    for bookmaker in match.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") == "h2h":
                return market.get("outcomes", [])
    return []


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


def _select_matches_near_odds(
    matches: list[dict],
    target: float,
    news_items: list[dict],
    db,
    form_scores: dict[str, float],
    table_scores: dict[str, float],
    limit: int = 2,
) -> list[dict]:
    candidates = []
    for match in matches:
        scored = _score_match(match, target, news_items, db, form_scores, table_scores)
        if scored:
            candidates.append(scored)
    candidates.sort(key=lambda item: (item["distance"], -item["score"]))
    return candidates[:limit]


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


def _fetch_odds_matches(api_key: str, keys: list[str] | None = None) -> list[dict]:
    matches: list[dict] = []
    keys = keys or _fetch_sports_keys(api_key)
    max_sports = int(os.environ.get("ODDS_MAX_SPORTS", "0"))
    if max_sports > 0:
        keys = keys[:max_sports]
    for key in keys:
        try:
            response = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{key}/odds",
                params={"apiKey": api_key, "regions": "eu", "markets": "h2h"},
                timeout=10,
            )
            if response.status_code == 200:
                matches.extend(response.json())
        except Exception:
            pass
    return matches

# HTML template
TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Soccer Bot - Profi AI Tipp Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        .hero { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 60px 0; }
        .card { border: none; box-shadow: 0 4px 8px rgba(0,0,0,0.1); transition: transform 0.2s; }
        .card:hover { transform: translateY(-5px); }
        .tip-highlight { background-color: #e8f5e8; border-left: 5px solid #28a745; }
        .odds-table th { background-color: #f8f9fa; }
        .badge { font-size: 0.9em; }
        .ai-tip { font-size: 1.2em; font-weight: bold; }
        .risk-green { background-color: #d4edda; }
        .risk-yellow { background-color: #fff3cd; }
        .risk-red { background-color: #f8d7da; }
    </style>
</head>
<body>
    <div class="hero text-center">
        <div class="container">
            <h1 class="display-4">Soccer Bot AI Dashboard</h1>
            <p class="lead">Fejlett futball tippek es elemzesek</p>
        </div>
    </div>

    <div class="container my-5">
        <div class="row">
            <div class="col-md-6">
                <div class="card mb-4">
                    <div class="card-header bg-primary text-white">
                        <h5 class="mb-0">API Allapot</h5>
                    </div>
                    <div class="card-body">
                        <p><strong>Odds API:</strong> {{ "Beallitva" if odds_configured else "Hianyzik" }}</p>
                        <p><strong>Football Data:</strong> {{ "Beallitva" if football_configured else "Hianyzik" }}</p>
                        <p><strong>Idojaras:</strong> Open-Meteo (nincs kulcs)</p>
                        <p><strong>Hirek:</strong> RSS feedek (nincs kulcs)</p>
                    </div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="card mb-4">
                    <div class="card-header bg-success text-white">
                        <h5 class="mb-0">Aktiv bajnoksagok</h5>
                    </div>
                    <div class="card-body">
                        <ul class="list-group list-group-flush">
                        {% for comp in competitions %}
                            <li class="list-group-item d-flex justify-content-between align-items-center">
                                {{ comp.name }}
                                <span class="badge bg-secondary rounded-pill">{{ comp.code }}</span>
                            </li>
                        {% endfor %}
                        </ul>
                    </div>
                </div>
            </div>
        </div>

        <div class="row">
            <div class="col-12">
                <div class="card mb-4 tip-highlight">
                    <div class="card-header bg-warning text-dark">
                        <h5 class="mb-0">Elo oddsok es AI tipp</h5>
                    </div>
                    <div class="card-body">
                        {% if odds %}
                        <h6 class="text-center mb-3">{{ odds.home_team }} vs {{ odds.away_team }}</h6>
                        <table class="table table-striped odds-table text-center">
                            <thead>
                                <tr>
                                    <th>Hazai</th>
                                    <th>Donto</th>
                                    <th>Vendeg</th>
                                </tr>
                            </thead>
                            <tbody>
                                <tr>
                                    <td><strong>{{ "%.2f"|format(odds.home_odds) }}</strong></td>
                                    <td><strong>{{ "%.2f"|format(odds.draw_odds) }}</strong></td>
                                    <td><strong>{{ "%.2f"|format(odds.away_odds) }}</strong></td>
                                </tr>
                            </tbody>
                        </table>
                        <div class="alert alert-success text-center">
                            <h5 class="ai-tip">AI javaslat: {{ odds.tip }}</h5>
                        </div>
                        <canvas id="oddsChart" width="400" height="200"></canvas>
                        {% else %}
                        <p class="text-muted text-center">Jelenleg nincs elerheto elo odds.</p>
                        {% endif %}
                    </div>
                </div>
            </div>
        </div>
        <div class="row">
            <div class="col-12">
                <div class="card mb-4">
                    <div class="card-header bg-dark text-white">
                        <h5 class="mb-0">Legjobb 2-meccses kombi (24h, nem rangado, ~2.00)</h5>
                    </div>
                    <div class="card-body">
                        {% if best_combo %}
                            <p class="text-center mb-2">
                                <strong>Ossz odds: {{ "%.2f"|format(best_combo.combined_odds) }}</strong>
                            </p>
                            <table class="table table-striped text-center">
                                <thead>
                                    <tr>
                                        <th>Match</th>
                                        <th>Kimenetel</th>
                                        <th>Odds</th>
                                        <th>Pont</th>
                                        <th>Kockazat</th>
                                        <th>Indok</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {% for pick in best_combo.matches %}
                                    <tr class="risk-{{ best_combo.risk }}">
                                        <td>{{ pick.home_team }} vs {{ pick.away_team }}</td>
                                        <td>{{ pick.outcome }}</td>
                                        <td><strong>{{ "%.2f"|format(pick.odds) }}</strong></td>
                                        <td>{{ "%.2f"|format(pick.score) }}</td>
                                        <td>{{ "zold" if best_combo.risk == "green" else ("sarga" if best_combo.risk == "yellow" else "piros") }}</td>
                                        <td>{{ ", ".join(_match_reasons(pick)) }}</td>
                                    </tr>
                                    {% endfor %}
                                </tbody>
                            </table>
                            <p class="text-muted text-center">Odds pool: {{ odds_count }} meccs (24h, nem rangado)</p>
                        {% else %}
                            <p class="text-muted text-center">Nincs ervenyes 2-meccses kombi 2.00 ± 0.15 savban.</p>
                        {% endif %}
                    </div>
                </div>
            </div>
        </div>

        <div class="row">
            <div class="col-12">
                <div class="card mb-4">
                    <div class="card-header bg-secondary text-white">
                        <h5 class="mb-0">Ket meccs 2.00 koruli oddssal</h5>
                    </div>
                    <div class="card-body">
                        <form method="get" class="text-center mb-3">
                            <input type="hidden" name="tips" value="1">
                            <button class="btn btn-success" type="submit">Mutass 2 meccset 2.00 korul</button>
                        </form>
                        {% if tips_requested %}
                            {% if target_matches %}
                                <table class="table table-striped text-center">
                                    <thead>
                                        <tr>
                                            <th>Match</th>
                                            <th>Kimenetel</th>
                                            <th>Odds</th>
                                            <th>Pont</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {% for match in target_matches %}
                                        <tr>
                                            <td>{{ match.home_team }} vs {{ match.away_team }}</td>
                                            <td>{{ match.outcome }}</td>
                                            <td><strong>{{ "%.2f"|format(match.odds) }}</strong></td>
                                            <td>{{ "%.2f"|format(match.score) }}</td>
                                        </tr>
                                        {% endfor %}
                                    </tbody>
                                </table>
                            {% else %}
                                <p class="text-muted text-center">Nincs meccs 2.00 koruli oddssal.</p>
                            {% endif %}
                        {% endif %}
                    </div>
                </div>
            </div>
        </div>

        <div class="row">
            <div class="col-12">
                <div class="card mb-4">
                    <div class="card-header bg-light">
                        <h5 class="mb-0">Legfrissebb hirek (RSS)</h5>
                    </div>
                    <div class="card-body">
                        {% if rss_items %}
                            <ul class="list-group list-group-flush">
                                {% for item in rss_items %}
                                <li class="list-group-item">
                                    {% if item.link %}
                                        <a href="{{ item.link }}" target="_blank" rel="noopener">{{ item.title }}</a>
                                    {% else %}
                                        {{ item.title }}
                                    {% endif %}
                                    {% if item.summary %}
                                        <div class="text-muted small">{{ item.summary }}</div>
                                    {% endif %}
                                    {% if item.source %}
                                        <div class="text-muted small">Forras: {{ item.source }}</div>
                                    {% endif %}
                                </li>
                                {% endfor %}
                            </ul>
                        {% else %}
                            <p class="text-muted text-center">Nincs elerheto hir.</p>
                        {% endif %}
                    </div>
                </div>
            </div>
        </div>

        <div class="row">
            <div class="col-md-6">
                <div class="card mb-4">
                    <div class="card-header bg-info text-white">
                        <h5 class="mb-0">Legutobbi meccsek</h5>
                    </div>
                    <div class="card-body">
                        <table class="table table-hover">
                            <thead>
                                <tr>
                                    <th>Hazai</th>
                                    <th>Vendeg</th>
                                    <th>Eredmeny</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for match in matches %}
                                <tr>
                                    <td>{{ match.home_team }}</td>
                                    <td>{{ match.away_team }}</td>
                                    <td><span class="badge bg-primary">{{ match.home_score }}-{{ match.away_score }}</span></td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="card mb-4">
                    <div class="card-header bg-danger text-white">
                        <h5 class="mb-0">Tabella</h5>
                    </div>
                    <div class="card-body">
                        <table class="table table-hover">
                            <thead>
                                <tr>
                                    <th>Hely</th>
                                    <th>Csapat</th>
                                    <th>Pont</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for team, points in points_table %}
                                <tr>
                                    <td>{{ loop.index }}</td>
                                    <td>{{ team }}</td>
                                    <td><span class="badge bg-success">{{ points }}</span></td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
        document.addEventListener('DOMContentLoaded', function() {
            {% if odds %}
            const ctx = document.getElementById('oddsChart').getContext('2d');
            new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: ['Hazai', 'Donto', 'Vendeg'],
                    datasets: [{
                        label: 'Probability',
                        data: [{{ odds.home_prob }}, {{ odds.draw_prob }}, {{ odds.away_prob }}],
                        backgroundColor: ['#28a745', '#ffc107', '#dc3545'],
                        borderColor: ['#28a745', '#ffc107', '#dc3545'],
                        borderWidth: 1
                    }]
                },
                options: {
                    scales: {
                        y: {
                            beginAtZero: true,
                            max: 1
                        }
                    }
                }
            });
            {% endif %}
        });
    </script>
</body>
</html>
"""

@app.route('/')
def dashboard():
    tips_requested = request.args.get("tips") == "1"
    target_odds = 2.0
    window_hours = 24

    db = connect(config.db_url)
    db.ensure_schema()
    matches = list_matches(db)
    if not matches:
        sample_matches = [
            Match("Csapat A", "Csapat B", 2, 1, "2023-01-01"),
            Match("Csapat A", "Csapat C", 1, 1, "2023-01-02"),
            Match("Csapat B", "Csapat C", 0, 3, "2023-01-03"),
        ]
        for match in sample_matches:
            try:
                add_match(db, match)
            except Exception:
                pass  # Already exists
        matches = list_matches(db)
    points_map = table(matches)
    points_table = sorted(points_map.items(), key=lambda x: x[1], reverse=True)
    form_scores = _build_form_scores(matches)
    table_scores = _build_table_scores(points_map)

    # Fetch competitions
    competitions = []
    try:
        headers = {"X-Auth-Token": config.football_data_token}
        response = requests.get("https://api.football-data.org/v4/competitions", headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            competitions = data.get('competitions', [])[:3]
    except Exception:
        pass

    # Fetch odds and tip
    odds_data = None
    target_matches = []
    rss_items: list[dict] = []
    odds_count = 0
    best_pick = None
    best_combo = None
    try:
        data = _fetch_odds_matches(config.odds_api_key)
        eligible = []
        if data:
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
            scored_candidates = []
            for match in eligible:
                scored = _score_match(match, target_odds, rss_items, db, form_scores, table_scores)
                if scored:
                    scored_candidates.append((scored["score"], match))
            picks = [
                _score_match(match, target_odds, rss_items, db, form_scores, table_scores)
                for match in eligible
            ]
            picks = [pick for pick in picks if pick]
            if picks:
                best_combo = _build_best_combo(picks, target_odds)
            if scored_candidates:
                best_match = max(scored_candidates, key=lambda item: item[0])[1]
                best_pick = _score_match(best_match, target_odds, rss_items, db, form_scores, table_scores)
                match = best_match
            else:
                match = eligible[0]
            home_team = match.get("home_team", "")
            away_team = match.get("away_team", "")
            outcomes = _extract_h2h_outcomes(match)
            home_odds = _find_price(outcomes, home_team)
            away_odds = _find_price(outcomes, away_team)
            draw_odds = _find_price(outcomes, "Draw")
            if home_odds is not None and away_odds is not None and draw_odds is not None:
                # Enhanced AI tip with stats, weather and news factors
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

                # Final scores with factors
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

            if tips_requested:
                target_matches = _select_matches_near_odds(
                    eligible, target_odds, rss_items, db, form_scores, table_scores, limit=2
                )
    except Exception:
        pass

    return render_template_string(TEMPLATE,
                                  odds_configured=bool(config.odds_api_key),
                                  football_configured=bool(config.football_data_token),
                                  competitions=competitions,
                                  odds=odds_data,
                                  best_pick=best_pick,
                                  best_combo=best_combo,
                                  odds_count=odds_count,
                                  tips_requested=tips_requested,
                                  target_matches=target_matches,
                                  matches=matches,
                                  points_table=points_table,
                                  _match_reasons=_match_reasons,
                                  rss_items=rss_items)

if __name__ == '__main__':
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug)
