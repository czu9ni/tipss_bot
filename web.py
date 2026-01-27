from flask import Flask, render_template_string, request, redirect, url_for, jsonify
from flask_compress import Compress
from soccer_bot.config import load_config
from soccer_bot.cache import DiskCache
from soccer_bot.db import connect
from soccer_bot.providers.odds_therundown import TheRundownClient
from soccer_bot.repo import Match, get_team_stats, list_matches
from soccer_bot.offline_stats import build_team_summary
from soccer_bot.scoring import match_points, score_fixture, table
from soccer_bot.utils import build_http_client
import html
import logging
import json
import os
import re
import time
import base64
import hmac
import hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import math
import traceback
import requests
import threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from zoneinfo import ZoneInfo

app = Flask(__name__)
Compress(app)

def _load_dotenv() -> None:
    env_path = os.environ.get("SOCCER_ENV_PATH") or os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="ascii") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass


_load_dotenv()

config = load_config()


def _reload_env() -> None:
    env_path = os.environ.get("SOCCER_ENV_PATH") or os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="ascii") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key:
                    os.environ[key] = value
    except Exception:
        pass


def _backtest_mode_enabled() -> bool:
    return os.environ.get("BACKTEST_MODE", "0") == "1"


def _therundown_enabled() -> bool:
    return bool(os.environ.get("RAPIDAPI_KEY") and os.environ.get("THERUNDOWN_BASE_URL") and os.environ.get("RAPIDAPI_HOST"))


def _therundown_client() -> TheRundownClient:
    logger = logging.getLogger("therundown_web")
    client = build_http_client(logger)
    return TheRundownClient(
        base_url=os.environ.get("THERUNDOWN_BASE_URL", ""),
        api_key=os.environ.get("RAPIDAPI_KEY", ""),
        api_host=os.environ.get("RAPIDAPI_HOST", ""),
        client=client,
    )


def _cache_dir() -> str:
    return os.environ.get("CACHE_DIR", os.path.join("data", "cache"))


def _therundown_sport_ids() -> list[str]:
    raw = os.environ.get("THERUNDOWN_SPORT_ID_SOCCER", "").strip()
    if not raw:
        return ["10", "11", "12", "13", "14", "15", "17"]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _therundown_sport_name(sport_id: str) -> str:
    names = {
        "10": "MLS",
        "11": "Premier League",
        "12": "Ligue 1",
        "13": "Bundesliga",
        "14": "LaLiga",
        "15": "Serie A",
        "17": "UEFA Europa/Champions",
    }
    return names.get(str(sport_id), f"Sport {sport_id}")


def _therundown_event_key(date_str: str, home: str, away: str) -> str:
    return f"{date_str}|{_normalize_team_name(home)}|{_normalize_team_name(away)}"


def _therundown_markets_to_bookmakers(markets: dict[str, dict], home: str, away: str) -> list[dict]:
    if not markets:
        return []
    bookmaker = {"key": "therundown", "title": "TheRundown", "markets": []}
    h2h = markets.get("1x2") or {}
    dc_markets: dict[str, float] = {}
    if h2h:
        outcomes = []
        home_odds = float(h2h["home"]) if isinstance(h2h.get("home"), (int, float)) else None
        draw_odds = float(h2h["draw"]) if isinstance(h2h.get("draw"), (int, float)) else None
        away_odds = float(h2h["away"]) if isinstance(h2h.get("away"), (int, float)) else None
        if home_odds:
            outcomes.append({"name": home, "price": home_odds})
        if draw_odds:
            outcomes.append({"name": "Draw", "price": draw_odds})
        if away_odds:
            outcomes.append({"name": away, "price": away_odds})
        if home_odds and draw_odds:
            dc_markets["1x"] = round(1 / ((1 / home_odds) + (1 / draw_odds)), 2)
        if away_odds and draw_odds:
            dc_markets["x2"] = round(1 / ((1 / away_odds) + (1 / draw_odds)), 2)
        if home_odds and away_odds:
            dc_markets["12"] = round(1 / ((1 / home_odds) + (1 / away_odds)), 2)
        if outcomes:
            bookmaker["markets"].append({"key": "h2h", "outcomes": outcomes})
    totals = markets.get("over_under") or {}
    if totals:
        outcomes = []
        if isinstance(totals.get("over_2.5"), (int, float)):
            outcomes.append({"name": "Over", "point": 2.5, "price": float(totals["over_2.5"])})
        if isinstance(totals.get("under_2.5"), (int, float)):
            outcomes.append({"name": "Under", "point": 2.5, "price": float(totals["under_2.5"])})
        if outcomes:
            bookmaker["markets"].append({"key": "totals", "outcomes": outcomes})
    btts = markets.get("btts") or {}
    if btts:
        outcomes = []
        if isinstance(btts.get("yes"), (int, float)):
            outcomes.append({"name": "Yes", "price": float(btts["yes"])})
        if isinstance(btts.get("no"), (int, float)):
            outcomes.append({"name": "No", "price": float(btts["no"])})
        if outcomes:
            bookmaker["markets"].append({"key": "btts", "outcomes": outcomes})
    dc = markets.get("double_chance") or {}
    if dc:
        outcomes = []
        if isinstance(dc.get("1x"), (int, float)):
            outcomes.append({"name": "1X", "price": float(dc["1x"])})
        if isinstance(dc.get("x2"), (int, float)):
            outcomes.append({"name": "X2", "price": float(dc["x2"])})
        if isinstance(dc.get("12"), (int, float)):
            outcomes.append({"name": "12", "price": float(dc["12"])})
        if outcomes:
            bookmaker["markets"].append({"key": "double_chance", "outcomes": outcomes})
    elif dc_markets:
        outcomes = []
        if isinstance(dc_markets.get("1x"), (int, float)):
            outcomes.append({"name": "1X", "price": float(dc_markets["1x"])})
        if isinstance(dc_markets.get("x2"), (int, float)):
            outcomes.append({"name": "X2", "price": float(dc_markets["x2"])})
        if isinstance(dc_markets.get("12"), (int, float)):
            outcomes.append({"name": "12", "price": float(dc_markets["12"])})
        if outcomes:
            bookmaker["markets"].append({"key": "double_chance", "outcomes": outcomes})
    if bookmaker["markets"]:
        return [bookmaker]
    return []


def _token_set(name: str) -> set[str]:
    return {tok for tok in _normalize_team_name(name).split(" ") if tok}


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


def _best_fixture_match(home: str, away: str, fixtures: list[dict]) -> dict | None:
    home_tokens = _token_set(home)
    away_tokens = _token_set(away)
    best = None
    best_score = 0.0
    for item in fixtures:
        cand_home = _token_set(str(item.get("home_team") or ""))
        cand_away = _token_set(str(item.get("away_team") or ""))
        if not cand_home or not cand_away:
            continue
        score_same = (_similarity(home_tokens, cand_home) + _similarity(away_tokens, cand_away)) / 2.0
        score_swap = (_similarity(home_tokens, cand_away) + _similarity(away_tokens, cand_home)) / 2.0
        score = max(score_same, score_swap)
        if score > best_score:
            best_score = score
            best = item
    return best if best_score >= 0.6 else None


def _therundown_update_match_odds(match: dict, line_id: str, client: TheRundownClient) -> None:
    if not line_id:
        return
    markets: dict[str, dict] = {}
    try:
        moneyline = client.moneyline(line_id)
        markets.update(client.markets_from_moneyline(moneyline))
    except Exception:
        pass
    try:
        totals = client.totals(line_id)
        markets.update(client.markets_from_totals(totals))
    except Exception:
        pass
    try:
        spread = client.spread(line_id)
        markets.update(client.markets_from_spread(spread))
    except Exception:
        pass
    if markets:
        match["therundown_markets"] = markets
        match["bookmakers"] = _therundown_markets_to_bookmakers(
            markets,
            match.get("home_team", ""),
            match.get("away_team", ""),
        )

BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
BASIC_AUTH_PASSWORD = os.environ.get("BASIC_AUTH_PASSWORD", "")
ALLOWED_IPS = {ip.strip() for ip in os.environ.get("ALLOWED_IPS", "127.0.0.1,::1").split(",") if ip.strip()}
FAST_MODE = os.environ.get("FAST_MODE", "0") == "1"

BUDAPEST_TZ = ZoneInfo("Europe/Budapest")

def _parse_match_dt(match: dict) -> datetime | None:
    dt_raw = (
        match.get("utc")
        or match.get("kickoff_utc")
        or match.get("date_utc")
        or match.get("datetime")
        or match.get("date")
        or match.get("commence_time")
    )
    if not dt_raw:
        return None

    s = str(dt_raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s + "T00:00:00")
        except Exception:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BUDAPEST_TZ)

    return dt


def _within_next_24h(match_dt: datetime, now: datetime) -> bool:
    return now <= match_dt < (now + timedelta(hours=24))
AUTO_REFRESH_SECONDS = int(os.environ.get("AUTO_REFRESH_SECONDS", "900"))
TRANSLATE_API_URL = os.environ.get("TRANSLATE_API_URL", "").strip()
TRANSLATE_API_KEY = os.environ.get("TRANSLATE_API_KEY", "").strip()
TRANSLATE_SOURCE = os.environ.get("TRANSLATE_SOURCE", "auto").strip()
TRANSLATE_TARGET = os.environ.get("TRANSLATE_TARGET", "hu").strip()
TRANSLATE_TIMEOUT_SECONDS = float(os.environ.get("TRANSLATE_TIMEOUT_SECONDS", "4"))
AF_STATS_ENABLED = os.environ.get("AF_STATS_ENABLED", "1") == "1"
STAT_ONLY_DC_CAP = float(os.environ.get("STAT_ONLY_DC_CAP", "0.70"))
RISK_EXCLUDE_CUP = os.environ.get("RISK_EXCLUDE_CUP", "1") == "1"
RISK_EXCLUDE_DERBY = os.environ.get("RISK_EXCLUDE_DERBY", "1") == "1"
RISK_EXCLUDE_ROTATION = os.environ.get("RISK_EXCLUDE_ROTATION", "1") == "1"
MIN_VALUE_EDGE = float(os.environ.get("MIN_VALUE_EDGE", "0.02"))
MIN_EV = float(os.environ.get("MIN_EV", "0.0"))
MIN_MODEL_PROB = float(os.environ.get("MIN_MODEL_PROB", "0.35"))
ODDS_MIN = float(os.environ.get("ODDS_MIN", "1.6") or 0)
ODDS_MAX = float(os.environ.get("ODDS_MAX", "0") or 0)
MARKET_ALLOWLIST = {
    item.strip()
    for item in os.environ.get("MARKET_ALLOWLIST", "").split(",")
    if item.strip()
}

def _build_http_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


_HTTP = _build_http_session()
_REMOTE_CALLS = 0
_REMOTE_CALLS_LIMIT = int(os.environ.get("MAX_REMOTE_CALLS", "25") or 25)

RSS_FEEDS = [
    {"url": "http://feeds.bbci.co.uk/sport/football/rss.xml", "weight": 0.9, "label": "BBC Football"},
    {"url": "https://www.skysports.com/rss/12040", "weight": 0.8, "label": "Sky Sports Football"},
    {"url": "https://www.espn.com/espn/rss/soccer/news", "weight": 0.8, "label": "ESPN Soccer"},
    {"url": "https://www.theguardian.com/football/rss", "weight": 0.8, "label": "Guardian Football"},
]
RSS_CACHE_TTL_SECONDS = 600
_RSS_CACHE: dict[str, object] = {"fetched_at": 0.0, "items": []}
_DATA_DIR = Path(os.path.dirname(__file__)) / "data"
_XG_INDEX: dict[str, dict] | None = None
_INJURY_RECORDS: dict[str, list[dict]] | None = None
_RSS_FEED_LIST: list[dict] | None = None
_OFFLINE_FIXTURES_CACHE: list[dict] | None = None
_GEOCODE_CACHE: dict[str, dict] = {}
_SERVER_TIME_CACHE = {"ts": 0.0, "value": None}
_SERVER_TIME_SOURCE = "system"
_LAST_WINDOW_INFO = {"from": "", "to": "", "source": "system"}
_API_USAGE_PATH = _DATA_DIR / "api_usage.json"
_API_USAGE_LOCK = threading.Lock()
_API_USAGE: dict[str, dict] | None = None
_API_HEADERS: dict[str, dict] = {}

API_LIMITS = {
    "odds": int(os.environ.get("ODDS_API_MONTHLY_LIMIT", "0") or "0") or None,
    "football_data": int(os.environ.get("FOOTBALL_DATA_MONTHLY_LIMIT", "0") or "0") or None,
    "api_football": int(os.environ.get("API_FOOTBALL_MONTHLY_LIMIT", "0") or "0") or None,
    "sportradar": int(os.environ.get("SPORTRADAR_MONTHLY_LIMIT", "0") or "0") or None,
    "open_meteo": int(os.environ.get("OPEN_METEO_MONTHLY_LIMIT", "0") or "0") or None,
    "rss": int(os.environ.get("RSS_MONTHLY_LIMIT", "0") or "0") or None,
    "translate": int(os.environ.get("TRANSLATE_MONTHLY_LIMIT", "0") or "0") or None,
    "clubelo": int(os.environ.get("CLUBELO_MONTHLY_LIMIT", "0") or "0") or None,
}


def _api_month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _api_day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_api_usage() -> dict:
    global _API_USAGE
    if _API_USAGE is not None:
        return _API_USAGE
    data = {}
    try:
        if _API_USAGE_PATH.exists():
            with open(_API_USAGE_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if "months" not in data:
        months = {k: v for k, v in data.items() if isinstance(k, str) and re.match(r"\d{4}-\d{2}$", k)}
        days = data.get("days", {}) if isinstance(data.get("days", {}), dict) else {}
        data = {"months": months, "days": days}
    if "days" not in data or not isinstance(data.get("days"), dict):
        data["days"] = {}
    _API_USAGE = data
    return data


def _save_api_usage(data: dict) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(_API_USAGE_PATH, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=True, indent=2)
    except Exception:
        pass


def _note_api_call(api_name: str, count: int = 1) -> None:
    if not api_name:
        return
    month_key = _api_month_key()
    day_key = _api_day_key()
    with _API_USAGE_LOCK:
        data = _load_api_usage()
        months = data.get("months", {})
        days = data.get("days", {})
        month = months.get(month_key)
        if not isinstance(month, dict):
            month = {}
        usage = month.get(api_name)
        if not isinstance(usage, dict):
            usage = {"count": 0}
        usage["count"] = int(usage.get("count", 0) or 0) + int(count)
        month[api_name] = usage
        months[month_key] = month
        day = days.get(day_key)
        if not isinstance(day, dict):
            day = {}
        daily = day.get(api_name)
        if not isinstance(daily, dict):
            daily = {"count": 0}
        daily["count"] = int(daily.get("count", 0) or 0) + int(count)
        day[api_name] = daily
        days[day_key] = day
        data["months"] = months
        data["days"] = days
        _save_api_usage(data)


def _api_quota_snapshot() -> dict[str, dict]:
    month_key = _api_month_key()
    day_key = _api_day_key()
    data = _load_api_usage()
    months = data.get("months", {}) if isinstance(data, dict) else {}
    days = data.get("days", {}) if isinstance(data, dict) else {}
    month = months.get(month_key, {}) if isinstance(months, dict) else {}
    day = days.get(day_key, {}) if isinstance(days, dict) else {}
    snapshot = {}
    for key, limit in API_LIMITS.items():
        used = 0
        used_today = 0
        if isinstance(month, dict):
            entry = month.get(key)
            if isinstance(entry, dict):
                used = int(entry.get("count", 0) or 0)
        if isinstance(day, dict):
            entry = day.get(key)
            if isinstance(entry, dict):
                used_today = int(entry.get("count", 0) or 0)
        remaining = None if limit is None else max(limit - used, 0)
        snapshot[key] = {
            "used": used,
            "used_today": used_today,
            "limit": limit,
            "remaining": remaining,
            "header": _API_HEADERS.get(key, {}),
        }
    return snapshot


def _api_usage_counts() -> tuple[dict[str, int], dict[str, int]]:
    month_key = _api_month_key()
    day_key = _api_day_key()
    data = _load_api_usage()
    months = data.get("months", {}) if isinstance(data, dict) else {}
    days = data.get("days", {}) if isinstance(data, dict) else {}
    month = months.get(month_key, {}) if isinstance(months, dict) else {}
    day = days.get(day_key, {}) if isinstance(days, dict) else {}
    month_counts = {}
    day_counts = {}
    if isinstance(month, dict):
        for key, entry in month.items():
            if isinstance(entry, dict):
                month_counts[key] = int(entry.get("count", 0) or 0)
    if isinstance(day, dict):
        for key, entry in day.items():
            if isinstance(entry, dict):
                day_counts[key] = int(entry.get("count", 0) or 0)
    return month_counts, day_counts


def _api_name_from_url(url: str) -> str | None:
    url = url.lower()
    if "api.football-data.org" in url:
        return "football_data"
    if "v3.football.api-sports.io" in url:
        return "api_football"
    if "api.the-odds-api.com" in url:
        return "odds"
    if "api.sportradar" in url:
        return "sportradar"
    if "api.open-meteo.com" in url or "geocoding-api.open-meteo.com" in url:
        return "open_meteo"
    if "translate.googleapis.com" in url:
        return "translate"
    if "api.clubelo.com" in url:
        return "clubelo"
    if TRANSLATE_API_URL and TRANSLATE_API_URL.lower() in url:
        return "translate"
    return None


def _reset_sportradar_budget() -> None:
    with _SR_REFRESH_LOCK:
        _SR_REFRESH_BUDGET["event"] = SR_MAX_EVENT_CALLS
        _SR_REFRESH_BUDGET["player"] = SR_MAX_PLAYER_CALLS
        _SR_REFRESH_BUDGET["live"] = SR_MAX_LIVE_CALLS
        _SR_REFRESH_BUDGET["mapping"] = SR_MAX_MAPPING_CALLS
        _SR_REFRESH_BUDGET["push"] = SR_MAX_PUSH_CALLS
        _SR_REFRESH_BUDGET["prob"] = SR_MAX_PROB_CALLS


def _take_sportradar_budget(kind: str, count: int = 1) -> bool:
    with _SR_REFRESH_LOCK:
        remaining = _SR_REFRESH_BUDGET.get(kind, 0)
        if remaining < count:
            return False
        _SR_REFRESH_BUDGET[kind] = remaining - count
        return True


def _sr_base_url() -> str:
    return (config.sportradar_api_base or "https://api.sportradar.com/soccer/trial/v4/en").rstrip("/")


def _sr_prob_base_url() -> str:
    return os.environ.get(
        "SPORTRADAR_PROB_API_BASE",
        "https://api.sportradar.com/soccer-probabilities/trial/v4/en",
    ).rstrip("/")


def _sr_get(
    path: str,
    timeout: float = 12,
    params: dict | None = None,
    allow_redirects: bool = True,
) -> requests.Response | None:
    if not config.sportradar_api_key:
        return None
    url = f"{_sr_base_url()}/{path.lstrip('/')}"
    try:
        return _http_get(
            url,
            headers={"x-api-key": config.sportradar_api_key, "accept": "application/json"},
            params=params,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )
    except Exception:
        return None


def _sr_prob_get(
    path: str,
    timeout: float = 12,
    params: dict | None = None,
    allow_redirects: bool = True,
) -> requests.Response | None:
    if not config.sportradar_api_key:
        return None
    url = f"{_sr_prob_base_url()}/{path.lstrip('/')}"
    try:
        return _http_get(
            url,
            headers={"x-api-key": config.sportradar_api_key, "accept": "application/json"},
            params=params,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )
    except Exception:
        return None


def _parse_int_header(headers: dict, *keys: str) -> int | None:
    for key in keys:
        value = headers.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except Exception:
            continue
    return None


def _update_api_headers(api_name: str, headers: dict) -> None:
    if not api_name or not headers:
        return
    lower = {str(k).lower(): v for k, v in headers.items()}
    info = {"ts": time.time()}
    if api_name == "odds":
        remaining = _parse_int_header(lower, "x-requests-remaining")
        used = _parse_int_header(lower, "x-requests-used")
        limit = _parse_int_header(lower, "x-requests-limit")
        if limit is None and remaining is not None and used is not None:
            limit = remaining + used
        info.update({"remaining": remaining, "used": used, "limit": limit, "window": "keret"})
    elif api_name == "football_data":
        remaining_day = _parse_int_header(lower, "x-requests-available-day")
        remaining_min = _parse_int_header(lower, "x-requests-available-minute")
        info.update(
            {
                "remaining": remaining_day if remaining_day is not None else remaining_min,
                "limit": None,
                "window": "nap" if remaining_day is not None else "perc",
            }
        )
    elif api_name == "api_football":
        remaining = _parse_int_header(lower, "x-ratelimit-requests-remaining", "x-ratelimit-remaining")
        limit = _parse_int_header(lower, "x-ratelimit-requests-limit", "x-ratelimit-limit")
        info.update({"remaining": remaining, "limit": limit, "window": "keret"})
    elif api_name == "sportradar":
        remaining = _parse_int_header(lower, "x-ratelimit-remaining", "x-ratelimit-requests-remaining")
        limit = _parse_int_header(lower, "x-ratelimit-limit", "x-ratelimit-requests-limit")
        info.update({"remaining": remaining, "limit": limit, "window": "keret"})
    else:
        return
    _API_HEADERS[api_name] = info


def _http_get(url: str, api: str | None = None, **kwargs) -> requests.Response:
    global _REMOTE_CALLS
    if _REMOTE_CALLS_LIMIT > 0:
        _REMOTE_CALLS += 1
        if _REMOTE_CALLS > _REMOTE_CALLS_LIMIT:
            raise RuntimeError("remote call limit reached")
    api_name = api or _api_name_from_url(url)
    if api_name:
        _note_api_call(api_name)
    response = _HTTP.get(url, **kwargs)
    if api_name:
        _update_api_headers(api_name, response.headers)
    return response


def _server_time_utc() -> datetime | None:
    if _SERVER_TIME_CACHE["value"] and (time.time() - _SERVER_TIME_CACHE["ts"]) < 3600:
        return _SERVER_TIME_CACHE["value"]
    global _SERVER_TIME_SOURCE
    key = config.api_football_key
    if key:
        try:
            response = _http_get(
                "https://v3.football.api-sports.io/status",
                headers={"x-apisports-key": key},
                timeout=10,
            )
            if response.status_code == 200:
                data = response.json()
                resp = data.get("response", {})
                raw_time = resp.get("time") or resp.get("current") or resp.get("timestamp")
                if isinstance(raw_time, (int, float)):
                    value = datetime.fromtimestamp(raw_time, tz=timezone.utc)
                elif isinstance(raw_time, str):
                    value = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=timezone.utc)
                else:
                    value = None
                if value:
                    _SERVER_TIME_CACHE["ts"] = time.time()
                    _SERVER_TIME_CACHE["value"] = value
                    _SERVER_TIME_SOURCE = "api-football"
                    return value
        except Exception:
            pass

    token = config.football_data_token
    if token:
        try:
            response = _http_get(
                "https://api.football-data.org/v4/competitions",
                headers={"X-Auth-Token": token},
                timeout=10,
            )
            if response.status_code == 200:
                date_header = response.headers.get("Date")
                if date_header:
                    value = parsedate_to_datetime(date_header)
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=timezone.utc)
                    value = value.astimezone(timezone.utc)
                    _SERVER_TIME_CACHE["ts"] = time.time()
                    _SERVER_TIME_CACHE["value"] = value
                    _SERVER_TIME_SOURCE = "football-data"
                    return value
        except Exception:
            pass

    try:
        response = _http_get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": 0, "longitude": 0, "current": "temperature_2m"},
            timeout=10,
        )
        if response.status_code == 200:
            date_header = response.headers.get("Date")
            if date_header:
                value = parsedate_to_datetime(date_header)
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                value = value.astimezone(timezone.utc)
                _SERVER_TIME_CACHE["ts"] = time.time()
                _SERVER_TIME_CACHE["value"] = value
                _SERVER_TIME_SOURCE = "open-meteo"
                return value
    except Exception:
        pass

    return None


_WEATHER_CACHE: dict[str, dict] = {}
_WEIGHTS_CACHE: dict[str, float] | None = None
_RIVALRIES_CACHE: list[tuple[str, str]] | None = None
_TEAM_LOCATION_OVERRIDES: dict[str, dict] | None = None
_SPORTS_CACHE: dict[str, object] = {"fetched_at": 0.0, "keys": []}
_ODDS_LAST_ERROR: str | None = None
_MARKET_ROI_CACHE: dict[str, object] = {"ts": 0.0, "data": {}}
_CACHE_KEY = "latest_picks"
_CACHE_RESET_LOCK = threading.Lock()
SPORTS_CACHE_TTL_SECONDS = 3600
FD_COMP_TTL_SECONDS = int(os.environ.get("FD_COMP_TTL_SECONDS", "21600"))
FD_RECENT_TTL_SECONDS = int(os.environ.get("FD_RECENT_TTL_SECONDS", "600"))
FD_STANDINGS_TTL_SECONDS = int(os.environ.get("FD_STANDINGS_TTL_SECONDS", "1800"))
FD_FIXTURES_TTL_SECONDS = int(os.environ.get("FD_FIXTURES_TTL_SECONDS", "120"))
AF_FIXTURES_TTL_SECONDS = int(os.environ.get("AF_FIXTURES_TTL_SECONDS", "120"))
SR_FIXTURES_TTL_SECONDS = int(os.environ.get("SR_FIXTURES_TTL_SECONDS", "300"))
SR_PROB_TTL_SECONDS = int(os.environ.get("SR_PROB_TTL_SECONDS", "900"))
SR_MAX_FIXTURES = int(os.environ.get("SR_MAX_FIXTURES", "200"))
SPORTRADAR_SCHEDULE_PATH = os.environ.get("SPORTRADAR_SCHEDULE_PATH", "schedules/{date}/schedules.json").strip()
AF_STATS_TTL_SECONDS = int(os.environ.get("AF_STATS_TTL_SECONDS", "21600"))
ODDS_CACHE_TTL_SECONDS = int(os.environ.get("ODDS_CACHE_TTL_SECONDS", "90"))
REFRESH_COOLDOWN_SECONDS = int(os.environ.get("REFRESH_COOLDOWN_SECONDS", "30"))
STAT_ONLY_MAX_FIXTURES = int(os.environ.get("STAT_ONLY_MAX_FIXTURES", "80"))
TEAM_PPG_TTL_SECONDS = int(os.environ.get("TEAM_PPG_TTL_SECONDS", "3600"))
TEAM_SUMMARY_TTL_SECONDS = int(os.environ.get("TEAM_SUMMARY_TTL_SECONDS", "21600"))
_ELO_CACHE: dict[str, float] = {}
_TEAM_ID_MAP: dict[str, int] | None = None
_FORM_CACHE: dict[int, dict[str, float]] = {}
_FORM_CACHE_TTL_SECONDS = 3600
_STANDINGS_CACHE: dict[str, list[dict]] = {}
_TEAM_SEASON_HINT: dict[str, str] = {}
_FD_COMP_CACHE: dict[str, dict[str, object]] = {}
_FD_RECENT_CACHE: dict[str, dict[str, object]] = {}
_FD_STANDINGS_CACHE: dict[str, dict[str, object]] = {}
_FD_FIXTURES_CACHE: dict[str, dict[str, object]] = {}
_FDCO_TABLE_CACHE: dict[str, dict[str, object]] = {}
_AF_FIXTURES_CACHE: dict[str, dict[str, object]] = {}
_SR_FIXTURES_CACHE: dict[str, dict[str, object]] = {}
_SR_PROB_CACHE: dict[str, dict[str, object]] = {}
_ODDS_CACHE: dict[str, object] = {"ts": 0.0, "matches": [], "error": None}
_TEAM_PPG_CACHE: dict[int, dict[str, object]] = {}
_TEAM_SUMMARY_CACHE: dict[str, dict[str, object]] = {}
_TEAM_RECENT_CACHE: dict[str, dict[str, object]] = {}
_TRANSLATE_CACHE: dict[str, dict[str, object]] = {}
_TEAM_ID_CACHE: dict[str, dict[str, object]] = {}
_FIXTURE_STATS_CACHE: dict[str, dict[str, object]] = {}
_TEAM_SQUAD_CACHE: dict[str, dict[str, object]] = {}
_SR_STANDINGS_CACHE: dict[str, dict[str, object]] = {}
_SR_STATS_CACHE: dict[str, dict[str, object]] = {}
_SR_EVENT_CACHE: dict[str, dict[str, object]] = {}
_SR_COMP_PROFILE_CACHE: dict[str, dict[str, object]] = {}
_SR_COMP_SUMMARY_CACHE: dict[str, dict[str, object]] = {}
_SR_PLAYER_PROFILE_CACHE: dict[str, dict[str, object]] = {}
_SR_PLAYER_SUMMARY_CACHE: dict[str, dict[str, object]] = {}
_SR_MAPPING_CACHE: dict[str, dict[str, object]] = {}
_SR_LIVE_CACHE: dict[str, dict[str, object]] = {}
_SR_REFRESH_BUDGET: dict[str, int] = {"event": 0, "player": 0, "live": 0, "mapping": 0, "push": 0, "prob": 0}
_SR_REFRESH_LOCK = threading.Lock()
_ODDS_MARKETS_DEFAULT = "h2h,totals,btts,team_totals,spreads,draw_no_bet,double_chance,alternate_totals,alternate_team_totals,alternate_spreads"
BACKGROUND_REFRESH_SECONDS = int(os.environ.get("BACKGROUND_REFRESH_SECONDS", "600"))
OFFLINE_FIXTURES_TTL_SECONDS = int(os.environ.get("OFFLINE_FIXTURES_TTL_SECONDS", "3600"))
TEAM_SQUAD_TTL_SECONDS = int(os.environ.get("TEAM_SQUAD_TTL_SECONDS", "2592000"))
TEAM_SQUAD_MAX_FETCH_PER_MONTH = int(os.environ.get("TEAM_SQUAD_MAX_FETCH_PER_MONTH", "30"))
SR_STANDINGS_TTL_SECONDS = int(os.environ.get("SR_STANDINGS_TTL_SECONDS", "3600"))
SR_STATS_TTL_SECONDS = int(os.environ.get("SR_STATS_TTL_SECONDS", "21600"))
SR_EVENT_TTL_SECONDS = int(os.environ.get("SR_EVENT_TTL_SECONDS", "900"))
SR_COMP_PROFILE_TTL_SECONDS = int(os.environ.get("SR_COMP_PROFILE_TTL_SECONDS", "86400"))
SR_COMP_SUMMARY_TTL_SECONDS = int(os.environ.get("SR_COMP_SUMMARY_TTL_SECONDS", "86400"))
SR_PLAYER_PROFILE_TTL_SECONDS = int(os.environ.get("SR_PLAYER_PROFILE_TTL_SECONDS", "86400"))
SR_PLAYER_SUMMARY_TTL_SECONDS = int(os.environ.get("SR_PLAYER_SUMMARY_TTL_SECONDS", "86400"))
SR_MAPPING_TTL_SECONDS = int(os.environ.get("SR_MAPPING_TTL_SECONDS", "2592000"))
SR_LIVE_TTL_SECONDS = int(os.environ.get("SR_LIVE_TTL_SECONDS", "60"))
SR_ENABLE_EVENT = os.environ.get("SR_ENABLE_EVENT", "0") == "1"
SR_ENABLE_PLAYER = os.environ.get("SR_ENABLE_PLAYER", "1") == "1"
SR_ENABLE_LIVE = os.environ.get("SR_ENABLE_LIVE", "0") == "1"
SR_ENABLE_MAPPING = os.environ.get("SR_ENABLE_MAPPING", "0") == "1"
SR_ENABLE_PUSH = os.environ.get("SR_ENABLE_PUSH", "0") == "1"
SR_ENABLE_PROB = os.environ.get("SR_ENABLE_PROB", "1") == "1"
SR_MAX_EVENT_CALLS = int(os.environ.get("SR_MAX_EVENT_CALLS", "1"))
SR_MAX_PLAYER_CALLS = int(os.environ.get("SR_MAX_PLAYER_CALLS", "2"))
SR_MAX_LIVE_CALLS = int(os.environ.get("SR_MAX_LIVE_CALLS", "1"))
SR_MAX_MAPPING_CALLS = int(os.environ.get("SR_MAX_MAPPING_CALLS", "1"))
SR_MAX_PUSH_CALLS = int(os.environ.get("SR_MAX_PUSH_CALLS", "1"))
SR_MAX_PROB_CALLS = int(os.environ.get("SR_MAX_PROB_CALLS", "1"))

_RESPONSE_CACHE: dict[str, object] = {"html": "", "ts": 0.0}
_RESPONSE_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()
_REFRESH_IN_FLIGHT = False
_API_BACKOFF: dict[str, float] = {}
RESPONSE_CACHE_TTL = int(os.environ.get("RESPONSE_CACHE_TTL", "30"))
RSS_REFRESH_SECONDS = int(os.environ.get("RSS_REFRESH_SECONDS", "300"))
STANDINGS_REFRESH_SECONDS = int(os.environ.get("STANDINGS_REFRESH_SECONDS", "120"))
ODDS_REFRESH_SECONDS = int(os.environ.get("ODDS_REFRESH_SECONDS", "60"))
MIN_API_REFRESH_SECONDS = int(os.environ.get("MIN_API_REFRESH_SECONDS", "86400"))
API_BACKOFF_SECONDS = int(os.environ.get("API_BACKOFF_SECONDS", "86400"))
_TEAM_SQUAD_CACHE_FILE = Path(os.path.dirname(__file__)) / "data" / "team_squads_cache.json"
_LAST_RSS_FETCH: float = 0.0
_LAST_STANDINGS_FETCH: float = 0.0
_LAST_ODDS_FETCH: float = 0.0
_LAST_RENDER_HASH: str | None = None
_LAST_RENDER_TS: float = 0.0
_LAST_PAYLOAD: dict | None = None


def _reset_runtime_caches() -> None:
    with _CACHE_RESET_LOCK:
        _ODDS_CACHE["ts"] = 0.0
        _ODDS_CACHE["matches"] = []
        _ODDS_CACHE["error"] = None
        _RSS_CACHE["fetched_at"] = 0.0
        _RSS_CACHE["items"] = []
        _SPORTS_CACHE["fetched_at"] = 0.0
        _SPORTS_CACHE["keys"] = []
        _LOCAL_DB_CACHE["matches"] = None
        _LOCAL_DB_CACHE["standings"] = None


def _backoff_active(key: str) -> bool:
    ts = _API_BACKOFF.get(key)
    return bool(ts and (time.time() - ts) < API_BACKOFF_SECONDS)


def _note_backoff(key: str) -> None:
    _API_BACKOFF[key] = time.time()


def _load_team_squad_cache() -> None:
    global _TEAM_SQUAD_CACHE
    if _TEAM_SQUAD_CACHE:
        return
    try:
        if _TEAM_SQUAD_CACHE_FILE.exists():
            data = json.loads(_TEAM_SQUAD_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _TEAM_SQUAD_CACHE = data
            else:
                _TEAM_SQUAD_CACHE = {}
        else:
            _TEAM_SQUAD_CACHE = {}
    except Exception:
        _TEAM_SQUAD_CACHE = {}


def _save_team_squad_cache() -> None:
    try:
        _TEAM_SQUAD_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TEAM_SQUAD_CACHE_FILE.write_text(json.dumps(_TEAM_SQUAD_CACHE, ensure_ascii=True), encoding="utf-8")
    except Exception:
        pass


def _squad_fetch_allowed() -> bool:
    _load_team_squad_cache()
    meta = _TEAM_SQUAD_CACHE.get("_meta", {})
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if not isinstance(meta, dict) or meta.get("month") != month:
        _TEAM_SQUAD_CACHE["_meta"] = {"month": month, "count": 0}
        _save_team_squad_cache()
        return True
    try:
        count = int(meta.get("count", 0))
    except Exception:
        count = 0
    return count < TEAM_SQUAD_MAX_FETCH_PER_MONTH


def _note_squad_fetch() -> None:
    meta = _TEAM_SQUAD_CACHE.get("_meta")
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if not isinstance(meta, dict) or meta.get("month") != month:
        meta = {"month": month, "count": 0}
    try:
        meta["count"] = int(meta.get("count", 0)) + 1
    except Exception:
        meta["count"] = 1
    _TEAM_SQUAD_CACHE["_meta"] = meta
    _save_team_squad_cache()
RSS_SOURCES_FILE = os.path.join(os.path.dirname(__file__), "data", "rss_sources.json")
RSS_EXTRA_SOURCES_FILE = os.path.join(os.path.dirname(__file__), "data", "rss_sources_extra.json")
CLUB_RSS_MAP_FILE = os.path.join(os.path.dirname(__file__), "data", "club_rss_map.json")
OFFLINE_FIXTURES_FILE = os.path.join(os.path.dirname(__file__), "data", "offline_fixtures.json")
LOCAL_DB_TTL_SECONDS = int(os.environ.get("LOCAL_DB_TTL_SECONDS", "300"))
_LOCAL_DB_CACHE: dict[str, object] = {"matches": None, "standings": None, "ts": 0.0}
_OFFLINE_FIXTURES_CACHE: dict[str, object] = {"fixtures": None, "ts": 0.0}
_PICK_MIN_ODDS = float(os.environ.get("PICK_MIN_ODDS", "0"))
_PICK_MAX_ODDS = float(os.environ.get("PICK_MAX_ODDS", "0"))
_SERVER_STARTED_AT = datetime.now(timezone.utc)
_TEAM_COMP_HINT: dict[str, str] = {}
FDCO_UK_TTL_SECONDS = int(os.environ.get("FDCO_UK_TTL_SECONDS", "21600"))
FDCO_UK_LEAGUE_MAP = {
    "PL": "E0",
    "ELC": "E1",
    "PD": "SP1",
    "SA": "I1",
    "BL1": "D1",
    "FL1": "F1",
    "DED": "N1",
    "PPL": "P1",
}
FDCO_UK_COMP_NAMES = {
    "E0": "Premier League",
    "E1": "Championship",
    "SP1": "Primera Division",
    "I1": "Serie A",
    "D1": "Bundesliga",
    "F1": "Ligue 1",
    "N1": "Eredivisie",
    "P1": "Primeira Liga",
}
FDCO_UK_COMP_COLUMNS = {
    "team": ("Team", "team"),
    "position": ("Pos", "Position", "Ranking"),
    "points": ("Pts", "Points"),
    "played": ("P", "Played"),
    "won": ("W", "Won"),
    "draw": ("D", "Draw"),
    "lost": ("L", "Lost"),
    "goals_for": ("F", "GoalsFor"),
    "goals_against": ("A", "GoalsAgainst"),
}

_COMP_NAME_TO_FD_CODE = {
    "premier league": "PL",
    "championship": "ELC",
    "english championship": "ELC",
    "la liga": "PD",
    "laliga": "PD",
    "primera division": "PD",
    "serie a": "SA",
    "bundesliga": "BL1",
    "ligue 1": "FL1",
    "eredivisie": "DED",
    "primeira liga": "PPL",
}


def _comp_name_to_fd_code(name: str | None) -> str | None:
    if not name:
        return None
    key = re.sub(r"[^a-z0-9\\s]", " ", str(name).lower()).strip()
    key = re.sub(r"\\s+", " ", key)
    if not key:
        return None
    return _COMP_NAME_TO_FD_CODE.get(key)

_TEMPLATE_PATHS = [
    os.path.join(os.path.dirname(__file__), "data", "ui_template.html"),
    os.path.join(os.path.dirname(__file__), "data", "innovative_dashboard.html"),
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


def _get_template() -> str:
    return _load_template()


def _is_authorized() -> bool:
    if not BASIC_AUTH_USER or not BASIC_AUTH_PASSWORD:
        return True
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
    except Exception:
        return False
    if ":" not in decoded:
        return False
    user, password = decoded.split(":", 1)
    user_ok = hmac.compare_digest(user, BASIC_AUTH_USER)
    pass_ok = hmac.compare_digest(password, BASIC_AUTH_PASSWORD)
    return user_ok and pass_ok


def _client_ip() -> str:
    return request.remote_addr or ""


@app.before_request
def _security_gate() -> object:
    if ALLOWED_IPS and _client_ip() not in ALLOWED_IPS:
        return ("Forbidden", 403)
    if not _is_authorized():
        return ("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="tipss_bot"'})
    return None

def _cache_get(cache: dict[str, dict[str, object]], key: str, ttl: int) -> object | None:
    item = cache.get(key)
    if not item:
        return None
    if time.time() - float(item.get("ts", 0.0)) < ttl:
        return item.get("data")
    return None


def _cache_set(cache: dict[str, dict[str, object]], key: str, data: object) -> None:
    cache[key] = {"ts": time.time(), "data": data}


def _parse_iso_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, fmt)
            except Exception:
                continue
    return None


def _local_db_matches() -> list[Match]:
    cache = _LOCAL_DB_CACHE
    now = time.time()
    if cache["matches"] is None or now - float(cache["ts"]) > LOCAL_DB_TTL_SECONDS:
        try:
            db = connect(config.db_url)
            db.ensure_schema()
            cache["matches"] = list_matches(db)
        except Exception:
            cache["matches"] = []
        cache["standings"] = None
        cache["ts"] = now
    return list(cache["matches"])


def _local_team_matches(team_name: str, limit: int = 5) -> list[dict]:
    norm = _normalize_team_name(team_name)
    if not norm:
        return []
    rows: list[dict] = []
    for match in _local_db_matches():
        home_norm = _normalize_team_name(match.home_team)
        away_norm = _normalize_team_name(match.away_team)
        if norm not in (home_norm, away_norm):
            continue
        is_home = home_norm == norm
        gf = match.home_score if is_home else match.away_score
        ga = match.away_score if is_home else match.home_score
        if gf is None or ga is None:
            continue
        opponent = match.away_team if is_home else match.home_team
        if not opponent:
            continue
        if gf > ga:
            result = "W"
        elif gf == ga:
            result = "D"
        else:
            result = "L"
        rows.append(
            {
                "date": match.date or "",
                "opponent": opponent,
                "score": f"{gf}-{ga}",
                "home": is_home,
                "gf": gf,
                "ga": ga,
                "result": result,
                "competition": "Local DB",
                "competition_type": "Local",
            }
        )
    rows.sort(key=lambda row: (_parse_iso_date(row.get("date")) or datetime.min), reverse=True)
    return rows[:limit]


def _local_team_rates(team_name: str) -> tuple[float, float, str]:
    rows = _local_team_matches(team_name, limit=5)
    if not rows:
        return 0.0, 0.0, ""
    total = max(1, len(rows))
    btts_hits = sum(1 for row in rows if (row.get("gf") or 0) > 0 and (row.get("ga") or 0) > 0)
    over25_hits = sum(1 for row in rows if ((row.get("gf") or 0) + (row.get("ga") or 0)) >= 3)
    form_line = _form_line(rows)
    return btts_hits / total, over25_hits / total, form_line


def _record_local_result(table: dict[str, dict[str, object]], team_name: str, gf: int, ga: int) -> None:
    norm = _normalize_team_name(team_name)
    if not norm:
        return
    entry = table.setdefault(
        norm,
        {
            "team": team_name,
            "played": 0,
            "points": 0,
            "won": 0,
            "draw": 0,
            "lost": 0,
            "goals_for": 0,
            "goals_against": 0,
            "position": None,
        },
    )
    entry["team"] = entry.get("team") or team_name
    entry["played"] += 1
    entry["goals_for"] += gf
    entry["goals_against"] += ga
    if gf > ga:
        entry["won"] += 1
        entry["points"] += 3
    elif gf == ga:
        entry["draw"] += 1
        entry["points"] += 1
    else:
        entry["lost"] += 1


def _local_standings_data() -> dict[str, dict[str, object]]:
    cached = _LOCAL_DB_CACHE.get("standings")
    if isinstance(cached, dict):
        return cached
    table: dict[str, dict[str, object]] = {}
    for match in _local_db_matches():
        if match.home_team and match.away_team:
            _record_local_result(table, match.home_team, match.home_score, match.away_score)
            _record_local_result(table, match.away_team, match.away_score, match.home_score)
    ordered = sorted(
        table.values(),
        key=lambda row: (
            -float(row.get("points", 0)),
            -float((row.get("goals_for", 0) or 0) - (row.get("goals_against", 0) or 0)),
            -float(row.get("goals_for", 0) or 0),
        ),
    )
    for idx, entry in enumerate(ordered, start=1):
        entry["position"] = idx
    _LOCAL_DB_CACHE["standings"] = table
    return table


def _local_table_entry(team_name: str) -> dict[str, object]:
    standings = _local_standings_data()
    return standings.get(_normalize_team_name(team_name), {})


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _same_local_day(value: datetime, tz: ZoneInfo) -> bool:
    try:
        return value.astimezone(tz).date() == datetime.now(tz).date()
    except Exception:
        return False


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
    sources: dict[str, dict] = {}
    max_sources = int(os.environ.get("RSS_MAX_SOURCES", "12"))
    for path in (RSS_SOURCES_FILE, RSS_EXTRA_SOURCES_FILE):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            url = str(entry.get("url") or "").strip()
            if not url:
                continue
            label = str(entry.get("label") or url)
            weight = float(entry.get("weight", 0.5))
            existing = sources.get(url)
            if existing and existing.get("weight", 0) >= weight:
                continue
            sources[url] = {"url": url, "weight": weight, "label": label}
    result = list(sources.values()) or RSS_FEEDS
    if max_sources > 0:
        return result[:max_sources]
    return result


def _load_club_rss_map() -> dict[str, list[str]]:
    if not os.path.exists(CLUB_RSS_MAP_FILE):
        return {}
    try:
        with open(CLUB_RSS_MAP_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            if isinstance(data, dict):
                cleaned: dict[str, list[str]] = {}
                for key, value in data.items():
                    if not key or not isinstance(value, list):
                        continue
                    urls = [str(item).strip() for item in value if str(item).strip()]
                    if urls:
                        cleaned[str(key)] = urls
                return cleaned
    except Exception:
        return {}
    return {}


def _team_rss_sources(team_names: list[str]) -> list[dict]:
    mapping = _load_club_rss_map()
    if not mapping:
        return []
    sources: list[dict] = []
    for team in team_names:
        urls = mapping.get(team) or []
        for url in urls:
            sources.append({"url": url, "weight": 1.0, "label": f"{team} official"})
    return sources


def _load_offline_fixtures() -> list[dict]:
    cache = _OFFLINE_FIXTURES_CACHE
    now = time.time()
    if cache.get("fixtures") is not None and (now - float(cache.get("ts", 0.0))) < OFFLINE_FIXTURES_TTL_SECONDS:
        return list(cache["fixtures"])
    fixtures: list[dict] = []
    if os.path.exists(OFFLINE_FIXTURES_FILE):
        try:
            with open(OFFLINE_FIXTURES_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        match = dict(item)
                        if match.get("home_team") and match.get("away_team"):
                            fixtures.append(match)
        except Exception:
            fixtures = []
    cache["fixtures"] = fixtures
    cache["ts"] = now
    return fixtures


_BACKGROUND_THREAD: threading.Thread | None = None


def _background_refresh_loop() -> None:
    while True:
        t0 = time.perf_counter()
        _render_and_cache(force=False)
        dt = time.perf_counter() - t0
        if dt > 0.2:
            print(f"[SLOW] background loop cycle: {dt:.2f}s")
        time.sleep(max(60, BACKGROUND_REFRESH_SECONDS))


def _start_background_refresh() -> None:
    global _BACKGROUND_THREAD
    if BACKGROUND_REFRESH_SECONDS <= 0 or _BACKGROUND_THREAD is not None:
        return
    thread = threading.Thread(target=_background_refresh_loop, daemon=True)
    thread.start()
    _BACKGROUND_THREAD = thread


def _render_and_cache(force: bool = False, active_tab: str = "tips") -> bool:
    global _LAST_RENDER_HASH, _LAST_RENDER_TS
    throttle = max(60, BACKGROUND_REFRESH_SECONDS)
    try:
        with app.app_context():
            context, payload = _render_dashboard(active_tab, refresh_requested=True, render=False, force_refresh=force)
    except Exception:
        # Safe refresh fallback: render from cached/local data only.
        with app.app_context():
            context, payload = _render_dashboard(
                active_tab,
                refresh_requested=False,
                render=False,
                force_refresh=False,
                allow_remote=False,
            )
    payload_hash = _stable_hash(payload)
    now = time.time()
    if not force and _LAST_RENDER_HASH == payload_hash and (now - _LAST_RENDER_TS) < throttle:
        return False
    with app.app_context():
        html = render_template_string(_get_template(), **context)
    with _RESPONSE_LOCK:
        _RESPONSE_CACHE["html"] = html
        _RESPONSE_CACHE["ts"] = time.time()
        global _LAST_PAYLOAD
        _LAST_PAYLOAD = payload
    _LAST_RENDER_HASH = payload_hash
    _LAST_RENDER_TS = now
    return True


def _trigger_refresh_async() -> None:
    global _REFRESH_IN_FLIGHT
    with _REFRESH_LOCK:
        if _REFRESH_IN_FLIGHT:
            return
        _REFRESH_IN_FLIGHT = True

    def _runner() -> None:
        global _REFRESH_IN_FLIGHT
        try:
            _render_and_cache(force=True)
        finally:
            with _REFRESH_LOCK:
                _REFRESH_IN_FLIGHT = False

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()


def _stable_hash(payload: dict) -> str:
    s = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _load_weights() -> dict[str, object]:
    global _WEIGHTS_CACHE
    if _WEIGHTS_CACHE is not None:
        return _WEIGHTS_CACHE
    defaults: dict[str, object] = {
        "elo": 0.30,
        "form": 0.20,
        "table": 0.05,
        "market": 0.15,
        "injury": 0.05,
        "weather": 0.05,
        "news": 0.20,
        "model": {
            "beta0": -0.2,
            "beta_elo": 0.8,
            "beta_form": 0.6,
            "beta_table": 0.25,
            "beta_xg": 0.5,
            "beta_lineup": 0.35,
            "beta_injury": -0.45,
            "beta_weather": -0.25,
            "beta_news": 0.12,
        },
        "model_market": {
            "h2h": {"beta_elo": 0.9, "beta_form": 0.7, "beta_table": 0.3},
            "double_chance": {"beta_elo": 0.7, "beta_form": 0.6, "beta_table": 0.25},
            "draw_no_bet": {"beta_elo": 0.8, "beta_form": 0.6, "beta_table": 0.25},
            "spreads": {"beta_elo": 0.85, "beta_form": 0.6, "beta_table": 0.25},
        },
        "final": {"value": 0.5, "prob": 0.3, "news": 0.2},
        "market_overrides": {
            "h2h": {"elo": 1.05, "form": 1.0, "table": 1.0, "news": 0.9},
            "double_chance": {"elo": 1.0, "form": 1.0, "table": 0.9, "news": 0.9},
            "btts": {"elo": 0.8, "form": 1.15, "table": 0.7, "news": 1.0},
            "totals": {"elo": 0.8, "form": 1.15, "table": 0.7, "news": 1.0},
        },
        "final_market": {
            "h2h": {"value": 0.55, "prob": 0.3, "news": 0.15},
            "double_chance": {"value": 0.45, "prob": 0.4, "news": 0.15},
            "btts": {"value": 0.4, "prob": 0.5, "news": 0.1},
            "totals": {"value": 0.4, "prob": 0.5, "news": 0.1},
        },
    }
    path = os.path.join("data", "weights.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                if isinstance(data, dict):
                    for key, value in data.items():
                        if key in defaults and isinstance(defaults[key], dict) and isinstance(value, dict):
                            defaults[key].update(value)
                        else:
                            defaults[key] = value
        except Exception:
            pass
    adaptive = _load_adaptive_weights()
    adaptive_weights = adaptive.get("weights") if isinstance(adaptive.get("weights"), dict) else None
    if adaptive_weights:
        defaults = _merge_adaptive_weights(defaults, adaptive_weights)
    _WEIGHTS_CACHE = defaults
    return defaults


def _market_weights(weights: dict[str, object], market_key: str) -> dict[str, float]:
    overrides = weights.get("market_overrides", {})
    base = {
        "elo": float(weights.get("elo", 0.3)),
        "form": float(weights.get("form", 0.2)),
        "table": float(weights.get("table", 0.05)),
        "injury": float(weights.get("injury", 0.05)),
        "weather": float(weights.get("weather", 0.05)),
        "news": float(weights.get("news", 0.2)),
    }
    if isinstance(overrides, dict):
        market_override = overrides.get(market_key, {})
        if isinstance(market_override, dict):
            for key in ("elo", "form", "table", "news"):
                if key in market_override:
                    base[key] *= float(market_override.get(key, 1.0))
    return base


def _market_final_weights(weights: dict[str, object], market_key: str) -> dict[str, float]:
    base = weights.get("final", {})
    final = {
        "value": float(base.get("value", 0.5)),
        "prob": float(base.get("prob", 0.3)),
        "news": float(base.get("news", 0.2)),
    }
    overrides = weights.get("final_market", {})
    if isinstance(overrides, dict):
        market_override = overrides.get(market_key, {})
        if isinstance(market_override, dict):
            for key in ("value", "prob", "news"):
                if key in market_override:
                    final[key] = float(market_override.get(key, final[key]))
    return final


def _market_model_weights(weights: dict[str, object], market_key: str) -> dict[str, object]:
    base_model = dict(weights.get("model", {}))
    overrides = weights.get("model_market", {})
    if isinstance(overrides, dict):
        market_override = overrides.get(market_key, {})
        if isinstance(market_override, dict):
            base_model.update(market_override)
    new_weights = dict(weights)
    new_weights["model"] = base_model
    return new_weights


def _team_index_key(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]", "", name.lower())
    return cleaned


def _load_json_records(filename: str) -> list[dict]:
    path = _DATA_DIR / filename
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
    except Exception:
        pass
    return []


def _load_xg_index() -> dict[str, dict]:
    global _XG_INDEX
    if _XG_INDEX is not None:
        return _XG_INDEX
    records = _load_json_records("xg_statsbomb_sample.json")
    index = {}
    for entry in records:
        key = entry.get("match_key")
        if not key:
            continue
        index[key] = entry
    _XG_INDEX = index
    return index


def _load_injury_records() -> dict[str, list[dict]]:
    global _INJURY_RECORDS
    if _INJURY_RECORDS is not None:
        return _INJURY_RECORDS
    records = _load_json_records("transfermarkt_injury_sample.json")
    grouped: dict[str, list[dict]] = {}
    for entry in records:
        team = str(entry.get("team") or "")
        key = _team_index_key(team)
        if not key:
            continue
        grouped.setdefault(key, []).append(entry)
    _INJURY_RECORDS = grouped
    return grouped


def _team_injury_index_data(team_name: str) -> float:
    key = _team_index_key(team_name)
    records = _load_injury_records().get(key, [])
    if not records:
        return 0.0
    total_games = sum(float(record.get("games_missed", 0)) for record in records)
    base = max(1.0, len(records) * 2.0)
    return min(1.0, total_games / base)


def _lineup_strength_from_data(team_name: str) -> float:
    key = _team_index_key(team_name)
    records = _load_injury_records().get(key, [])
    if not records:
        return 1.0
    penalty = min(0.6, len(records) * 0.12)
    return max(0.5, 1.0 - penalty)


def _sample_match_xg(match: dict) -> tuple[float | None, float | None]:
    records = _load_xg_index()
    key = _match_key(match)
    entry = records.get(key)
    if entry:
        return entry.get("expected_goals_home"), entry.get("expected_goals_away")
    return None, None


def _normalize_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]", "", name.lower())
    cleaned = cleaned.replace("fc", "").replace("cf", "").replace("afc", "")
    return cleaned


def _is_friendly_comp(name: str | None = None, code: str | None = None, league_type: str | None = None, sport_title: str | None = None) -> bool:
    tokens = " ".join(value for value in [name, code, league_type, sport_title] if value)
    text = tokens.lower()
    keywords = [
        "friendly",
        "friendlies",
        "club friendly",
        "international friendly",
        "intl friendly",
        "test match",
        "pre-season",
        "preseason",
        "training",
        "exhibition",
        "charity",
        "testimonial",
        "warm-up",
        "warmup",
        "practice",
        "baratsagos",
    ]
    return any(keyword in text for keyword in keywords)


def _is_cup_comp(name: str | None = None, code: str | None = None, league_type: str | None = None, sport_title: str | None = None) -> bool:
    tokens = " ".join(value for value in [name, code, league_type, sport_title] if value)
    text = tokens.lower()
    keywords = [
        "cup",
        "copa",
        "kupa",
        "coupe",
        "coppa",
        "pokalen",
        "taça",
        "kup",
        "knockout",
        "ko",
        "playoff",
        "play-off",
    ]
    return any(keyword in text for keyword in keywords)


def _match_competition(match: dict) -> str:
    comp_name = str(match.get("comp_name") or "")
    if comp_name:
        return comp_name
    comp = match.get("competition", {})
    if isinstance(comp, dict):
        name = comp.get("name")
        if name:
            return str(name)
    sport_title = match.get("sport_title")
    if sport_title:
        return str(sport_title)
    return str(match.get("comp_code") or match.get("sport_key") or "")


def _normalize_comp(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _normalize_team_tokens(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "", value.lower())
    for token in ("fc", "cf", "afc", "ac", "sc", "ud", "calcio"):
        cleaned = cleaned.replace(token, "")
    return cleaned


def _is_youth_comp(name: str) -> bool:
    text = name.lower()
    return any(token in text for token in ("u20", "u21", "u23", "youth", "primavera", "reserve"))


def _default_allowed_comp_rules() -> list[tuple[str, str | None]]:
    return [
        ("uefa champions league", None),
        ("uefa europa league", None),
        ("uefa europa conference league", None),
        ("premier league", "england"),
        ("championship", "england"),
        ("serie a", "italy"),
        ("bundesliga", "germany"),
        ("ligue 1", "france"),
        ("eredivisie", "netherlands"),
        ("primeira liga", "portugal"),
        ("primera division", "spain"),
        ("la liga", "spain"),
    ]


def _allowed_comp_match(comp_name: str, comp_country: str) -> bool:
    if not comp_name:
        return False
    name_norm = _normalize_comp(comp_name)
    country_norm = _normalize_comp(comp_country or "")
    for name_rule, country_rule in _default_allowed_comp_rules():
        if _normalize_comp(name_rule) not in name_norm:
            continue
        if country_rule:
            if country_norm == _normalize_comp(country_rule):
                return True
            continue
        return True
    return False


def _fd_competition_map(competitions: list[dict]) -> dict[tuple[str, str], str]:
    mapping: dict[tuple[str, str], str] = {}
    for comp in competitions:
        code = str(comp.get("code") or "")
        name = str(comp.get("name") or "")
        country = str(comp.get("area", {}).get("name") or "")
        if code and name and country:
            mapping[(_normalize_comp(name), _normalize_comp(country))] = code
    return mapping


def _season_for_date(value: str | None) -> int:
    if not value:
        return datetime.now(timezone.utc).year
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        year = dt.year
        return year - 1 if dt.month < 7 else year
    except Exception:
        return datetime.now(timezone.utc).year


def _fdco_season_code(season_year: int) -> str:
    start = int(season_year)
    return f"{str(start)[2:]}{str(start + 1)[2:]}"


def _fdco_league_code(comp_code: str | None) -> str | None:
    if not comp_code:
        return None
    return FDCO_UK_LEAGUE_MAP.get(str(comp_code))


def _api_football_team_id(api_key: str, team_name: str) -> int | None:
    if not api_key or not team_name:
        return None
    cache_key = _normalize_team_tokens(team_name)
    cached = _cache_get(_TEAM_ID_CACHE, cache_key, 86400)
    if isinstance(cached, int):
        return cached
    try:
        response = _http_get(
            "https://v3.football.api-sports.io/teams",
            headers={"x-apisports-key": api_key},
            params={"search": team_name},
            timeout=12,
        )
        if response.status_code != 200:
            return None
        items = response.json().get("response", [])
        if not items:
            return None
        target = _normalize_team_tokens(team_name)
        best_id = None
        best_score = -1
        for item in items:
            team = item.get("team", {})
            candidate_id = team.get("id")
            if not isinstance(candidate_id, int):
                continue
            name = str(team.get("name") or "")
            code = str(team.get("code") or "")
            candidates = [name, code]
            score = 0
            for cand in candidates:
                norm = _normalize_team_tokens(cand)
                if not norm:
                    continue
                if norm == target:
                    score = max(score, 3)
                elif target and (norm in target or target in norm):
                    score = max(score, 2)
                elif cand and cand.lower() in team_name.lower():
                    score = max(score, 1)
            if score > best_score:
                best_score = score
                best_id = candidate_id
        if best_id is None:
            best = items[0].get("team", {})
            best_id = best.get("id")
        if isinstance(best_id, int):
            _cache_set(_TEAM_ID_CACHE, cache_key, best_id)
            return best_id
    except Exception:
        return None
    return None


def _fetch_competitions_fd(token: str) -> list[dict]:
    cached = _cache_get(_FD_COMP_CACHE, "all", FD_COMP_TTL_SECONDS)
    if isinstance(cached, list):
        return cached
    if not token:
        return []
    if _backoff_active("football-data"):
        return []
    try:
        response = _http_get(
            "https://api.football-data.org/v4/competitions",
            headers={"X-Auth-Token": token},
            timeout=12,
        )
        if response.status_code == 429:
            _note_backoff("football-data")
            return []
        if response.status_code != 200:
            return []
        competitions = [comp for comp in response.json().get("competitions", []) if comp.get("code")]
        _cache_set(_FD_COMP_CACHE, "all", competitions)
        return competitions
    except Exception:
        return []


def _team_id_map() -> dict[str, int]:
    global _TEAM_ID_MAP
    if _TEAM_ID_MAP is not None:
        return _TEAM_ID_MAP
    _TEAM_ID_MAP = {}
    try:
        if FAST_MODE:
            return _TEAM_ID_MAP
        competitions = _fetch_competitions_fd(config.football_data_token)
        if not competitions:
            return _TEAM_ID_MAP
        headers = {"X-Auth-Token": config.football_data_token}
        max_comp = int(os.environ.get("FD_TEAMMAP_MAX_COMP", "10"))
        for comp in competitions[:max_comp]:
            comp_code = comp.get("code")
            if not comp_code:
                continue
            teams_resp = _http_get(
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
        response = _http_get(
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
        response = _http_get(f"http://api.clubelo.com/{norm}", timeout=15)
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


def _fast_clubelo(team_name: str) -> float:
    norm = team_name.replace(" FC", "").replace("AFC ", "").strip().replace(" ", "_")
    return _ELO_CACHE.get(norm, 1500.0)


def _model_probs(
    elo_diff: float,
    form_diff: float,
    *,
    table_diff: float = 0.0,
    xg_diff: float = 0.0,
    lineup_diff: float = 0.0,
    injury_diff: float = 0.0,
    weather_risk: float = 0.0,
    news_sentiment_diff: float = 0.0,
    weights: dict[str, object] | None = None,
) -> tuple[float, float, float]:
    if weights is None:
        weights = _load_weights()
    model_config = weights.get("model", {})
    beta0 = float(model_config.get("beta0", -0.2))
    beta_elo = float(model_config.get("beta_elo", 0.8))
    beta_form = float(model_config.get("beta_form", 0.6))
    beta_table = float(model_config.get("beta_table", 0.25))
    beta_xg = float(model_config.get("beta_xg", 0.5))
    beta_lineup = float(model_config.get("beta_lineup", 0.35))
    beta_injury = float(model_config.get("beta_injury", -0.45))
    beta_weather = float(model_config.get("beta_weather", -0.25))
    beta_news = float(model_config.get("beta_news", 0.12))

    z = (
        beta0
        + beta_elo * elo_diff
        + beta_form * form_diff
        + beta_table * table_diff
        + beta_xg * xg_diff
        + beta_lineup * lineup_diff
        + beta_injury * injury_diff
        + beta_weather * weather_risk
        + beta_news * news_sentiment_diff
    )
    p_home = 1 / (1 + math.exp(-z))
    err = 1.0 - abs(max(-1.0, min(1.0, elo_diff)))
    p_draw = max(0.12, min(0.32, 0.18 + 0.12 * err))
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


def _team_norm_match(team_norm: str, cand_norm: str) -> bool:
    if not team_norm or not cand_norm:
        return False
    if team_norm == cand_norm:
        return True
    if len(cand_norm) >= 4 and cand_norm in team_norm:
        return True
    if len(team_norm) >= 4 and team_norm in cand_norm:
        return True
    return False


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
    if _backoff_active("football-data"):
        return []
    try:
        headers = {"X-Auth-Token": config.football_data_token}
        response = _http_get(
            f"https://api.football-data.org/v4/competitions/{comp_code}/standings",
            headers=headers,
            timeout=12,
        )
        if response.status_code == 429:
            _note_backoff("football-data")
            return []
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
            team_info = row.get("team", {})
            team = team_info.get("name")
            points = row.get("points")
            position = row.get("position")
            if team and points is not None and position is not None:
                rows.append(
                    {
                        "position": position,
                        "team": team,
                        "team_id": team_info.get("id"),
                        "points": points,
                        "played": row.get("playedGames"),
                        "won": row.get("won"),
                        "draw": row.get("draw"),
                        "lost": row.get("lost"),
                        "goals_for": row.get("goalsFor"),
                        "goals_against": row.get("goalsAgainst"),
                    }
                )
        _STANDINGS_CACHE[comp_code] = rows
        return rows
    except Exception:
        return []


def _summary_dict(team_name: str, team_id: int | None = None) -> dict:
    if not team_name:
        return {
            "team": "",
            "win_rate": 0.0,
            "goals_for_avg": 0.0,
            "btts_rate": None,
            "over25_rate": None,
            "corners_avg": None,
            "cards_avg": None,
            "source": "n/a",
            "matches": [],
        }
    if FAST_MODE:
        return {
            "team": team_name,
            "win_rate": 0.0,
            "goals_for_avg": 0.0,
            "btts_rate": None,
            "over25_rate": None,
            "corners_avg": None,
            "cards_avg": None,
            "source": "n/a",
            "matches": [],
        }
    team_norm = _normalize_team_name(team_name)
    comp_hint = _TEAM_COMP_HINT.get(team_norm)
    if comp_hint:
        mapped = _comp_name_to_fd_code(str(comp_hint))
        if mapped:
            comp_hint = mapped
            _TEAM_COMP_HINT[team_norm] = mapped
    season_hint = _TEAM_SEASON_HINT.get(team_norm)
    cache_key = f"{team_norm}:{team_id or ''}"
    cached = _cache_get(_TEAM_SUMMARY_CACHE, cache_key, TEAM_SUMMARY_TTL_SECONDS)
    if isinstance(cached, dict):
        if cached.get("source") == "n/a":
            if isinstance(team_id, str) and team_id.startswith("sr:competitor:"):
                cached = None
            elif comp_hint or season_hint or team_id:
                cached = None
        if cached is not None and comp_hint and cached.get("table_entry") is None:
            cached = None
        if cached is not None:
            return cached
    league_code = _fdco_league_code(comp_hint)
    season_year = _season_for_date(datetime.now(timezone.utc).date().isoformat())
    season_code = _fdco_season_code(season_year)
    corners_avg = None
    cards_avg = None
    team_id_af = team_id
    team_id_fd = _team_id_map().get(_normalize_name(team_name))
    if team_id_af is None and (team_id is None or isinstance(team_id, int)):
        team_id_af = _api_football_team_id(config.api_football_key, team_name)
    table_entry: dict[str, object] | None = None
    if comp_hint:
        standings = _fetch_standings(comp_hint)
        table_entry = _standings_highlight(standings, team_name)
        if not table_entry:
            table_entry = None
        if team_id_fd is None and table_entry and table_entry.get("team_id"):
            team_id_fd = table_entry.get("team_id")
    if table_entry is None and season_hint:
        standings = _fetch_standings_sportradar(season_hint)
        table_entry = _standings_highlight(standings, team_name, team_id if isinstance(team_id, str) else None)
    if league_code is None:
        for code in ("E0", "SC0", "D1", "I1", "SP1", "F1"):
            fdco_matches, season_stats, corners_avg, cards_avg, table_entry = _fdco_team_season_stats(
                team_name,
                code,
                season_year,
                list_limit=5,
            )
            if season_stats:
                recent = _calc_recent_rates(fdco_matches) or {}
                result = {
                    "team": team_name,
                    "win_rate": season_stats["win_rate"],
                    "goals_for_avg": season_stats["goals_for_avg"],
                    "btts_rate": season_stats["btts_rate"],
                    "over25_rate": season_stats["over25_rate"],
                    "season_win_rate": season_stats["win_rate"],
                    "season_goals_for_avg": season_stats["goals_for_avg"],
                    "season_btts_rate": season_stats["btts_rate"],
                    "season_over25_rate": season_stats["over25_rate"],
                    "recent_win_rate": recent.get("win_rate"),
                    "recent_goals_for_avg": recent.get("goals_for_avg"),
                    "corners_avg": corners_avg,
                    "cards_avg": cards_avg,
                    "source": "football-data.co.uk",
                    "matches": fdco_matches[:5],
                }
                _cache_set(_TEAM_SUMMARY_CACHE, cache_key, result)
                return result
    if season_hint and isinstance(team_id, str) and team_id.startswith("sr:competitor:"):
        stats = _fetch_competitor_stats_sportradar(season_hint, team_id)
        matches_played = stats.get("matches_played")
        corners = stats.get("corner_kicks")
        cards = stats.get("cards_given")
        if isinstance(matches_played, (int, float)) and matches_played > 0:
            if corners is not None:
                corners_avg = float(corners) / float(matches_played)
            if cards is not None:
                cards_avg = float(cards) / float(matches_played)
    if team_id_fd is None and team_id is not None and comp_hint:
        team_id_fd = team_id
    if league_code:
        fdco_matches, season_stats, corners_avg, cards_avg, table_entry = _fdco_team_season_stats(
            team_name,
            league_code,
            season_year,
            list_limit=5,
        )
        if season_stats:
            recent = _calc_recent_rates(fdco_matches) or {}
            result = {
                "team": team_name,
                "win_rate": season_stats["win_rate"],
                "goals_for_avg": season_stats["goals_for_avg"],
                "btts_rate": season_stats["btts_rate"],
                "over25_rate": season_stats["over25_rate"],
                "season_win_rate": season_stats["win_rate"],
                "season_goals_for_avg": season_stats["goals_for_avg"],
                "season_btts_rate": season_stats["btts_rate"],
                "season_over25_rate": season_stats["over25_rate"],
                "recent_win_rate": recent["win_rate"] if recent else None,
                "recent_goals_for_avg": recent["goals_for_avg"] if recent else None,
                "recent_btts_rate": recent["btts_rate"] if recent else None,
                "recent_over25_rate": recent["over25_rate"] if recent else None,
                "corners_avg": corners_avg,
                "cards_avg": cards_avg,
                "form_line": _form_line(fdco_matches),
                "source": "football-data.co.uk",
                "matches": [
                    {
                        "date": row.get("date", ""),
                        "opponent": row.get("opponent", ""),
                        "score": row.get("score", ""),
                        "home": row.get("home", False),
                        "competition": row.get("competition", ""),
                        "competition_type": row.get("competition_type", ""),
                    }
                    for row in fdco_matches
                ],
                "table_entry": table_entry,
            }
            _cache_set(_TEAM_SUMMARY_CACHE, cache_key, result)
            return result

    matches = _team_recent_matches_api_football(config.api_football_key, team_id_af, limit=5)
    source = "api-football" if matches else "football-data"
    if not matches:
        if isinstance(team_id, str) and team_id.startswith("sr:competitor:"):
            matches = _team_recent_matches_sportradar(team_id, limit=5)
            source = "sportradar" if matches else source
    if not matches:
        matches = _team_recent_matches_fd(config.football_data_token, team_id_fd, limit=5)
    if not matches:
        local_matches = _local_team_matches(team_name, limit=5)
        if local_matches:
            matches = local_matches
            source = "local-db"
            if table_entry is None:
                table_entry = _local_table_entry(team_name)
    if not matches:
        result = {
            "team": team_name,
            "win_rate": None,
            "goals_for_avg": None,
            "btts_rate": None,
            "over25_rate": None,
            "corners_avg": corners_avg,
            "cards_avg": cards_avg,
            "form_line": "",
            "source": "n/a",
            "matches": [],
            "table_entry": table_entry,
        }
        _cache_set(_TEAM_SUMMARY_CACHE, cache_key, result)
        return result
    recent = _calc_recent_rates(matches)
    season_win_rate = None
    season_goals_for_avg = None
    if table_entry and table_entry.get("played"):
        played = float(table_entry.get("played") or 0)
        wins = float(table_entry.get("won") or 0)
        goals_for = float(table_entry.get("goals_for") or 0)
        if played > 0:
            season_win_rate = wins / played
            season_goals_for_avg = goals_for / played
    total = len(matches)
    wins = sum(1 for row in matches if row.get("result") == "W")
    goals_for = sum(row.get("gf", 0) for row in matches)
    btts_hits = sum(1 for row in matches if row.get("gf", 0) > 0 and row.get("ga", 0) > 0)
    over25_hits = sum(1 for row in matches if (row.get("gf", 0) + row.get("ga", 0)) >= 3)
    if matches and config.api_football_key and AF_STATS_ENABLED and (corners_avg is None or cards_avg is None):
        corners_values = []
        cards_values = []
        for row in matches:
            fixture_id = row.get("fixture_id")
            team_id_value = row.get("team_id") or team_id_af
            if not fixture_id or not team_id_value:
                continue
            try:
                team_id_value = int(team_id_value)
            except Exception:
                continue
            stats = _fixture_stats_api_football(config.api_football_key, fixture_id)
            team_stats = stats.get(team_id_value) if stats else None
            if not team_stats:
                continue
            corners = team_stats.get("corners")
            cards = team_stats.get("cards")
            if corners is not None:
                corners_values.append(corners)
            if cards is not None:
                cards_values.append(cards)
        if corners_values:
            corners_avg = sum(corners_values) / len(corners_values)
        if cards_values:
            cards_avg = sum(cards_values) / len(cards_values)
    if (corners_avg is None or cards_avg is None):
        comp_hint = _TEAM_COMP_HINT.get(_normalize_team_name(team_name))
        league_code = _fdco_league_code(comp_hint)
        if league_code:
            season_year = _season_for_date(datetime.now(timezone.utc).date().isoformat())
            fdco_matches, fdco_corners, fdco_cards, _ = _fdco_team_summary(team_name, league_code, season_year, limit=5)
            if corners_avg is None and fdco_corners is not None:
                corners_avg = fdco_corners
            if cards_avg is None and fdco_cards is not None:
                cards_avg = fdco_cards
    if table_entry is None and league_code:
        table_data = _fdco_table_data(league_code, season_code)
        table_entry = table_data.get(team_norm)
    result = {
        "team": team_name,
        "win_rate": season_win_rate if season_win_rate is not None else wins / total,
        "goals_for_avg": season_goals_for_avg if season_goals_for_avg is not None else goals_for / total,
        "btts_rate": btts_hits / total,
        "over25_rate": over25_hits / total,
        "season_win_rate": season_win_rate,
        "season_goals_for_avg": season_goals_for_avg,
        "season_btts_rate": None,
        "season_over25_rate": None,
        "recent_win_rate": recent["win_rate"] if recent else None,
        "recent_goals_for_avg": recent["goals_for_avg"] if recent else None,
        "recent_btts_rate": recent["btts_rate"] if recent else None,
        "recent_over25_rate": recent["over25_rate"] if recent else None,
        "corners_avg": corners_avg,
        "cards_avg": cards_avg,
        "form_line": _form_line(matches),
        "source": source,
        "matches": [
            {
                "date": row.get("date", ""),
                "opponent": row.get("opponent", ""),
                "score": row.get("score", ""),
                "home": row.get("home", False),
                "competition": row.get("competition", ""),
                "competition_type": row.get("competition_type", ""),
            }
            for row in matches
        ],
        "table_entry": table_entry,
    }
    _cache_set(_TEAM_SUMMARY_CACHE, cache_key, result)
    return result


def _form_line(matches: list[dict]) -> str:
    if not matches:
        return ""
    return "".join(row.get("result", "") or "-" for row in matches[:5])


def _calc_recent_rates(matches: list[dict]) -> dict[str, float] | None:
    if not matches:
        return None
    rows = matches[:5]
    total = max(1, len(rows))
    wins = sum(1 for row in rows if row.get("result") == "W")
    goals_for = sum(row.get("gf", 0) for row in rows)
    btts_hits = sum(1 for row in rows if row.get("gf", 0) > 0 and row.get("ga", 0) > 0)
    over25_hits = sum(1 for row in rows if (row.get("gf", 0) + row.get("ga", 0)) >= 3)
    return {
        "win_rate": wins / total,
        "goals_for_avg": goals_for / total,
        "btts_rate": btts_hits / total,
        "over25_rate": over25_hits / total,
        "total": float(total),
    }


def _standings_highlight(standings: list[dict], team_name: str, team_id: int | None = None) -> dict:
    if team_id is not None:
        for row in standings:
            if row.get("team_id") == team_id:
                return row
    key = _normalize_team_name(team_name)
    for row in standings:
        if _normalize_team_name(row.get("team", "")) == key:
            return row
    key2 = _normalize_team_tokens(team_name)
    for row in standings:
        if key2 and key2 in _normalize_team_tokens(row.get("team", "")):
            return row
    return {}


def _confidence_profile(pick: dict) -> dict[str, object]:
    notes = []
    signals = 0
    odds = pick.get("odds")
    if odds:
        signals += 1
    else:
        notes.append("nincs odds")
    home_summary = pick.get("home_summary") or {}
    away_summary = pick.get("away_summary") or {}
    home_matches = home_summary.get("matches") or []
    away_matches = away_summary.get("matches") or []
    if len(home_matches) >= 5 and len(away_matches) >= 5:
        signals += 1
    else:
        if len(home_matches) < 5:
            notes.append("keves hazai meccs")
        if len(away_matches) < 5:
            notes.append("keves vendeg meccs")
    home_standing = pick.get("home_standing") or {}
    away_standing = pick.get("away_standing") or {}
    if home_standing.get("position") is not None and away_standing.get("position") is not None:
        signals += 1
    else:
        if home_standing.get("position") is None:
            notes.append("nincs hazai tabella")
        if away_standing.get("position") is None:
            notes.append("nincs vendeg tabella")
    if pick.get("btts_rate") or pick.get("over25_rate"):
        signals += 1
    if pick.get("news_score"):
        signals += 1
    weather = pick.get("weather") or {}
    if weather and any(k in weather for k in ("precip_prob_max", "temp_min", "temp_max")):
        signals += 1
    if signals >= 4:
        level = "eros"
    elif signals >= 2:
        level = "kozepes"
    else:
        level = "gyenge"
    return {"level": level, "notes": notes}


def _pick_risk_flags(match: dict | None, pick: dict) -> list[str]:
    flags = []
    comp_name = ""
    comp_type = ""
    if isinstance(match, dict):
        comp_name = str(match.get("competition_name") or match.get("competition") or "")
        comp_type = str(match.get("competition_type") or match.get("type") or "")
    else:
        comp_name = str(pick.get("competition") or "")
    home_team = str(pick.get("home_team") or "")
    away_team = str(pick.get("away_team") or "")
    if RISK_EXCLUDE_CUP and _is_cup_comp(comp_name, comp_type, comp_type, comp_name):
        flags.append("kupa")
    if RISK_EXCLUDE_DERBY and home_team and away_team and _is_rivalry(home_team, away_team):
        flags.append("derbi")
    if RISK_EXCLUDE_ROTATION:
        lineup_diff = abs(float(pick.get("lineup_diff") or 0.0))
        injury_index = float(pick.get("injury_index") or 0.0)
        if lineup_diff >= 0.4 or injury_index >= 0.6:
            flags.append("rotacio")
    return flags


def _filter_picks_by_risk(picks: list[dict]) -> list[dict]:
    if not picks:
        return []
    safe = [item for item in picks if not item.get("risk_flags")]
    return safe if safe else picks


def _passes_value_filters(pick: dict) -> bool:
    if not pick:
        return False
    odds = pick.get("odds")
    if not isinstance(odds, (int, float)) or odds <= 1.0:
        return True
    if MARKET_ALLOWLIST and pick.get("market_key") not in MARKET_ALLOWLIST:
        return False
    if ODDS_MIN > 0 and odds < ODDS_MIN:
        return False
    if ODDS_MAX > 0 and odds > ODDS_MAX:
        return False
    model_prob = pick.get("model_prob")
    implied_prob = pick.get("implied_prob")
    value = pick.get("value")
    if not isinstance(value, (int, float)) and isinstance(model_prob, (int, float)) and isinstance(implied_prob, (int, float)):
        value = model_prob - implied_prob
    if isinstance(model_prob, (int, float)) and model_prob < MIN_MODEL_PROB:
        return False
    if isinstance(value, (int, float)) and value < MIN_VALUE_EDGE:
        return False
    ev = pick.get("ev")
    if isinstance(ev, (int, float)) and ev < MIN_EV:
        return False
    return True


def _enrich_pick(pick: dict) -> dict:
    if not pick:
        return pick
    if pick.get("sport_key", "").startswith("sr:competition:"):
        pick.setdefault("comp_code", pick.get("sport_key"))
    comp_hint = pick.get("fd_code") or pick.get("comp_code") or _sport_key_to_comp(pick.get("sport_key", ""))
    comp_name = str(pick.get("competition") or pick.get("comp_name") or "")
    if comp_hint:
        mapped = _comp_name_to_fd_code(str(comp_hint))
        if mapped:
            comp_hint = mapped
            pick["comp_code"] = comp_hint
            pick["fd_code"] = comp_hint
    if not comp_hint:
        comp_hint = _comp_name_to_fd_code(comp_name)
        if comp_hint:
            pick["comp_code"] = comp_hint
            pick["fd_code"] = comp_hint
    if comp_hint and pick.get("home_team") and pick.get("away_team"):
        _TEAM_COMP_HINT[_normalize_team_name(pick.get("home_team", ""))] = str(comp_hint)
        _TEAM_COMP_HINT[_normalize_team_name(pick.get("away_team", ""))] = str(comp_hint)
    if pick.get("sport_key", "").startswith("sr:competition:") and not pick.get("sr_season_id"):
        _fill_sportradar_ids(pick)
    sr_season_id = pick.get("sr_season_id")
    if sr_season_id and pick.get("home_team") and pick.get("away_team"):
        _TEAM_SEASON_HINT[_normalize_team_name(pick.get("home_team", ""))] = str(sr_season_id)
        _TEAM_SEASON_HINT[_normalize_team_name(pick.get("away_team", ""))] = str(sr_season_id)
    pick["home_summary"] = _summary_dict(pick.get("home_team", ""), pick.get("home_id"))
    pick["away_summary"] = _summary_dict(pick.get("away_team", ""), pick.get("away_id"))
    standings = []
    league_id = pick.get("comp_code") or pick.get("sport_key")
    try:
        league_id = int(league_id)
    except Exception:
        league_id = None
    season = pick.get("season")
    season = int(season) if isinstance(season, int) else _season_for_date(pick.get("commence_time"))
    standings = _fetch_standings_api_football(config.api_football_key, league_id, season)
    if not standings:
        if sr_season_id:
            standings = _fetch_standings_sportradar(str(sr_season_id))
    if not standings:
        comp_code = pick.get("fd_code") or _sport_key_to_comp(pick.get("sport_key", ""))
        standings = _fetch_standings(comp_code)
    pick["standings"] = standings
    pick["home_standing"] = _standings_highlight(standings, pick.get("home_team", ""), pick.get("home_id"))
    pick["away_standing"] = _standings_highlight(standings, pick.get("away_team", ""), pick.get("away_id"))
    pick["weather"] = _weather_details(pick.get("home_team", ""))
    _attach_sportradar_extras(pick)
    pick["story"] = _build_story(pick)
    model_prob = pick.get("model_prob")
    if not isinstance(model_prob, (int, float)):
        model_prob = float(pick.get("score") or 0.0)
    pick["model_prob"] = model_prob
    pick["confidence"] = _confidence_profile(pick)
    if "risk_flags" not in pick:
        pick["risk_flags"] = _pick_risk_flags(None, pick)
    return _ensure_pick_fields(pick)


def _normalize_target_matches(
    best_pick: dict | None,
    target_matches: list[dict] | None,
    best_combo: dict | None,
    fallback_fixtures: list[dict] | None = None,
    fallback_standings: dict[str, list[dict]] | None = None,
    rss_items: list[dict] | None = None,
    market_roi: dict[str, float] | None = None,
) -> list[dict]:
    """Ensure best_pick is first and return up to 2 picks if possible."""
    picks: list[dict] = []
    if best_combo and isinstance(best_combo, dict):
        combo_matches = best_combo.get("matches") or []
        if isinstance(combo_matches, list):
            picks.extend(combo_matches)
    if best_pick:
        picks.append(best_pick)
    if target_matches:
        picks.extend(target_matches)

    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in picks:
        if not item:
            continue
        home = str(item.get("home_team") or "").strip().lower()
        away = str(item.get("away_team") or "").strip().lower()
        key = (home, away)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= 2:
            break
    if len(deduped) < 2 and fallback_fixtures is not None and fallback_standings is not None and market_roi is not None:
        extra = _build_stat_only_picks(
            fallback_fixtures,
            fallback_standings,
            rss_items or [],
            market_roi,
            24,
        )
        for item in extra:
            if len(deduped) >= 2:
                break
            home = str(item.get("home_team") or "").strip().lower()
            away = str(item.get("away_team") or "").strip().lower()
            key = (home, away)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
    if len(deduped) < 2 and deduped:
        dup = dict(deduped[0])
        dup["outcome"] = str(dup.get("outcome") or "")
        deduped.append(dup)
    return deduped


def _is_pick_complete(pick: dict) -> bool:
    if not pick:
        return False
    home_summary = pick.get("home_summary") or {}
    away_summary = pick.get("away_summary") or {}
    home_matches = home_summary.get("matches") or []
    away_matches = away_summary.get("matches") or []
    if len(home_matches) < 5 or len(away_matches) < 5:
        return False
    home_standing = pick.get("home_standing") or {}
    away_standing = pick.get("away_standing") or {}
    if home_standing.get("position") is None or away_standing.get("position") is None:
        return False
    return True


def _select_stat_picks(picks: list[dict], limit: int = 2) -> tuple[dict | None, list[dict]]:
    if not picks:
        return None, []
    enrich_limit = int(os.environ.get("STAT_ONLY_ENRICH_LIMIT", "4"))
    if enrich_limit < limit:
        enrich_limit = limit
    enriched: list[dict] = []
    idx = 0
    while idx < len(picks) and len(enriched) < enrich_limit:
        enriched.append(_enrich_pick(picks[idx]))
        idx += 1
    while idx < len(picks) and len(enriched) < limit:
        enriched.append(_enrich_pick(picks[idx]))
        idx += 1
    complete = [item for item in enriched if _is_pick_complete(item)]
    complete = _filter_picks_by_risk(complete)
    remaining = [item for item in enriched if item not in complete]
    remaining = _filter_picks_by_risk(remaining)
    ranked = complete + remaining
    # Hard diversify: take the single best pick per market first.
    market_best: dict[str, dict] = {}
    for item in ranked:
        market_key = str(item.get("market_key") or "")
        if not market_key:
            continue
        prev = market_best.get(market_key)
        if not prev or float(item.get("score", 0.0)) > float(prev.get("score", 0.0)):
            market_best[market_key] = item
    diversified = sorted(market_best.values(), key=lambda it: float(it.get("score", 0.0)), reverse=True)
    selected: list[dict] = diversified[:limit]
    if len(selected) < limit:
        for item in ranked:
            if item in selected:
                continue
            selected.append(item)
            if len(selected) >= limit:
                break
    if not complete:
        for item in selected:
            item["notice"] = "NINCS ELEG STAT, AZ AJANLAS KORLATOZOTT"
    else:
        for item in selected:
            if not _is_pick_complete(item):
                item["notice"] = "RESZLEGES ADATOK MIATT KORLATOZOTT"
    best_pick = max(selected, key=lambda item: item.get("score", 0.0)) if selected else enriched[0]
    return best_pick, selected


def _ensure_pick_fields(pick: dict | None) -> dict | None:
    if not pick:
        return None
    if not isinstance(pick.get("home_summary"), dict) or "win_rate" not in pick.get("home_summary", {}):
        pick["home_summary"] = _placeholder_summary()
    if not isinstance(pick.get("away_summary"), dict) or "win_rate" not in pick.get("away_summary", {}):
        pick["away_summary"] = _placeholder_summary()
    pick.setdefault("value", 0.0)
    pick.setdefault("model_prob", 0.0)
    pick.setdefault("implied_prob", 0.0)
    pick.setdefault("ev", 0.0)
    pick.setdefault("xg_diff", 0.0)
    pick.setdefault("lineup_diff", 0.0)
    pick.setdefault("news_score", 0.0)
    pick.setdefault("score", 0.0)
    pick.setdefault("odds", 0.0)
    pick.setdefault("market_label", "")
    pick.setdefault("commence_time", "")
    return pick


def _placeholder_summary() -> dict:
    return {
        "team": "FrissĂ­tĂ©s alatt",
        "win_rate": 0.0,
        "goals_for_avg": 0.0,
        "btts_rate": None,
        "over25_rate": None,
        "corners_avg": None,
        "cards_avg": None,
        "source": "n/a",
        "matches": [],
    }


def _placeholder_standing() -> dict:
    return {
        "position": None,
        "points": None,
        "played": None,
        "won": None,
        "draw": None,
        "lost": None,
        "goals_for": None,
        "goals_against": None,
    }


def _placeholder_pick() -> dict:
    pick = {
        "match_key": "placeholder",
        "home_team": "FrissĂ­tĂ©s alatt",
        "away_team": "AdatgyĹ±jtĂ©s folyamatban",
        "competition": "FrissĂ­tĂ©s",
        "market_key": "h2h",
        "market_label": "ĂltalĂˇnos",
        "outcome": "Javaslat kĂ©szĂĽl",
        "line": 0.0,
        "story": "Adatok feltĂ¶ltĂ©se folyamatban, kĂ©rjĂĽk vĂˇrjon.",
        "weather": {},
        "home_summary": _placeholder_summary(),
        "away_summary": _placeholder_summary(),
        "home_standing": _placeholder_standing(),
        "away_standing": _placeholder_standing(),
    }
    return _ensure_pick_fields(pick) or pick


def _enforce_tip_presence(best_pick: dict | None, target_matches: list[dict]) -> tuple[dict | None, list[dict]]:
    normalized_targets: list[dict] = []
    for item in target_matches:
        ensured = _ensure_pick_fields(item)
        if ensured and ensured.get("match_key") != "placeholder":
            normalized_targets.append(ensured)
    best_pick = _ensure_pick_fields(best_pick)
    if best_pick and best_pick.get("match_key") == "placeholder":
        best_pick = None
    if normalized_targets and not best_pick:
        best_pick = normalized_targets[0]
    if best_pick and not normalized_targets:
        normalized_targets = [best_pick]
    return best_pick, normalized_targets


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


def _fetch_rss_items(team_names: list[str] | None = None) -> list[dict]:
    if FAST_MODE:
        return []
    now = time.time()
    cached_at = float(_RSS_CACHE.get("fetched_at", 0.0))
    if now - cached_at < RSS_CACHE_TTL_SECONDS:
        return list(_RSS_CACHE.get("items", []))

    items: list[dict] = []
    sources: list[dict] = []
    if team_names:
        sources = _team_rss_sources(team_names)
        if not sources:
            sources = _load_rss_sources()
    else:
        sources = _load_rss_sources()
    if sources:
        seen_urls = set()
        deduped = []
        for source in sources:
            url = str(source.get("url") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            deduped.append(source)
        sources = deduped
    for source in sources:
        url = source.get("url", "")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        weight = float(source.get("weight", 0.5))
        try:
            response = _http_get(url, api="rss", timeout=8)
            if response.status_code == 200:
                for item in _parse_rss_items(response.text):
                    item["weight"] = weight
                    item["source"] = source.get("label", url)
                    items.append(item)
        except Exception:
            pass
    max_items = int(os.environ.get("RSS_MAX_ITEMS", "80"))
    if max_items > 0:
        items = items[:max_items]

    _RSS_CACHE["fetched_at"] = now
    _RSS_CACHE["items"] = items
    return items


def _translate_via_google(text: str) -> str:
    try:
        response = _http_get(
            "https://translate.googleapis.com/translate_a/single",
            params={
                "client": "gtx",
                "sl": TRANSLATE_SOURCE or "auto",
                "tl": TRANSLATE_TARGET or "hu",
                "dt": "t",
                "q": text,
            },
            timeout=TRANSLATE_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            return text
        data = response.json()
        if not isinstance(data, list) or not data:
            return text
        chunks: list[str] = []
        for item in data[0]:
            if isinstance(item, list) and item:
                chunks.append(str(item[0]))
        translated = "".join(chunks).strip()
        return translated or text
    except Exception:
        return text


def _translate_text(text: str) -> str:
    if not text:
        return text
    cache_key = text.strip()
    cached = _cache_get(_TRANSLATE_CACHE, cache_key, 86400)
    if isinstance(cached, str):
        return cached
    if TRANSLATE_API_URL:
        payload = {
            "q": text,
            "source": TRANSLATE_SOURCE or "auto",
            "target": TRANSLATE_TARGET or "hu",
            "format": "text",
        }
        if TRANSLATE_API_KEY:
            payload["api_key"] = TRANSLATE_API_KEY
        try:
            _note_api_call("translate")
            response = requests.post(TRANSLATE_API_URL, json=payload, timeout=TRANSLATE_TIMEOUT_SECONDS)
            if response.status_code == 200:
                data = response.json()
                translated = data.get("translatedText") or data.get("translation") or text
                _cache_set(_TRANSLATE_CACHE, cache_key, translated)
                return translated
        except Exception:
            pass
    translated = _translate_via_google(text)
    _cache_set(_TRANSLATE_CACHE, cache_key, translated)
    return translated


def _news_summary_from_items(items: list[dict], limit: int = 8) -> str:
    if not items:
        return "Nincs relevans hir a ket csapatrol."
    lines = []
    for item in items[:limit]:
        title = str(item.get("title_hu") or item.get("title") or "").strip()
        summary = str(item.get("summary_hu") or item.get("summary") or "").strip()
        if summary and summary != title:
            lines.append(f"- {title}: {summary}")
        else:
            lines.append(f"- {title}")
    return "\n".join(lines)


def _rss_player_tokens_from_pick(pick: dict) -> set[str]:
    tokens: set[str] = set()
    for key in ("home_players", "away_players"):
        players = pick.get(key) or []
        for name in players:
            words = [w.lower() for w in re.split(r"\W+", str(name)) if len(w) > 2]
            tokens.update(words)
    return tokens


def _news_blocks(picks: list[dict], rss_items: list[dict], limit: int = 10) -> list[dict]:
    if FAST_MODE:
        return []
    blocks = []
    if not picks or not rss_items:
        return blocks
    max_translate = int(os.environ.get("TRANSLATE_MAX_ITEMS", "3"))
    for pick in picks:
        home = pick.get("home_team", "")
        away = pick.get("away_team", "")
        extra_tokens = _rss_player_tokens_from_pick(pick)
        items = _news_items_for_match(home, away, rss_items, limit=limit, extra_tokens=extra_tokens)
        summary = _news_summary_text(home, away, len(items))
        translated_items = []
        for idx, item in enumerate(items):
            if idx < max_translate:
                title_hu = _translate_text(str(item.get("title", "")))
                summary_hu = _translate_text(str(item.get("summary", "")))
            else:
                title_hu = str(item.get("title", ""))
                summary_hu = str(item.get("summary", ""))
            translated_items.append(
                {
                    **item,
                    "title_hu": title_hu,
                    "summary_hu": summary_hu,
                }
            )
        blocks.append(
            {
                "match": f"{home} vs {away}",
                "summary": _translate_text(summary),
                "summary_hu": _news_summary_from_items(translated_items),
                "items": translated_items,
            }
        )
    return blocks


def _news_summary_text(home: str, away: str, count: int) -> str:
    if count <= 0:
        return "Nincs relevans hir a ket csapatrol."
    return f"Osszefoglalo: {home} es {away} kapcsan {count} relevans hir talalhato."


def _news_items_for_match(
    home: str,
    away: str,
    rss_items: list[dict],
    limit: int = 10,
    extra_tokens: set[str] | None = None,
) -> list[dict]:
    if not rss_items:
        return []
    home_norm = _normalize_team_name(home)
    away_norm = _normalize_team_name(away)
    stopwords = {
        "afc",
        "fc",
        "cf",
        "sc",
        "ac",
        "cd",
        "ud",
        "rc",
        "bc",
        "cc",
        "if",
        "club",
        "city",
        "united",
        "sporting",
        "kv",
        "fk",
        "sfp",
        "sv",
        "ss",
        "as",
        "ts",
        "madrid",
        "barcelona",
        "manchester",
        "london",
        "milan",
        "rome",
        "roma",
        "turin",
        "napoli",
        "porto",
        "paris",
        "lyon",
        "marseille",
        "sevilla",
        "valencia",
        "istanbul",
    }
    raw_tokens = [t.lower() for t in re.split(r"\W+", f"{home} {away}") if len(t) > 2]
    team_tokens = {t for t in raw_tokens if t not in stopwords}
    team_ids = _team_id_map()
    home_id = team_ids.get(_normalize_name(home))
    away_id = team_ids.get(_normalize_name(away))
    player_tokens = _team_squad_tokens(home_id) | _team_squad_tokens(away_id)
    player_tokens = {t for t in player_tokens if t and len(t) > 3 and t not in stopwords}
    tokens = team_tokens | player_tokens | (extra_tokens or set())
    if not tokens:
        return []
    team_phrases = {
        _normalize_news_text(home),
        _normalize_news_text(away),
    }
    matched = []
    seen = set()
    other_tokens = tokens - team_tokens
    for item in rss_items:
        title = (item.get("title") or "").lower()
        summary = (item.get("summary") or "").lower()
        text = f"{title} {summary}".strip()
        if not text:
            continue
        text_norm = _normalize_news_text(text)
        text_tokens = {t for t in re.split(r"\W+", text) if len(t) > 2}
        has_team = bool(team_tokens.intersection(text_tokens)) if team_tokens else False
        has_other = bool(other_tokens.intersection(text_tokens)) if other_tokens else False
        has_phrase = any(phrase and phrase in text_norm for phrase in team_phrases)
        if team_tokens and not has_team and not has_other and not has_phrase:
            continue
        if tokens.intersection(text_tokens):
            key = (item.get("title"), item.get("source"))
            if key in seen:
                continue
            seen.add(key)
            matched.append(item)
        if len(matched) >= limit:
            break
    return matched


def _data_coverage_for_pick(pick: dict) -> float:
    if not pick:
        return 0.0
    total = 6
    score = 0
    home_summary = pick.get("home_summary") or {}
    away_summary = pick.get("away_summary") or {}
    home_matches = home_summary.get("matches") or []
    away_matches = away_summary.get("matches") or []
    if len(home_matches) >= 3:
        score += 1
    if len(away_matches) >= 3:
        score += 1
    if pick.get("home_standing"):
        score += 1
    if pick.get("away_standing"):
        score += 1
    if home_summary.get("goals_for_avg") is not None:
        score += 1
    if away_summary.get("goals_for_avg") is not None:
        score += 1
    return score / max(1, total)


def _efficiency_score(picks: list[dict], rss_items: list[dict], cached_updated_at: str | None) -> float:
    if not picks:
        return 0.0
    best_pick = picks[0]
    coverage = _data_coverage_for_pick(best_pick)
    news = 1.0 if _news_items_for_match(best_pick.get("home_team", ""), best_pick.get("away_team", ""), rss_items, limit=1) else 0.0
    freshness = 0.3
    if cached_updated_at:
        cached_dt = _parse_iso_datetime(cached_updated_at)
        if cached_dt:
            freshness = 1.0 if (datetime.now(timezone.utc) - cached_dt).total_seconds() <= 60 * 60 * 30 else 0.6
    pick_count = 1.0 if len(picks) >= 2 else 0.6
    score = 10.0 * (0.55 * coverage + 0.2 * freshness + 0.15 * news + 0.1 * pick_count)
    return max(0.0, min(10.0, round(score, 1)))


def _team_tokens(team_name: str) -> list[str]:
    lowered = team_name.lower()
    tokens = [lowered]
    tokens.append(re.sub(r"\b(fc|cf|afc)\b", "", lowered).strip())
    return [token for token in tokens if token]


def _normalize_news_text(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value).lower())
    return re.sub(r"\s+", " ", text).strip()


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
        response = _http_get(
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
    if home_team in _WEATHER_CACHE and "factor" in _WEATHER_CACHE[home_team]:
        return float(_WEATHER_CACHE[home_team]["factor"])
    details = _weather_details(home_team)
    if details and details.get("precip_prob_max") is not None:
        high_rain = details["precip_prob_max"] >= 50
        factor = -0.1 if high_rain else 0.0
        _WEATHER_CACHE.setdefault(home_team, {})["factor"] = factor
        return factor
    _WEATHER_CACHE.setdefault(home_team, {})["factor"] = 0.0
    return 0.0


def _weather_details(home_team: str) -> dict:
    if not home_team:
        return {}
    if FAST_MODE:
        return {"precip_prob_max": None, "precip_max": None, "temp_min": None, "temp_max": None}
    cached = _WEATHER_CACHE.get(home_team)
    if cached and any(k in cached for k in ("precip_prob_max", "temp_min", "temp_max")):
        return cached
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
        _WEATHER_CACHE[home_team] = {"precip_prob_max": None, "precip_max": None, "temp_min": None, "temp_max": None}
        return _WEATHER_CACHE[home_team]
    try:
        response = _http_get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": result["latitude"],
                "longitude": result["longitude"],
                "hourly": "precipitation_probability,precipitation,temperature_2m",
                "forecast_days": 1,
            },
            timeout=8,
        )
        if response.status_code == 200:
            data = response.json()
            hourly = data.get("hourly", {})
            probs = hourly.get("precipitation_probability", []) or []
            precs = hourly.get("precipitation", []) or []
            temps = hourly.get("temperature_2m", []) or []
            details = {
                "precip_prob_max": max(probs) if probs else None,
                "precip_max": max(precs) if precs else None,
                "temp_min": min(temps) if temps else None,
                "temp_max": max(temps) if temps else None,
            }
            _WEATHER_CACHE[home_team] = details
            return details
    except Exception:
        pass
    _WEATHER_CACHE[home_team] = {"precip_prob_max": None, "precip_max": None, "temp_min": None, "temp_max": None}
    return _WEATHER_CACHE[home_team]
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
        response = _http_get(
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


def _build_story(pick: dict) -> str:
    home = pick.get("home_team") or "Hazai"
    away = pick.get("away_team") or "Vendeg"
    outcome = pick.get("outcome") or "Ajanlas"
    market_label = pick.get("market_label") or "piac"
    home_summary = pick.get("home_summary") or {}
    away_summary = pick.get("away_summary") or {}
    home_standing = pick.get("home_standing") or {}
    away_standing = pick.get("away_standing") or {}
    home_win = home_summary.get("win_rate")
    away_win = away_summary.get("win_rate")
    home_goals = home_summary.get("goals_for_avg")
    away_goals = away_summary.get("goals_for_avg")
    base_sentence = f"A tipp: {outcome} ({market_label})."

    sentences = [base_sentence]
    if home_standing or away_standing:
        home_pos = home_standing.get("position") or "n/a"
        away_pos = away_standing.get("position") or "n/a"
        home_pts = home_standing.get("points") or "n/a"
        away_pts = away_standing.get("points") or "n/a"
        point_diff = None
        try:
            point_diff = float(home_pts) - float(away_pts)
        except Exception:
            point_diff = None
        sentences.append(
            f"Tabella: {home} ({home_pos}. hely, {home_pts} pont) vs "
            f"{away} ({away_pos}. hely, {away_pts} pont)"
            + (f", kulonbseg {point_diff:+.0f} pont." if point_diff is not None else ".")
        )
    if home_win is not None or away_win is not None:
        home_pct = f"{(home_win or 0.0) * 100:.0f}%" if home_win is not None else "n/a"
        away_pct = f"{(away_win or 0.0) * 100:.0f}%" if away_win is not None else "n/a"
        sentences.append(f"Forma (utobbi meccsek): gyozelmi arany {home_pct} vs {away_pct}.")
    if home_goals is not None or away_goals is not None:
        home_g = f"{home_goals:.2f}" if home_goals is not None else "n/a"
        away_g = f"{away_goals:.2f}" if away_goals is not None else "n/a"
        sentences.append(f"Rugott gol atlag: {home_g} vs {away_g}.")
    btts_rate = pick.get("btts_rate")
    over25_rate = pick.get("over25_rate")
    if isinstance(btts_rate, (int, float)) and btts_rate > 0:
        sentences.append(f"GG arany: {btts_rate:.0%}.")
    if isinstance(over25_rate, (int, float)) and over25_rate > 0:
        sentences.append(f"Over 2.5 arany: {over25_rate:.0%}.")
    home_corners = home_summary.get("corners_avg")
    away_corners = away_summary.get("corners_avg")
    if home_corners is not None or away_corners is not None:
        home_c = f"{home_corners:.2f}" if home_corners is not None else "n/a"
        away_c = f"{away_corners:.2f}" if away_corners is not None else "n/a"
        sentences.append(f"Szoglet atlag: {home_c} vs {away_c}.")
    home_cards = home_summary.get("cards_avg")
    away_cards = away_summary.get("cards_avg")
    if home_cards is not None or away_cards is not None:
        home_l = f"{home_cards:.2f}" if home_cards is not None else "n/a"
        away_l = f"{away_cards:.2f}" if away_cards is not None else "n/a"
        sentences.append(f"Lap atlag: {home_l} vs {away_l}.")
    home_season = home_summary.get("season_win_rate")
    home_recent = home_summary.get("recent_win_rate")
    away_season = away_summary.get("season_win_rate")
    away_recent = away_summary.get("recent_win_rate")
    trend_notes = []
    if isinstance(home_season, (int, float)) and isinstance(home_recent, (int, float)):
        if home_recent - home_season >= 0.15:
            trend_notes.append(f"{home} forma feljovo")
        elif home_season - home_recent >= 0.15:
            trend_notes.append(f"{home} forma visszaeso")
    if isinstance(away_season, (int, float)) and isinstance(away_recent, (int, float)):
        if away_recent - away_season >= 0.15:
            trend_notes.append(f"{away} forma feljovo")
        elif away_season - away_recent >= 0.15:
            trend_notes.append(f"{away} forma visszaeso")
    if trend_notes:
        sentences.append("Trend: " + ", ".join(trend_notes) + ".")
    home_players = pick.get("home_player_count")
    away_players = pick.get("away_player_count")
    if isinstance(home_players, int) or isinstance(away_players, int):
        home_cnt = str(home_players) if isinstance(home_players, int) else "n/a"
        away_cnt = str(away_players) if isinstance(away_players, int) else "n/a"
        sentences.append(f"Keretmeret: {home_cnt} vs {away_cnt}.")
    if pick.get("is_live"):
        sentences.append("Elo meccs, aktualis status szerint frissulhet.")
    sr_event = pick.get("sr_event") or {}
    if isinstance(sr_event, dict) and sr_event.get("status"):
        status = sr_event.get("status")
        sentences.append(f"Meccs statusz: {status}.")

    market_label = pick.get("market_label") or pick.get("market") or ""
    selection = pick.get("selection") or pick.get("outcome") or ""
    if market_label and selection:
        sentences.append(
            f"A valasztott piac a legmagasabb valoszinusegu opcio volt: {selection} ({market_label})."
        )
    model_prob = pick.get("model_prob")
    if not isinstance(model_prob, (int, float)):
        model_prob = pick.get("score")
    if isinstance(model_prob, (int, float)):
        sentences.append(f"Modell esely: {model_prob:.0%}.")
    missing_parts = []
    if not home_standing and not away_standing:
        missing_parts.append("tabella")
    if home_win is None and away_win is None:
        missing_parts.append("forma")
    if home_goals is None and away_goals is None:
        missing_parts.append("gol atlag")
    if missing_parts:
        sentences.append("Hianyzo adatok: " + ", ".join(missing_parts) + ".")
    return " ".join(sentences)


def _build_picks_for_match(
    match: dict,
    target: float,
    news_items: list[dict],
    db,
    form_scores: dict[str, float],
    table_scores: dict[str, float],
    roi_map: dict[str, float],
) -> list[dict]:
    picks: list[dict] = []
    home_team = match.get("home_team", "")
    away_team = match.get("away_team", "")
    comp_name = _match_competition(match)
    comp_type = match.get("competition", {}).get("type")
    if RISK_EXCLUDE_CUP and _is_cup_comp(comp_name, None, comp_type, comp_name):
        return picks
    if RISK_EXCLUDE_DERBY and home_team and away_team and _is_rivalry(home_team, away_team):
        return picks
    comp_code = _sport_key_to_comp(str(match.get("sport_key") or ""))
    if comp_code and home_team and away_team:
        _TEAM_COMP_HINT[_normalize_team_name(home_team)] = str(comp_code)
        _TEAM_COMP_HINT[_normalize_team_name(away_team)] = str(comp_code)

    comp_standings = match.get("comp_standings") or []
    standings_index = {}
    for row in comp_standings:
        team = row.get("team")
        if team:
            standings_index[_normalize_team_name(team)] = row

    odds_markets = match.get("odds_markets") or match.get("therundown_markets") or _build_odds_markets_from_match(match)
    odds_payload = {"markets": odds_markets} if odds_markets else None
    picks_v2 = score_fixture(
        fixture_id=str(match.get("id") or ""),
        home_team=home_team,
        away_team=away_team,
        standings=standings_index,
        odds=odds_payload,
        stats=None,
        events=None,
    )
    for pick in picks_v2:
        market_key = _market_key_from_pick(pick.market)
        odds = _odds_for_pick(market_key, pick.outcome, odds_markets)
        odds_value = float(odds) if isinstance(odds, (int, float)) else 0.0
        picks.append(
            {
                "match_key": _match_key(match),
                "home_team": home_team,
                "away_team": away_team,
                "competition": _match_competition(match),
                "market_key": market_key,
                "market_label": _market_label(market_key),
                "outcome": pick.outcome,
                "line": None,
                "odds": odds_value,
                "distance": abs(odds_value - target),
                "score": pick.score,
                "risk": _risk_label(pick.score),
                "explain_hu": pick.explanation_hu,
                "model_prob": pick.model_prob,
                "implied_prob": pick.implied_prob,
                "ev": pick.ev,
                "value": pick.value,
            }
        )
    picks = [item for item in picks if _passes_value_filters(item)]
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
    cache_key = f"{comp_code}:{limit}"
    cached = _cache_get(_FD_RECENT_CACHE, cache_key, FD_RECENT_TTL_SECONDS)
    if isinstance(cached, list):
        return cached
    try:
        response = _http_get(
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
        matches = matches[:limit]
        _cache_set(_FD_RECENT_CACHE, cache_key, matches)
        return matches
    except Exception:
        return []


def _fetch_standings_api_football(api_key: str, league_id: int | None, season: int) -> list[dict]:
    if not api_key or not league_id:
        return []
    cache_key = f"af:{league_id}:{season}"
    cached = _cache_get(_FD_STANDINGS_CACHE, cache_key, FD_STANDINGS_TTL_SECONDS)
    if isinstance(cached, list):
        return cached
    try:
        response = _http_get(
            "https://v3.football.api-sports.io/standings",
            headers={"x-apisports-key": api_key},
            params={"league": str(league_id), "season": str(season)},
            timeout=12,
        )
        if response.status_code != 200:
            return []
        data = response.json()
        standings = data.get("response", [])
        if not standings:
            return []
        table = standings[0].get("league", {}).get("standings", [])
        rows = []
        for group in table:
            for row in group:
                team = row.get("team", {}).get("name")
                rank = row.get("rank")
                points = row.get("points")
                all_stats = row.get("all", {})
                if team and rank is not None and points is not None:
                    rows.append(
                        {
                            "position": rank,
                            "team": team,
                            "team_id": row.get("team", {}).get("id"),
                            "points": points,
                            "played": all_stats.get("played"),
                            "won": all_stats.get("win"),
                            "draw": all_stats.get("draw"),
                            "lost": all_stats.get("lose"),
                            "goals_for": all_stats.get("goals", {}).get("for"),
                            "goals_against": all_stats.get("goals", {}).get("against"),
                        }
                    )
        _cache_set(_FD_STANDINGS_CACHE, cache_key, rows)
        return rows
    except Exception:
        return []




def _diagnostics_fixtures(token: str, competitions: list[dict], hours: int = 24) -> dict:
    all_fixtures = _fetch_upcoming_fixtures_fd_all(token, hours)
    comps_count = len(all_fixtures)
    all_count = len(all_fixtures)
    api_football_count = len(_fetch_upcoming_fixtures_api_football(config.api_football_key, hours))
    sportradar_count = len(_fetch_upcoming_fixtures_sportradar(config.sportradar_api_key, hours))
    _, date_from, date_to = _fixture_window(hours)
    return {
        "comp_24": comps_count,
        "all_24": all_count,
        "api_football_24": api_football_count,
        "sportradar_24": sportradar_count,
        "window_from": date_from,
        "window_to": date_to,
        "window_source": _LAST_WINDOW_INFO.get("source", "system"),
        "api_football_enabled": bool(config.api_football_key),
        "sportradar_enabled": bool(config.sportradar_api_key),
    }




def _fixture_window(hours: int) -> tuple[datetime, str, str]:
    override = (os.environ.get("FIXTURES_DATE", "") or os.environ.get("SOCCER_DATE_OVERRIDE", "")).strip()
    if override:
        try:
            base = datetime.fromisoformat(override).replace(tzinfo=timezone.utc)
            source = "override"
        except Exception:
            base = datetime.now(timezone.utc)
            source = "system"
    else:
        base = datetime.now(timezone.utc)
        source = "system"

    # Optional override to force server time when needed.
    if not override and os.environ.get("FORCE_SERVER_TIME", "") == "1":
        server_time = _server_time_utc()
        if server_time:
            base = server_time
            source = _SERVER_TIME_SOURCE

    days_ahead = max(1, int(math.ceil(hours / 24)))
    date_from = base.date().isoformat()
    date_to = (base + timedelta(days=days_ahead)).date().isoformat()
    _LAST_WINDOW_INFO["from"] = date_from
    _LAST_WINDOW_INFO["to"] = date_to
    _LAST_WINDOW_INFO["source"] = source
    return base, date_from, date_to

def _fetch_upcoming_fixtures_api_football(api_key: str, hours: int = 24, limit: int = 40) -> list[dict]:
    if not api_key:
        return []
    if _backoff_active("api-football"):
        return []
    cache_key = f"{hours}:{limit}"
    cached = _cache_get(_AF_FIXTURES_CACHE, cache_key, AF_FIXTURES_TTL_SECONDS)
    if isinstance(cached, list):
        return cached
    try:
        now, date_from, date_to = _fixture_window(hours)
        fixtures = []

        def _fetch(params: dict) -> list[dict]:
            response = _http_get(
                "https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": api_key},
                params=params,
                timeout=12,
            )
            if response.status_code != 200:
                if response.status_code == 429:
                    _note_backoff("api-football")
                return []
            data = response.json()
            if data.get("errors"):
                if data.get("errors", {}).get("requests"):
                    _note_backoff("api-football")
                return []
            return data.get("response", [])

        params = {"date": date_from, "timezone": "UTC", "status": "NS"}
        items = _fetch(params)
        if date_to != date_from:
            params2 = {"date": date_to, "timezone": "UTC", "status": "NS"}
            items.extend(_fetch(params2))
        if not items:
            items = _fetch({"next": 100, "timezone": "UTC", "status": "NS"})

        for item in items:
            fixture = item.get("fixture", {})
            teams = item.get("teams", {})
            league = item.get("league", {})
            utc_date = fixture.get("date")
            if not utc_date:
                continue
            try:
                commence_dt = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
            except Exception:
                continue
            delta = commence_dt - now
            if delta.total_seconds() < 0 or delta.total_seconds() > hours * 3600:
                continue
            home = teams.get("home", {})
            away = teams.get("away", {})
            home_name = home.get("name")
            away_name = away.get("name")
            if not home_name or not away_name:
                continue
            comp_code = league.get("id") or league.get("name") or ""
            comp_name = league.get("name") or ""
            comp_country = league.get("country") or ""
            comp_season = league.get("season")
            if _is_friendly_comp(comp_name, str(comp_code), league.get("type")):
                continue
            fixtures.append(
                {
                    "id": fixture.get("id"),
                    "sport_key": str(comp_code),
                    "comp_code": str(comp_code),
                    "comp_name": str(comp_name),
                    "comp_country": str(comp_country),
                    "season": comp_season,
                    "commence_time": utc_date,
                    "home_team": home_name,
                    "away_team": away_name,
                    "home_id": home.get("id"),
                    "away_id": away.get("id"),
                }
            )
        fixtures = fixtures[:limit]
        _cache_set(_AF_FIXTURES_CACHE, cache_key, fixtures)
        return fixtures
    except Exception:
        return []


def _parse_sportradar_event(item: dict) -> dict | None:
    event = item.get("sport_event") if isinstance(item.get("sport_event"), dict) else item
    if not isinstance(event, dict):
        return None
    start_time = event.get("start_time") or event.get("scheduled") or event.get("start_time_utc")
    if not start_time:
        return None
    context = event.get("sport_event_context") if isinstance(event.get("sport_event_context"), dict) else {}
    tournament = event.get("tournament") or context.get("competition") or {}
    category = context.get("category") if isinstance(context, dict) else {}
    competitors = event.get("competitors") or []
    home_name = None
    away_name = None
    home_id = None
    away_id = None
    if isinstance(competitors, list):
        for comp in competitors:
            if not isinstance(comp, dict):
                continue
            qualifier = str(comp.get("qualifier") or "").lower()
            name = comp.get("name")
            if qualifier == "home":
                home_name = name
                home_id = comp.get("id")
            elif qualifier == "away":
                away_name = name
                away_id = comp.get("id")
    if not home_name or not away_name:
        return None
    comp_name = ""
    comp_country = ""
    season_id = ""
    if isinstance(tournament, dict):
        comp_name = str(tournament.get("name") or "")
        if isinstance(category, dict):
            comp_country = str(category.get("name") or "")
    if isinstance(context, dict):
        season_id = str(context.get("season", {}).get("id") or "")
    return {
        "id": event.get("id"),
        "sport_key": str(tournament.get("id") or comp_name),
        "comp_code": str(tournament.get("id") or ""),
        "comp_name": comp_name,
        "comp_country": comp_country,
        "season": context.get("season", {}).get("name") if isinstance(context, dict) else None,
        "sr_season_id": season_id,
        "sr_competition_id": str(tournament.get("id") or ""),
        "commence_time": str(start_time),
        "home_team": home_name,
        "away_team": away_name,
        "home_id": home_id,
        "away_id": away_id,
    }


def _fetch_upcoming_fixtures_sportradar(api_key: str, hours: int = 24, limit: int = 40) -> list[dict]:
    if not api_key:
        return []
    cache_key = f"{hours}:{limit}"
    cached = _cache_get(_SR_FIXTURES_CACHE, cache_key, SR_FIXTURES_TTL_SECONDS)
    if isinstance(cached, list):
        return cached
    try:
        now, date_from, date_to = _fixture_window(hours)
        fixtures: list[dict] = []

        def _fetch(date_value: str) -> list[dict]:
            path = SPORTRADAR_SCHEDULE_PATH.format(date=date_value)
            response = _sr_get(path, timeout=12)
            if response.status_code != 200:
                if response.status_code == 429:
                    _note_backoff("sportradar")
                return []
            payload = response.json()
            events = payload.get("sport_events") or payload.get("schedules") or []
            if not isinstance(events, list):
                return []
            return events

        events = _fetch(date_from)
        if date_to != date_from:
            events.extend(_fetch(date_to))
        for item in events:
            parsed = _parse_sportradar_event(item)
            if not parsed:
                continue
            commence = _parse_match_dt(parsed)
            if not commence:
                continue
            delta = commence - now
            if delta.total_seconds() < 0 or delta.total_seconds() > hours * 3600:
                continue
            fixtures.append(parsed)
        fixtures = fixtures[:limit]
        _cache_set(_SR_FIXTURES_CACHE, cache_key, fixtures)
        return fixtures
    except Exception:
        return []


def _normalize_prob_value(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        val = float(value)
        if val > 1.0:
            val = val / 100.0
        return max(0.0, min(1.0, val))
    try:
        val = float(str(value).strip().replace("%", ""))
        if val > 1.0:
            val = val / 100.0
        return max(0.0, min(1.0, val))
    except Exception:
        return None


def _extract_probabilities(item: dict) -> dict[str, float]:
    if not isinstance(item, dict):
        return {}
    raw = item.get("probabilities") or item.get("sport_event_probabilities") or {}
    if not isinstance(raw, dict):
        return {}
    home = _normalize_prob_value(raw.get("home_win") or raw.get("home") or raw.get("home_probability"))
    draw = _normalize_prob_value(raw.get("draw") or raw.get("tie") or raw.get("draw_probability"))
    away = _normalize_prob_value(raw.get("away_win") or raw.get("away") or raw.get("away_probability"))
    probs = {}
    if home is not None:
        probs["home"] = home
    if draw is not None:
        probs["draw"] = draw
    if away is not None:
        probs["away"] = away
    return probs


def _fetch_upcoming_probabilities_sportradar() -> dict[str, dict[str, float]]:
    if not config.sportradar_api_key or not SR_ENABLE_PROB:
        return {}
    if _backoff_active("sportradar"):
        return {}
    cache_key = "upcoming"
    cached = _cache_get(_SR_PROB_CACHE, cache_key, SR_PROB_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    if not _take_sportradar_budget("prob"):
        return {}
    response = _sr_prob_get("sport_events/upcoming_probabilities.json", timeout=12)
    if not response:
        return {}
    if response.status_code == 429:
        _note_backoff("sportradar")
        return {}
    if response.status_code != 200:
        return {}
    try:
        data = response.json()
    except Exception:
        return {}
    events = data.get("sport_events") or data.get("probabilities") or []
    if not isinstance(events, list):
        return {}
    prob_map: dict[str, dict[str, float]] = {}
    for item in events:
        if not isinstance(item, dict):
            continue
        event = item.get("sport_event") if isinstance(item.get("sport_event"), dict) else item.get("event")
        event_id = None
        if isinstance(event, dict):
            event_id = event.get("id")
        event_id = event_id or item.get("sport_event_id") or item.get("event_id")
        if not event_id:
            continue
        probs = _extract_probabilities(item)
        if probs:
            prob_map[str(event_id)] = probs
    _cache_set(_SR_PROB_CACHE, cache_key, prob_map)
    return prob_map


def _sr_probabilities_for_event(event_id: str | None) -> dict[str, float]:
    if not event_id:
        return {}
    cached = _cache_get(_SR_PROB_CACHE, "upcoming", SR_PROB_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached.get(str(event_id), {}) or {}
    return {}


def _fill_sportradar_ids(pick: dict) -> None:
    if not pick or not config.sportradar_api_key:
        return
    if pick.get("sr_season_id"):
        return
    home = pick.get("home_team")
    away = pick.get("away_team")
    if not home or not away:
        return
    fixtures = _fetch_upcoming_fixtures_sportradar(config.sportradar_api_key, 24, SR_MAX_FIXTURES)
    pick_dt = _parse_match_dt(pick)
    home_norm = _normalize_team_name(home)
    away_norm = _normalize_team_name(away)
    for item in fixtures:
        if _normalize_team_name(item.get("home_team", "")) != home_norm:
            continue
        if _normalize_team_name(item.get("away_team", "")) != away_norm:
            continue
        if pick_dt and (dt := _parse_match_dt(item)):
            if abs((dt - pick_dt).total_seconds()) > 7200:
                continue
        if not pick.get("home_id"):
            pick["home_id"] = item.get("home_id")
        if not pick.get("away_id"):
            pick["away_id"] = item.get("away_id")
        if not pick.get("comp_code"):
            pick["comp_code"] = item.get("comp_code")
        pick["sr_season_id"] = item.get("sr_season_id")
        break


def _extract_sr_standings(payload: dict) -> list[dict]:
    rows: list[dict] = []
    standings = payload.get("standings")
    if not isinstance(standings, list):
        return rows
    for entry in standings:
        if not isinstance(entry, dict):
            continue
        groups = entry.get("groups")
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_rows = group.get("standings")
            if not isinstance(group_rows, list):
                continue
            for row in group_rows:
                if not isinstance(row, dict):
                    continue
                comp = row.get("competitor") or {}
                name = str(comp.get("name") or "")
                team_id = comp.get("id")
                if not name:
                    continue
                points = row.get("points")
                rank = row.get("rank") or row.get("position")
                played = row.get("played")
                wins = row.get("win")
                draws = row.get("draw")
                losses = row.get("loss")
                goals_for = row.get("goals_for")
                goals_against = row.get("goals_against")
                ppg = row.get("points_per_game")
                rows.append(
                    {
                        "team": name,
                        "team_id": team_id,
                        "points": int(points) if isinstance(points, (int, float)) else None,
                        "position": int(rank) if isinstance(rank, (int, float)) else None,
                        "played": int(played) if isinstance(played, (int, float)) else None,
                        "won": int(wins) if isinstance(wins, (int, float)) else None,
                        "draw": int(draws) if isinstance(draws, (int, float)) else None,
                        "lost": int(losses) if isinstance(losses, (int, float)) else None,
                        "goals_for": int(goals_for) if isinstance(goals_for, (int, float)) else None,
                        "goals_against": int(goals_against) if isinstance(goals_against, (int, float)) else None,
                        "points_per_game": float(ppg) if isinstance(ppg, (int, float)) else None,
                    }
                )
    return rows


def _fetch_standings_sportradar(season_id: str) -> list[dict]:
    if not season_id:
        return []
    cache_key = f"sr:{season_id}"
    cached = _cache_get(_SR_STANDINGS_CACHE, cache_key, SR_STANDINGS_TTL_SECONDS)
    if isinstance(cached, list):
        return cached
    base_url = (config.sportradar_api_base or "https://api.sportradar.com/soccer/trial/v4/en").rstrip("/")
    url = f"{base_url}/seasons/{season_id}/standings.json"
    try:
        response = _http_get(url, headers={"x-api-key": config.sportradar_api_key, "accept": "application/json"}, timeout=12)
        if response.status_code != 200:
            if response.status_code == 429:
                _note_backoff("sportradar")
            return []
        data = response.json()
        rows = _extract_sr_standings(data)
        _cache_set(_SR_STANDINGS_CACHE, cache_key, rows)
        return rows
    except Exception:
        return []


def _fetch_competitor_stats_sportradar(season_id: str, competitor_id: str) -> dict:
    if not season_id or not competitor_id:
        return {}
    if _backoff_active("sportradar"):
        return {}
    cache_key = f"sr:stats:{season_id}:{competitor_id}"
    cached = _cache_get(_SR_STATS_CACHE, cache_key, SR_STATS_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    base_url = (config.sportradar_api_base or "https://api.sportradar.com/soccer/trial/v4/en").rstrip("/")
    url = f"{base_url}/seasons/{season_id}/competitors/{competitor_id}/statistics.json"
    try:
        response = _http_get(url, headers={"x-api-key": config.sportradar_api_key, "accept": "application/json"}, timeout=12)
        if response.status_code != 200:
            if response.status_code == 429:
                _note_backoff("sportradar")
            return {}
        data = response.json()
        stats = data.get("competitor", {}).get("statistics", {}) if isinstance(data, dict) else {}
        if not isinstance(stats, dict):
            stats = {}
        _cache_set(_SR_STATS_CACHE, cache_key, stats)
        return stats
    except Exception:
        return {}


def _fetch_competitor_profile_sportradar(competitor_id: str) -> dict:
    if not competitor_id or not SR_ENABLE_PLAYER:
        return {}
    cache_key = f"sr:profile:{competitor_id}"
    cached = _cache_get(_SR_COMP_PROFILE_CACHE, cache_key, SR_COMP_PROFILE_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    if not _take_sportradar_budget("player"):
        return {}
    response = _sr_get(f"competitors/{competitor_id}/profile.json", timeout=12)
    if not response or response.status_code != 200:
        if response and response.status_code == 429:
            _note_backoff("sportradar")
        return {}
    data = response.json()
    if not isinstance(data, dict):
        return {}
    _cache_set(_SR_COMP_PROFILE_CACHE, cache_key, data)
    return data


def _fetch_competitor_summary_sportradar(competitor_id: str) -> dict:
    if not competitor_id or not SR_ENABLE_PLAYER:
        return {}
    cache_key = f"sr:summary:{competitor_id}"
    cached = _cache_get(_SR_COMP_SUMMARY_CACHE, cache_key, SR_COMP_SUMMARY_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    if not _take_sportradar_budget("player"):
        return {}
    response = _sr_get(f"competitors/{competitor_id}/summaries.json", timeout=12)
    if not response or response.status_code != 200:
        if response and response.status_code == 429:
            _note_backoff("sportradar")
        return {}
    data = response.json()
    if not isinstance(data, dict):
        return {}
    _cache_set(_SR_COMP_SUMMARY_CACHE, cache_key, data)
    return data


def _fetch_player_profile_sportradar(player_id: str) -> dict:
    if not player_id or not SR_ENABLE_PLAYER:
        return {}
    cache_key = f"sr:player:profile:{player_id}"
    cached = _cache_get(_SR_PLAYER_PROFILE_CACHE, cache_key, SR_PLAYER_PROFILE_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    if not _take_sportradar_budget("player"):
        return {}
    response = _sr_get(f"players/{player_id}/profile.json", timeout=12)
    if not response or response.status_code != 200:
        if response and response.status_code == 429:
            _note_backoff("sportradar")
        return {}
    data = response.json()
    if not isinstance(data, dict):
        return {}
    _cache_set(_SR_PLAYER_PROFILE_CACHE, cache_key, data)
    return data


def _fetch_player_summary_sportradar(player_id: str) -> dict:
    if not player_id or not SR_ENABLE_PLAYER:
        return {}
    cache_key = f"sr:player:summary:{player_id}"
    cached = _cache_get(_SR_PLAYER_SUMMARY_CACHE, cache_key, SR_PLAYER_SUMMARY_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    if not _take_sportradar_budget("player"):
        return {}
    response = _sr_get(f"players/{player_id}/summaries.json", timeout=12)
    if not response or response.status_code != 200:
        if response and response.status_code == 429:
            _note_backoff("sportradar")
        return {}
    data = response.json()
    if not isinstance(data, dict):
        return {}
    _cache_set(_SR_PLAYER_SUMMARY_CACHE, cache_key, data)
    return data


def _fetch_event_summary_sportradar(event_id: str) -> dict:
    if not event_id or not SR_ENABLE_EVENT:
        return {}
    cache_key = f"sr:event:summary:{event_id}"
    cached = _cache_get(_SR_EVENT_CACHE, cache_key, SR_EVENT_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    if not _take_sportradar_budget("event"):
        return {}
    response = _sr_get(f"sport_events/{event_id}/summary.json", timeout=12)
    if not response or response.status_code != 200:
        if response and response.status_code == 429:
            _note_backoff("sportradar")
        return {}
    data = response.json()
    if not isinstance(data, dict):
        return {}
    _cache_set(_SR_EVENT_CACHE, cache_key, data)
    return data


def _fetch_event_timeline_sportradar(event_id: str) -> dict:
    if not event_id or not SR_ENABLE_EVENT:
        return {}
    cache_key = f"sr:event:timeline:{event_id}"
    cached = _cache_get(_SR_EVENT_CACHE, cache_key, SR_EVENT_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    if not _take_sportradar_budget("event"):
        return {}
    response = _sr_get(f"sport_events/{event_id}/timeline.json", timeout=12)
    if not response or response.status_code != 200:
        if response and response.status_code == 429:
            _note_backoff("sportradar")
        return {}
    data = response.json()
    if not isinstance(data, dict):
        return {}
    _cache_set(_SR_EVENT_CACHE, cache_key, data)
    return data


def _fetch_live_schedules_sportradar() -> dict:
    if not SR_ENABLE_LIVE:
        return {}
    cache_key = "sr:live:schedules"
    cached = _cache_get(_SR_LIVE_CACHE, cache_key, SR_LIVE_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    if not _take_sportradar_budget("live"):
        return {}
    try:
        response = _sr_get("schedules/live/schedules.json", timeout=8)
        if not response or response.status_code != 200:
            if response and response.status_code == 429:
                _note_backoff("sportradar")
            return {}
        data = response.json()
        if not isinstance(data, dict):
            return {}
        _cache_set(_SR_LIVE_CACHE, cache_key, data)
        return data
    except Exception:
        return {}


def _fetch_live_summaries_sportradar() -> dict:
    if not SR_ENABLE_LIVE:
        return {}
    cache_key = "sr:live:summaries"
    cached = _cache_get(_SR_LIVE_CACHE, cache_key, SR_LIVE_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    if not _take_sportradar_budget("live"):
        return {}
    try:
        response = _sr_get("schedules/live/summaries.json", timeout=8)
        if not response or response.status_code != 200:
            if response and response.status_code == 429:
                _note_backoff("sportradar")
            return {}
        data = response.json()
        if not isinstance(data, dict):
            return {}
        _cache_set(_SR_LIVE_CACHE, cache_key, data)
        return data
    except Exception:
        return {}


def _fetch_live_timeline_delta_sportradar() -> dict:
    if not SR_ENABLE_LIVE:
        return {}
    cache_key = "sr:live:delta"
    cached = _cache_get(_SR_LIVE_CACHE, cache_key, SR_LIVE_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    if not _take_sportradar_budget("live"):
        return {}
    try:
        response = _sr_get("schedules/live/timelines_delta.json", timeout=8)
        if not response or response.status_code != 200:
            if response and response.status_code == 429:
                _note_backoff("sportradar")
            return {}
        data = response.json()
        if not isinstance(data, dict):
            return {}
        _cache_set(_SR_LIVE_CACHE, cache_key, data)
        return data
    except Exception:
        return {}


def _fetch_mappings_sportradar() -> dict:
    if not SR_ENABLE_MAPPING:
        return {}
    cache_key = "sr:mappings"
    cached = _cache_get(_SR_MAPPING_CACHE, cache_key, SR_MAPPING_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    if not _take_sportradar_budget("mapping"):
        return {}
    data = {}
    try:
        comp_map = _sr_get("competitors/mappings.json", timeout=10)
        if comp_map and comp_map.status_code == 200:
            data["competitors"] = comp_map.json()
        player_map = _sr_get("players/mappings.json", timeout=10)
        if player_map and player_map.status_code == 200:
            data["players"] = player_map.json()
    except Exception:
        return {}
    _cache_set(_SR_MAPPING_CACHE, cache_key, data)
    return data


def _fetch_push_feed_sportradar() -> dict:
    if not SR_ENABLE_PUSH:
        return {}
    cache_key = "sr:push:subscribe"
    cached = _cache_get(_SR_MAPPING_CACHE, cache_key, SR_MAPPING_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    if not _take_sportradar_budget("push"):
        return {}
    try:
        response = _sr_get("stream/events/subscribe", timeout=6, allow_redirects=False)
        if not response:
            return {}
        data = {"status": response.status_code}
        if response.headers.get("Location"):
            data["location"] = response.headers.get("Location")
        _cache_set(_SR_MAPPING_CACHE, cache_key, data)
        return data
    except Exception:
        return {}


def _summarize_sr_event(summary: dict, timeline: dict) -> dict:
    status = ""
    home_score = None
    away_score = None
    if isinstance(summary, dict):
        se_status = summary.get("sport_event_status") or {}
        if isinstance(se_status, dict):
            status = str(se_status.get("status") or se_status.get("match_status") or "")
            home_score = se_status.get("home_score")
            away_score = se_status.get("away_score")
    events_count = 0
    if isinstance(timeline, dict):
        events = timeline.get("events")
        if isinstance(events, list):
            events_count = len(events)
    return {
        "status": status,
        "home_score": home_score,
        "away_score": away_score,
        "events_count": events_count,
    }


def _attach_sportradar_extras(pick: dict) -> None:
    if not pick:
        return
    event_id = pick.get("match_key")
    if isinstance(event_id, str) and event_id.startswith("sr:sport_event:"):
        summary = _fetch_event_summary_sportradar(event_id) if SR_ENABLE_EVENT else {}
        timeline = _fetch_event_timeline_sportradar(event_id) if SR_ENABLE_EVENT else {}
        if summary or timeline:
            pick["sr_event"] = _summarize_sr_event(summary, timeline)
    live_payload = _fetch_live_schedules_sportradar() if SR_ENABLE_LIVE else {}
    if isinstance(live_payload, dict) and live_payload.get("sport_events"):
        live_ids = {str(ev.get("sport_event", {}).get("id") or ev.get("id") or "") for ev in live_payload.get("sport_events", [])}
        if event_id in live_ids:
            pick["is_live"] = True
    if SR_ENABLE_PLAYER:
        for side in ("home", "away"):
            comp_id = pick.get(f"{side}_id")
            if isinstance(comp_id, str) and comp_id.startswith("sr:competitor:"):
                profile = _fetch_competitor_profile_sportradar(comp_id)
                summary = _fetch_competitor_summary_sportradar(comp_id)
                if profile:
                    players = profile.get("players") or []
                    if isinstance(players, list):
                        names = [p.get("name") for p in players if isinstance(p, dict) and p.get("name")]
                        pick[f"{side}_players"] = names[:2]
                        pick[f"{side}_player_count"] = len(names)
                if summary:
                    pick[f"{side}_sr_summary"] = {"tournaments": len(summary.get("summaries") or [])}


def _team_recent_matches_sportradar(competitor_id: str, limit: int = 5) -> list[dict]:
    if not competitor_id:
        return []
    cache_key = f"sr:recent:{competitor_id}"
    cached = _cache_get(_TEAM_RECENT_CACHE, cache_key, TEAM_SUMMARY_TTL_SECONDS)
    if isinstance(cached, list):
        return cached[:limit]
    base_url = (config.sportradar_api_base or "https://api.sportradar.com/soccer/trial/v4/en").rstrip("/")
    url = f"{base_url}/competitors/{competitor_id}/schedules.json"
    try:
        response = _http_get(url, headers={"x-api-key": config.sportradar_api_key, "accept": "application/json"}, timeout=12)
        if response.status_code != 200:
            if response.status_code == 429:
                _note_backoff("sportradar")
            return []
        data = response.json()
        schedules = data.get("schedules") or []
        rows = []
        for item in schedules:
            ev = item.get("sport_event") if isinstance(item, dict) else None
            status = item.get("sport_event_status") if isinstance(item, dict) else None
            if not isinstance(ev, dict) or not isinstance(status, dict):
                continue
            start_time = ev.get("start_time") or ev.get("scheduled")
            if not start_time:
                continue
            match_status = str(status.get("match_status") or status.get("status") or "").lower()
            if match_status not in {"ended", "finished", "closed"}:
                continue
            home_score = status.get("home_score")
            away_score = status.get("away_score")
            if not isinstance(home_score, (int, float)) or not isinstance(away_score, (int, float)):
                continue
            competitors = ev.get("competitors") or []
            home = next((c for c in competitors if isinstance(c, dict) and c.get("qualifier") == "home"), None)
            away = next((c for c in competitors if isinstance(c, dict) and c.get("qualifier") == "away"), None)
            if not home or not away:
                continue
            home_id = home.get("id")
            away_id = away.get("id")
            if competitor_id not in {home_id, away_id}:
                continue
            is_home = competitor_id == home_id
            gf = home_score if is_home else away_score
            ga = away_score if is_home else home_score
            context = ev.get("sport_event_context") if isinstance(ev.get("sport_event_context"), dict) else {}
            comp_name = ""
            if isinstance(context, dict):
                comp = context.get("competition") or {}
                comp_name = str(comp.get("name") or "")
            utc_date = str(start_time)
            date = utc_date.split("T", 1)[0] if "T" in utc_date else utc_date
            result = "D"
            if gf > ga:
                result = "W"
            elif gf < ga:
                result = "L"
            rows.append(
                {
                    "date": date,
                    "opponent": away.get("name") if is_home else home.get("name"),
                    "score": f"{int(gf)}-{int(ga)}",
                    "home": is_home,
                    "competition": comp_name,
                    "gf": int(gf),
                    "ga": int(ga),
                    "result": result,
                }
            )
        rows.sort(key=lambda r: r.get("date", ""), reverse=True)
        _cache_set(_TEAM_RECENT_CACHE, cache_key, rows)
        return rows[:limit]
    except Exception:
        return []
def _fetch_upcoming_fixtures_fd_all(token: str, hours: int = 24, limit: int = 40) -> list[dict]:
    if not token:
        return []
    cache_key = f"all:{hours}:{limit}"
    cached = _cache_get(_FD_FIXTURES_CACHE, cache_key, FD_FIXTURES_TTL_SECONDS)
    if isinstance(cached, list):
        return cached
    if _backoff_active("football-data"):
        return []
    try:
        now, date_from, date_to = _fixture_window(hours)
        response = _http_get(
            "https://api.football-data.org/v4/matches",
            headers={"X-Auth-Token": token},
            params={"status": "SCHEDULED,TIMED", "dateFrom": date_from, "dateTo": date_to},
            timeout=12,
        )
        if response.status_code == 429:
            _note_backoff("football-data")
            return []
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
            comp_name = comp.get("name") or ""
            comp_country = comp.get("area", {}).get("name") or ""
            if not home_name or not away_name:
                continue
            if _is_friendly_comp(comp_name, str(comp_code), comp.get("type")):
                continue
            fixtures.append(
                {
                    "id": match.get("id"),
                    "sport_key": comp_code,
                    "comp_code": comp_code,
                    "comp_name": comp_name,
                    "comp_country": comp_country,
                    "commence_time": utc_date,
                    "home_team": home_name,
                    "away_team": away_name,
                    "home_id": home.get("id"),
                    "away_id": away.get("id"),
                }
            )
        fixtures = fixtures[:limit]
        _cache_set(_FD_FIXTURES_CACHE, cache_key, fixtures)
        return fixtures
    except Exception:
        return []


def _fetch_upcoming_fixtures_fd(token: str, comp_code: str, hours: int = 24, limit: int = 10) -> list[dict]:
    if not token or not comp_code:
        return []
    cache_key = f"{comp_code}:{hours}:{limit}"
    cached = _cache_get(_FD_FIXTURES_CACHE, cache_key, FD_FIXTURES_TTL_SECONDS)
    if isinstance(cached, list):
        return cached
    if _backoff_active("football-data"):
        return []
    try:
        now, date_from, date_to = _fixture_window(hours)
        response = _http_get(
            f"https://api.football-data.org/v4/competitions/{comp_code}/matches",
            headers={"X-Auth-Token": token},
            params={"status": "SCHEDULED,TIMED", "dateFrom": date_from, "dateTo": date_to},
            timeout=12,
        )
        if response.status_code == 429:
            _note_backoff("football-data")
            return []
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
            if _is_friendly_comp(None, str(comp_code), match.get("competition", {}).get("type")):
                continue
            fixtures.append(
                {
                    "id": match.get("id"),
                    "sport_key": comp_code,
                    "comp_code": comp_code,
                    "comp_name": match.get("competition", {}).get("name") or "",
                    "comp_country": match.get("competition", {}).get("area", {}).get("name") or "",
                    "commence_time": utc_date,
                    "home_team": home_name,
                    "away_team": away_name,
                    "home_id": home.get("id"),
                    "away_id": away.get("id"),
                }
            )
        fixtures = fixtures[:limit]
        _cache_set(_FD_FIXTURES_CACHE, cache_key, fixtures)
        return fixtures
    except Exception:
        return []


def _team_ppg_fd(token: str, team_id: int | None, limit: int = 6) -> float:
    if not token or not team_id:
        return 1.5
    cached = _cache_get(_TEAM_PPG_CACHE, str(team_id), TEAM_PPG_TTL_SECONDS)
    if isinstance(cached, (int, float)):
        return float(cached)
    try:
        response = _http_get(
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
        result = total_points / count
        _cache_set(_TEAM_PPG_CACHE, str(team_id), result)
        return result
    except Exception:
        return 1.5


def _team_recent_matches_fd(token: str, team_id: int | None, limit: int = 5) -> list[dict]:
    if not token or not team_id:
        return []
    cache_key = str(team_id)
    cached = _cache_get(_TEAM_RECENT_CACHE, cache_key, TEAM_SUMMARY_TTL_SECONDS)
    if isinstance(cached, list):
        return cached[:limit]
    if _backoff_active("football-data"):
        return []
    try:
        response = _http_get(
            f"https://api.football-data.org/v4/teams/{team_id}/matches",
            headers={"X-Auth-Token": token},
            params={"status": "FINISHED", "limit": str(limit)},
            timeout=12,
        )
        if response.status_code == 429:
            _note_backoff("football-data")
            return []
        if response.status_code != 200:
            return []
        rows = []
        for match in response.json().get("matches", []):
            utc_date = match.get("utcDate") or ""
            date = utc_date.split("T", 1)[0] if "T" in utc_date else utc_date
            comp = match.get("competition", {})
            comp_name = comp.get("name") or ""
            comp_code = comp.get("code") or ""
            comp_type = comp.get("type") or ""
            home = match.get("homeTeam", {}).get("name")
            away = match.get("awayTeam", {}).get("name")
            score = match.get("score", {}).get("fullTime", {})
            home_score = score.get("home")
            away_score = score.get("away")
            if home is None or away is None or home_score is None or away_score is None:
                continue
            if _is_friendly_comp(comp_name, str(comp_code), comp_type):
                continue
            is_home = team_id == match.get("homeTeam", {}).get("id")
            gf = home_score if is_home else away_score
            ga = away_score if is_home else home_score
            if gf > ga:
                result = "W"
            elif gf == ga:
                result = "D"
            else:
                result = "L"
            opponent = away if is_home else home
            rows.append(
                {
                    "date": date,
                    "opponent": opponent,
                    "score": f"{gf}-{ga}",
                    "home": is_home,
                    "gf": gf,
                    "ga": ga,
                    "result": result,
                    "competition": comp_name or comp_code,
                    "competition_type": str(comp_type),
                }
            )
        _cache_set(_TEAM_RECENT_CACHE, cache_key, rows)
        return rows[:limit]
    except Exception:
        return []


def _team_recent_matches_api_football(api_key: str, team_id: int | None, limit: int = 5) -> list[dict]:
    if not api_key or not team_id:
        return []
    if _backoff_active("api-football"):
        return []
    cache_key = f"af:{team_id}"
    cached = _cache_get(_TEAM_RECENT_CACHE, cache_key, TEAM_SUMMARY_TTL_SECONDS)
    if isinstance(cached, list):
        return cached[:limit]
    try:
        season = _season_for_date(datetime.now(timezone.utc).date().isoformat())
        response = _http_get(
            "https://v3.football.api-sports.io/fixtures",
            headers={"x-apisports-key": api_key},
            params={
                "team": str(team_id),
                "season": str(season),
                "status": "FT",
                "last": str(limit),
                "timezone": "UTC",
            },
            timeout=12,
        )
        if response.status_code != 200:
            if response.status_code == 429:
                _note_backoff("api-football")
            return []
        data = response.json()
        errors = data.get("errors") or {}
        items = data.get("response", [])
        if errors.get("plan") and not items:
            # Free plan workaround: use a date range and pick latest results.
            to_date = datetime.now(timezone.utc).date()
            from_date = (to_date - timedelta(days=180)).isoformat()
            season = _season_for_date(to_date.isoformat())
            fallback_season = min(season, max(2022, to_date.year - 2))
            season = fallback_season
            response = _http_get(
                "https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": api_key},
                params={
                    "team": str(team_id),
                    "season": str(season),
                    "status": "FT",
                    "from": from_date,
                    "to": to_date.isoformat(),
                    "timezone": "UTC",
                },
                timeout=12,
            )
            if response.status_code != 200:
                if response.status_code == 429:
                    _note_backoff("api-football")
                return []
            data = response.json()
            if data.get("errors", {}).get("requests"):
                _note_backoff("api-football")
                return []
            items = data.get("response", [])
        rows = []
        for item in items:
            fixture = item.get("fixture", {})
            teams = item.get("teams", {})
            league = item.get("league", {})
            goals = item.get("goals", {})
            utc_date = fixture.get("date") or ""
            date = utc_date.split("T", 1)[0] if "T" in utc_date else utc_date
            comp_name = str(league.get("name") or "")
            comp_type = str(league.get("type") or "")
            comp_id = league.get("id") or ""
            if _is_friendly_comp(comp_name, str(comp_id), comp_type):
                continue
            home = (teams.get("home") or {}).get("name")
            away = (teams.get("away") or {}).get("name")
            home_id = (teams.get("home") or {}).get("id")
            away_id = (teams.get("away") or {}).get("id")
            home_score = goals.get("home")
            away_score = goals.get("away")
            if home is None or away is None or home_score is None or away_score is None:
                continue
            is_home = team_id == home_id
            gf = home_score if is_home else away_score
            ga = away_score if is_home else home_score
            if gf > ga:
                result = "W"
            elif gf == ga:
                result = "D"
            else:
                result = "L"
            opponent = away if is_home else home
            rows.append(
                {
                    "date": date,
                    "opponent": opponent,
                    "score": f"{gf}-{ga}",
                    "home": is_home,
                    "gf": gf,
                    "ga": ga,
                    "result": result,
                    "competition": comp_name,
                    "competition_type": comp_type,
                    "fixture_id": fixture.get("id"),
                    "team_id": team_id,
                }
            )
        rows.sort(key=lambda r: r.get("date", ""), reverse=True)
        _cache_set(_TEAM_RECENT_CACHE, cache_key, rows)
        return rows[:limit]
    except Exception:
        return []


def _stat_value(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace("%", "")
        try:
            return float(cleaned)
        except Exception:
            return None
    return None


def _safe_rate(value: object | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _team_squad_tokens(team_id: int | None) -> set[str]:
    if not team_id or not config.football_data_token:
        return set()
    _load_team_squad_cache()
    key = str(team_id)
    cached = _TEAM_SQUAD_CACHE.get(key)
    if isinstance(cached, dict):
        ts = float(cached.get("ts", 0.0))
        if time.time() - ts < TEAM_SQUAD_TTL_SECONDS:
            tokens = cached.get("tokens") or []
            return {str(tok) for tok in tokens if tok}
    if _backoff_active("football-data"):
        return set()
    if not _squad_fetch_allowed():
        return set()
    try:
        response = _http_get(
            f"https://api.football-data.org/v4/teams/{team_id}",
            headers={"X-Auth-Token": config.football_data_token},
            timeout=12,
        )
        if response.status_code == 429:
            _note_backoff("football-data")
            return set()
        if response.status_code != 200:
            return set()
        data = response.json()
        squad = data.get("squad", []) if isinstance(data, dict) else []
        tokens: set[str] = set()
        for player in squad:
            name = player.get("name") or player.get("shortName")
            if name:
                words = [w.lower() for w in re.split(r"\W+", str(name)) if len(w) > 2]
                tokens.update(words)
        _TEAM_SQUAD_CACHE[key] = {"ts": time.time(), "tokens": sorted(tokens)}
        _note_squad_fetch()
        _save_team_squad_cache()
        return tokens
    except Exception:
        return set()


def _fixture_stats_api_football(api_key: str, fixture_id: int | None) -> dict[int, dict[str, float]]:
    if not api_key or not fixture_id or not AF_STATS_ENABLED:
        return {}
    cache_key = f"fixture:{fixture_id}"
    cached = _cache_get(_FIXTURE_STATS_CACHE, cache_key, AF_STATS_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    try:
        response = _http_get(
            "https://v3.football.api-sports.io/fixtures/statistics",
            headers={"x-apisports-key": api_key},
            params={"fixture": str(fixture_id)},
            timeout=12,
        )
        if response.status_code != 200:
            return {}
        data = response.json()
        items = data.get("response", [])
        team_stats: dict[int, dict[str, float]] = {}
        for entry in items:
            team = entry.get("team", {})
            team_id = team.get("id")
            if not isinstance(team_id, int):
                continue
            stats = entry.get("statistics", [])
            corners = None
            cards = 0.0
            cards_found = False
            for stat in stats:
                stat_type = str(stat.get("type") or "")
                val = _stat_value(stat.get("value"))
                if not stat_type:
                    continue
                if stat_type.lower() in ("corner kicks", "corners"):
                    corners = val
                if stat_type.lower() in ("yellow cards", "red cards", "cards"):
                    if val is not None:
                        cards += val
                        cards_found = True
            team_stats[team_id] = {
                "corners": corners,
                "cards": cards if cards_found else None,
            }
        _cache_set(_FIXTURE_STATS_CACHE, cache_key, team_stats)
        return team_stats
    except Exception:
        return {}


def _fdco_fetch_csv(league_code: str, season_code: str) -> list[dict]:
    cache_key = f"{league_code}:{season_code}"
    cached = _cache_get(_FD_RECENT_CACHE, cache_key, FDCO_UK_TTL_SECONDS)
    if isinstance(cached, list):
        return cached
    url = f"https://www.football-data.co.uk/mmz4281/{season_code}/{league_code}.csv"
    try:
        response = _http_get(url, timeout=12)
        if response.status_code != 200:
            return []
        text = response.text
        import csv
        reader = csv.DictReader(text.splitlines())
        rows = [row for row in reader]
        _cache_set(_FD_RECENT_CACHE, cache_key, rows)
        return rows
    except Exception:
        return []


def _fdco_parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d/%m/%y").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _stat_value_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _fdco_table_data(league_code: str, season_code: str) -> dict[str, dict[str, object]]:
    cache_key = f"table:{league_code}:{season_code}"
    cached = _cache_get(_FDCO_TABLE_CACHE, cache_key, FDCO_UK_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    rows = _fdco_fetch_csv(league_code, season_code)
    table: dict[str, dict[str, object]] = {}
    for row in rows:
        team = row.get("HomeTeam") or row.get("AwayTeam") or row.get("Team") or ""
        if not team:
            continue
        norm = _normalize_team_name(team)
        if not norm:
            continue
        entry: dict[str, object] = {"team": team}
        for key in ("position", "points", "played", "won", "draw", "lost", "goals_for", "goals_against"):
            columns = FDCO_UK_COMP_COLUMNS.get(key, ())
            value = None
            for col in columns:
                if col in row:
                    value = _stat_value_int(row.get(col))
                    if value is not None:
                        break
            if value is not None:
                entry_key = key
                entry[entry_key] = value
        table[norm] = entry
    # If the CSV doesn't include table columns, derive standings from results.
    if table and not any(entry.get("points") is not None or entry.get("position") is not None for entry in table.values()):
        standings: dict[str, dict[str, object]] = {}
        for row in rows:
            home = str(row.get("HomeTeam") or "")
            away = str(row.get("AwayTeam") or "")
            if not home or not away:
                continue
            try:
                home_goals = int(row.get("FTHG"))
                away_goals = int(row.get("FTAG"))
            except Exception:
                continue
            for team_name, gf, ga, is_home in (
                (home, home_goals, away_goals, True),
                (away, away_goals, home_goals, False),
            ):
                norm = _normalize_team_name(team_name)
                entry = standings.setdefault(
                    norm,
                    {
                        "team": team_name,
                        "played": 0,
                        "points": 0,
                        "won": 0,
                        "draw": 0,
                        "lost": 0,
                        "goals_for": 0,
                        "goals_against": 0,
                    },
                )
                entry["played"] = int(entry.get("played") or 0) + 1
                entry["goals_for"] = int(entry.get("goals_for") or 0) + int(gf)
                entry["goals_against"] = int(entry.get("goals_against") or 0) + int(ga)
                if gf > ga:
                    entry["won"] = int(entry.get("won") or 0) + 1
                    entry["points"] = int(entry.get("points") or 0) + 3
                elif gf == ga:
                    entry["draw"] = int(entry.get("draw") or 0) + 1
                    entry["points"] = int(entry.get("points") or 0) + 1
                else:
                    entry["lost"] = int(entry.get("lost") or 0) + 1
        if standings:
            ranked = sorted(
                standings.values(),
                key=lambda e: (
                    -(int(e.get("points") or 0)),
                    -((int(e.get("goals_for") or 0) - int(e.get("goals_against") or 0))),
                    -(int(e.get("goals_for") or 0)),
                    str(e.get("team") or ""),
                ),
            )
            for idx, entry in enumerate(ranked, start=1):
                entry["position"] = idx
            table = { _normalize_team_name(entry.get("team") or ""): entry for entry in ranked if entry.get("team") }
    _cache_set(_FDCO_TABLE_CACHE, cache_key, table)
    return table



def _fdco_team_summary(team_name: str, league_code: str, season_year: int, limit: int = 5) -> tuple[list[dict], float | None, float | None, dict[str, object] | None]:
    season_code = _fdco_season_code(season_year)
    rows = _fdco_fetch_csv(league_code, season_code)
    if not rows:
        return [], None, None, None
    team_norm = _normalize_team_name(team_name)
    matches = []
    for row in rows:
        home = str(row.get("HomeTeam") or "")
        away = str(row.get("AwayTeam") or "")
        if not home or not away:
            continue
        home_norm = _normalize_team_name(home)
        away_norm = _normalize_team_name(away)
        if not _team_norm_match(team_norm, home_norm) and not _team_norm_match(team_norm, away_norm):
            continue
        date_raw = str(row.get("Date") or "")
        date_dt = _fdco_parse_date(date_raw)
        date = date_dt.date().isoformat() if date_dt else ""
        home_goals = row.get("FTHG")
        away_goals = row.get("FTAG")
        try:
            home_goals = int(home_goals)
            away_goals = int(away_goals)
        except Exception:
            continue
        is_home = _team_norm_match(team_norm, home_norm)
        gf = home_goals if is_home else away_goals
        ga = away_goals if is_home else home_goals
        if gf > ga:
            result = "W"
        elif gf == ga:
            result = "D"
        else:
            result = "L"
        opponent = away if is_home else home
        corners = None
        cards = None
        try:
            corners = int(row.get("HC") if is_home else row.get("AC"))
        except Exception:
            corners = None
        try:
            yellow = int(row.get("HY") if is_home else row.get("AY"))
            red = int(row.get("HR") if is_home else row.get("AR"))
            cards = yellow + red
        except Exception:
            cards = None
        matches.append(
            {
                "date": date,
                "opponent": opponent,
                "score": f"{gf}-{ga}",
                "home": is_home,
                "gf": gf,
                "ga": ga,
                "result": result,
                "competition": FDCO_UK_COMP_NAMES.get(league_code, league_code),
                "competition_type": "League",
                "corners": corners,
                "cards": cards,
            }
        )
    matches = [m for m in matches if m.get("date")]
    matches.sort(key=lambda m: m.get("date", ""), reverse=True)
    matches = matches[:limit]
    corners_values = [m["corners"] for m in matches if m.get("corners") is not None]
    cards_values = [m["cards"] for m in matches if m.get("cards") is not None]
    corners_avg = sum(corners_values) / len(corners_values) if corners_values else None
    cards_avg = sum(cards_values) / len(cards_values) if cards_values else None
    table = _fdco_table_data(league_code, season_code)
    table_entry = table.get(team_norm)
    if table_entry is None:
        for key, entry in table.items():
            if _team_norm_match(team_norm, key):
                table_entry = entry
                break
    return matches, corners_avg, cards_avg, table_entry


def _fdco_team_season_stats(
    team_name: str,
    league_code: str,
    season_year: int,
    list_limit: int = 5,
) -> tuple[list[dict], dict[str, float] | None, float | None, float | None, dict[str, object] | None]:
    season_code = _fdco_season_code(season_year)
    rows = _fdco_fetch_csv(league_code, season_code)
    if not rows:
        return [], None, None, None, None
    team_norm = _normalize_team_name(team_name)
    matches = []
    wins = 0
    goals_for = 0
    btts_hits = 0
    over25_hits = 0
    total = 0
    corners_values = []
    cards_values = []
    for row in rows:
        home = str(row.get("HomeTeam") or "")
        away = str(row.get("AwayTeam") or "")
        if not home or not away:
            continue
        home_norm = _normalize_team_name(home)
        away_norm = _normalize_team_name(away)
        if not _team_norm_match(team_norm, home_norm) and not _team_norm_match(team_norm, away_norm):
            continue
        date_raw = str(row.get("Date") or "")
        date_dt = _fdco_parse_date(date_raw)
        date = date_dt.date().isoformat() if date_dt else ""
        home_goals = row.get("FTHG")
        away_goals = row.get("FTAG")
        try:
            home_goals = int(home_goals)
            away_goals = int(away_goals)
        except Exception:
            continue
        is_home = _team_norm_match(team_norm, home_norm)
        gf = home_goals if is_home else away_goals
        ga = away_goals if is_home else home_goals
        if gf > ga:
            result = "W"
            wins += 1
        elif gf == ga:
            result = "D"
        else:
            result = "L"
        total += 1
        goals_for += gf
        if gf > 0 and ga > 0:
            btts_hits += 1
        if (gf + ga) >= 3:
            over25_hits += 1
        opponent = away if is_home else home
        corners = None
        cards = None
        try:
            corners = int(row.get("HC") if is_home else row.get("AC"))
        except Exception:
            corners = None
        try:
            yellow = int(row.get("HY") if is_home else row.get("AY"))
            red = int(row.get("HR") if is_home else row.get("AR"))
            cards = yellow + red
        except Exception:
            cards = None
        if corners is not None:
            corners_values.append(corners)
        if cards is not None:
            cards_values.append(cards)
        matches.append(
            {
                "date": date,
                "opponent": opponent,
                "score": f"{gf}-{ga}",
                "home": is_home,
                "gf": gf,
                "ga": ga,
                "result": result,
                "competition": FDCO_UK_COMP_NAMES.get(league_code, league_code),
                "competition_type": "League",
                "corners": corners,
                "cards": cards,
            }
        )
    matches = [m for m in matches if m.get("date")]
    matches.sort(key=lambda m: m.get("date", ""), reverse=True)
    list_matches = matches[:list_limit] if list_limit > 0 else matches
    corners_avg = sum(corners_values) / len(corners_values) if corners_values else None
    cards_avg = sum(cards_values) / len(cards_values) if cards_values else None
    table = _fdco_table_data(league_code, season_code)
    table_entry = table.get(team_norm)
    if table_entry is None:
        for key, entry in table.items():
            if _team_norm_match(team_norm, key):
                table_entry = entry
                break
    if total <= 0:
        return list_matches, None, corners_avg, cards_avg, table_entry
    stats = {
        "win_rate": wins / total,
        "goals_for_avg": goals_for / total,
        "btts_rate": btts_hits / total,
        "over25_rate": over25_hits / total,
        "total": float(total),
    }
    return list_matches, stats, corners_avg, cards_avg, table_entry


def _table_scores_from_standings(standings: list[dict]) -> dict[str, float]:
    if not standings:
        return {}
    max_points = max(row.get("points", 0) for row in standings) or 1
    return {row.get("team", ""): (row.get("points", 0) / max_points) for row in standings}


def _market_key_from_pick(market: str) -> str:
    lowered = str(market or "").lower()
    if "over/under" in lowered or "over" in lowered or "under" in lowered:
        return "totals"
    if "btts" in lowered or "gg" in lowered:
        return "btts"
    if "dupla" in lowered or "double chance" in lowered:
        return "double_chance"
    return "h2h"


def _build_odds_markets_from_match(match: dict) -> dict[str, dict]:
    markets: dict[str, dict] = {}
    for market_key in ("h2h", "totals", "btts", "double_chance"):
        outcomes = _average_market_outcomes(match, market_key)
        if not outcomes:
            continue
        if market_key == "h2h":
            parsed: dict[str, float] = {}
            home = match.get("home_team", "")
            away = match.get("away_team", "")
            for outcome in outcomes:
                name = str(outcome.get("name") or "").lower()
                price = outcome.get("price")
                if not isinstance(price, (int, float)):
                    continue
                if name == "draw":
                    parsed["draw"] = float(price)
                elif name == str(home).lower():
                    parsed["home"] = float(price)
                elif name == str(away).lower():
                    parsed["away"] = float(price)
            if parsed:
                markets["1x2"] = parsed
        elif market_key == "totals":
            parsed = {}
            for outcome in outcomes:
                name = str(outcome.get("name") or "").lower()
                point = outcome.get("point")
                price = outcome.get("price")
                if point != 2.5 or not isinstance(price, (int, float)):
                    continue
                if name == "over":
                    parsed["over_2.5"] = float(price)
                elif name == "under":
                    parsed["under_2.5"] = float(price)
            if parsed:
                markets["over_under"] = parsed
        elif market_key == "btts":
            parsed = {}
            for outcome in outcomes:
                name = str(outcome.get("name") or "").lower()
                price = outcome.get("price")
                if not isinstance(price, (int, float)):
                    continue
                if name == "yes":
                    parsed["yes"] = float(price)
                elif name == "no":
                    parsed["no"] = float(price)
            if parsed:
                markets["btts"] = parsed
        elif market_key == "double_chance":
            parsed = {}
            for outcome in outcomes:
                name = str(outcome.get("name") or "").lower().replace(" ", "")
                price = outcome.get("price")
                if not isinstance(price, (int, float)):
                    continue
                if name in {"1x", "homedraw"}:
                    parsed["1x"] = float(price)
                elif name in {"x2", "drawaway"}:
                    parsed["x2"] = float(price)
                elif name in {"12", "homeaway"}:
                    parsed["12"] = float(price)
            if parsed:
                markets["double_chance"] = parsed
    return markets


def _odds_for_pick(market_key: str, outcome: str, markets: dict[str, dict]) -> float | None:
    if not markets:
        return None
    if market_key == "h2h":
        odds = markets.get("1x2", {})
        if outcome == "Hazai gyozelem":
            return odds.get("home")
        if outcome == "Vendeg gyozelem":
            return odds.get("away")
        if outcome == "Döntetlen":
            return odds.get("draw")
    if market_key == "totals":
        odds = markets.get("over_under", {})
        if outcome.startswith("Over"):
            return odds.get("over_2.5")
        if outcome.startswith("Under"):
            return odds.get("under_2.5")
    if market_key == "btts":
        odds = markets.get("btts", {})
        if "Igen" in outcome:
            return odds.get("yes")
        if "Nem" in outcome:
            return odds.get("no")
    if market_key == "double_chance":
        odds = markets.get("double_chance", {})
        key = outcome.replace(" ", "").lower()
        return odds.get(key)
    return None


def _stat_pick_for_match(
    match: dict,
    comp_standings: list[dict],
    table_scores: dict[str, float],
    news_items: list[dict],
    weights: dict[str, float],
    roi_map: dict[str, float],
) -> dict | None:
    home_team = match.get("home_team", "")
    away_team = match.get("away_team", "")
    if not home_team or not away_team:
        return None
    standings_index = {}
    for row in comp_standings:
        team = row.get("team")
        if team:
            standings_index[_normalize_team_name(team)] = row
    home_form = _team_form_stats(home_team, table_scores)
    away_form = _team_form_stats(away_team, table_scores)
    played_est = 10
    def _ensure_form_row(team_name: str, form: dict[str, float]) -> None:
        norm = _normalize_team_name(team_name)
        row = standings_index.get(norm, {})
        if not isinstance(row, dict):
            row = {}
        if not row.get("played"):
            row["played"] = played_est
        if not row.get("points"):
            ppg_norm = float(form.get("ppg_norm", table_scores.get(team_name, 0.5)) or 0.5)
            row["points"] = int(max(0, round(ppg_norm * row["played"] * 3)))
        if not row.get("goals_for"):
            row["goals_for"] = int(max(0, round(float(form.get("gf_avg", 1.2) or 1.2) * row["played"])))
        if not row.get("goals_against"):
            row["goals_against"] = int(max(0, round(float(form.get("ga_avg", 1.2) or 1.2) * row["played"])))
        row.setdefault("team", team_name)
        standings_index[norm] = row
    _ensure_form_row(home_team, home_form)
    _ensure_form_row(away_team, away_form)
    home_summary = _summary_dict(home_team, match.get("home_id"))
    away_summary = _summary_dict(away_team, match.get("away_id"))
    # Use any table entries discovered during summary fetch to enrich standings.
    for team_name, summary in ((home_team, home_summary), (away_team, away_summary)):
        table_entry = summary.get("table_entry") if isinstance(summary, dict) else None
        if not isinstance(table_entry, dict):
            continue
        norm = _normalize_team_name(str(table_entry.get("team") or team_name))
        if norm and norm not in standings_index:
            standings_index[norm] = {
                "team": table_entry.get("team") or team_name,
                "position": table_entry.get("position"),
                "points": table_entry.get("points"),
                "played": table_entry.get("played"),
                "won": table_entry.get("won"),
                "draw": table_entry.get("draw"),
                "lost": table_entry.get("lost"),
                "goals_for": table_entry.get("goals_for"),
                "goals_against": table_entry.get("goals_against"),
            }
    def _rates_from_matches(summary: dict, team_name: str) -> dict[str, float]:
        matches = summary.get("matches") if isinstance(summary, dict) else []
        if not isinstance(matches, list) or not matches:
            return {}
        team_norm = _normalize_team_name(team_name)
        over_hits = 0
        btts_hits = 0
        gf_sum = 0.0
        ga_sum = 0.0
        total = 0
        for row in matches[:8]:
            try:
                gf = float(row.get("gf", 0))
                ga = float(row.get("ga", 0))
            except Exception:
                continue
            total_goals = gf + ga
            if total_goals >= 3:
                over_hits += 1
            if gf > 0 and ga > 0:
                btts_hits += 1
            gf_sum += gf
            ga_sum += ga
            total += 1
        if total <= 0:
            return {}
        return {
            "over25_rate": over_hits / total,
            "btts_rate": btts_hits / total,
            "gf_avg": gf_sum / total,
            "ga_avg": ga_sum / total,
        }
    home_recent = _rates_from_matches(home_summary, home_team)
    away_recent = _rates_from_matches(away_summary, away_team)
    stats_payload = {
        "home_corners": home_summary.get("corners_avg"),
        "away_corners": away_summary.get("corners_avg"),
        "home_cards": home_summary.get("cards_avg"),
        "away_cards": away_summary.get("cards_avg"),
        # Richer stat hints so the scorer can use more than a single equation.
        "home_over25_rate": home_recent.get("over25_rate") or home_summary.get("over25_rate") or home_form.get("over25_rate"),
        "away_over25_rate": away_recent.get("over25_rate") or away_summary.get("over25_rate") or away_form.get("over25_rate"),
        "home_btts_rate": home_recent.get("btts_rate") or home_summary.get("btts_rate") or home_form.get("btts_rate"),
        "away_btts_rate": away_recent.get("btts_rate") or away_summary.get("btts_rate") or away_form.get("btts_rate"),
        "home_gf_avg": home_recent.get("gf_avg") or home_summary.get("goals_for_avg") or home_form.get("gf_avg"),
        "away_gf_avg": away_recent.get("gf_avg") or away_summary.get("goals_for_avg") or away_form.get("gf_avg"),
        "home_ga_avg": home_recent.get("ga_avg") or home_form.get("ga_avg"),
        "away_ga_avg": away_recent.get("ga_avg") or away_form.get("ga_avg"),
    }
    has_stats = any(isinstance(stats_payload.get(k), (int, float)) for k in stats_payload)
    picks = score_fixture(
        fixture_id=str(match.get("id") or ""),
        home_team=home_team,
        away_team=away_team,
        standings=standings_index,
        odds=None,
        stats=stats_payload if has_stats else None,
        events=None,
    )
    # Stat-only mode can now recommend all supported markets.
    # Bias the stat-only market choice toward what the data says about goals.
    over_rate_vals = [
        val
        for val in (home_summary.get("over25_rate"), away_summary.get("over25_rate"))
        if isinstance(val, (int, float))
    ]
    if not over_rate_vals:
        over_rate_vals = [
            val
            for val in (home_form.get("over25_rate"), away_form.get("over25_rate"))
            if isinstance(val, (int, float))
        ]
    btts_rate_vals = [
        val
        for val in (home_summary.get("btts_rate"), away_summary.get("btts_rate"))
        if isinstance(val, (int, float))
    ]
    if not btts_rate_vals:
        btts_rate_vals = [
            val
            for val in (home_form.get("btts_rate"), away_form.get("btts_rate"))
            if isinstance(val, (int, float))
        ]
    over_rate = (sum(over_rate_vals) / len(over_rate_vals)) if over_rate_vals else None
    btts_rate = (sum(btts_rate_vals) / len(btts_rate_vals)) if btts_rate_vals else None
    adjusted: list[tuple[float, object]] = []
    for pick_item in picks:
        score = pick_item.score
        market_lower = pick_item.market.lower()
        outcome_lower = pick_item.outcome.lower()
        if isinstance(over_rate, (int, float)) and "over/under" in market_lower:
            if over_rate >= 0.6 and outcome_lower.startswith("over"):
                score += 0.06
            elif over_rate <= 0.45 and outcome_lower.startswith("under"):
                score += 0.06
            else:
                # Penalize totals when the over-rate signal is ambiguous.
                score -= 0.08
        if isinstance(btts_rate, (int, float)) and market_lower == "btts":
            if btts_rate >= 0.6 and "igen" in outcome_lower:
                score += 0.05
            elif btts_rate <= 0.45 and "nem" in outcome_lower:
                score += 0.05
        adjusted.append((score, pick_item))
    best = max(adjusted, key=lambda item: item[0])[1] if adjusted else max(picks, key=lambda p: p.score)
    market_key = _market_key_from_pick(best.market)
    pick = {
        "match_key": _match_key(match),
        "home_team": home_team,
        "away_team": away_team,
        "competition": _match_competition(match),
        "comp_code": match.get("comp_code"),
        "fd_code": match.get("fd_code"),
        "home_id": match.get("home_id"),
        "away_id": match.get("away_id"),
        "season": match.get("season"),
        "sr_season_id": match.get("sr_season_id"),
        "sport_key": match.get("sport_key", ""),
        "commence_time": match.get("commence_time", ""),
        "market_key": market_key,
        "market_label": _market_label(market_key),
        "outcome": best.outcome,
        "line": None,
        "odds": None,
        "distance": 0.0,
        "score": best.score,
        "risk": _risk_label(best.score),
        "explain_hu": best.explanation_hu,
        "model_prob": best.model_prob,
        "implied_prob": best.implied_prob,
        "ev": best.ev,
        "value": best.value,
    }
    pick["risk_flags"] = _pick_risk_flags(match, pick)
    if pick["risk_flags"]:
        total = max(0.0, best.score - 0.05 * len(pick["risk_flags"]))
        pick["score"] = total
        pick["risk"] = _risk_label(total)
    return pick


def _build_stat_only_picks(
    fixtures: list[dict],
    standings_by_comp: dict[str, list[dict]],
    news_items: list[dict],
    roi_map: dict[str, float],
    window_hours: int = 24,
) -> list[dict]:
    now = datetime.now(BUDAPEST_TZ)
    filtered: list[dict] = []
    for match in fixtures:
        match_dt = _parse_match_dt(match)
        if not match_dt:
            continue
        match_local = match_dt.astimezone(BUDAPEST_TZ)
        if match_local >= now and match_local < (now + timedelta(hours=window_hours)):
            filtered.append(match)
    if not filtered:
        return []
    fixtures = filtered
    weights = _load_weights()
    picks: list[dict] = []
    for match in fixtures:
        comp_code = match.get("comp_code") or match.get("fd_code") or match.get("sport_key", "")
        if comp_code:
            mapped = _comp_name_to_fd_code(str(comp_code))
            if mapped:
                comp_code = mapped
                match["comp_code"] = mapped
                match["fd_code"] = mapped
        if not comp_code:
            comp_name = str(match.get("comp_name") or match.get("competition") or "")
            comp_code = _comp_name_to_fd_code(comp_name) or ""
            if comp_code:
                match["comp_code"] = comp_code
                match["fd_code"] = comp_code
        comp_standings = standings_by_comp.get(comp_code, [])
        table_scores = _table_scores_from_standings(comp_standings)
        if comp_code:
            _TEAM_COMP_HINT[_normalize_team_name(match.get("home_team", ""))] = str(comp_code)
            _TEAM_COMP_HINT[_normalize_team_name(match.get("away_team", ""))] = str(comp_code)
        sr_season_id = match.get("sr_season_id")
        if sr_season_id:
            _TEAM_SEASON_HINT[_normalize_team_name(match.get("home_team", ""))] = str(sr_season_id)
            _TEAM_SEASON_HINT[_normalize_team_name(match.get("away_team", ""))] = str(sr_season_id)
        pick = _stat_pick_for_match(match, comp_standings, table_scores, news_items, weights, roi_map)
        if pick:
            picks.append(pick)
    picks.sort(key=lambda item: item["score"], reverse=True)
    limit = int(os.environ.get("STAT_ONLY_PICK_LIMIT", "2"))
    if limit > 0 and len(picks) < limit:
        existing_keys = {item.get("match_key") for item in picks}
        for match in fixtures:
            key = _match_key(match)
            if key in existing_keys:
                continue
            comp_code = match.get("comp_code") or match.get("fd_code") or match.get("sport_key", "")
            if comp_code:
                mapped = _comp_name_to_fd_code(str(comp_code))
                if mapped:
                    comp_code = mapped
                    match["comp_code"] = mapped
                    match["fd_code"] = mapped
            if not comp_code:
                comp_name = str(match.get("comp_name") or match.get("competition") or "")
                comp_code = _comp_name_to_fd_code(comp_name) or ""
                if comp_code:
                    match["comp_code"] = comp_code
                    match["fd_code"] = comp_code
            comp_standings = standings_by_comp.get(comp_code, [])
            table_scores = _table_scores_from_standings(comp_standings)
            fallback_pick = _stat_pick_for_match(match, comp_standings, table_scores, news_items, weights, roi_map)
            if not fallback_pick:
                continue
            picks.append(fallback_pick)
            existing_keys.add(key)
            if len(picks) >= limit:
                break
    if limit > 0:
        picks = picks[:limit]
    return picks


def _dedupe_fixtures(fixtures: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict] = []
    for item in fixtures:
        key = (
            str(item.get("commence_time") or ""),
            str(item.get("home_team") or ""),
            str(item.get("away_team") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _fetch_public_fixtures(
    competitions: list[dict],
    window_hours: int,
) -> tuple[list[dict], dict[str, list[dict]]]:
    standings_by_comp: dict[str, list[dict]] = {}
    fixtures: list[dict] = []
    allowed_codes: set[str] = set()
    allowed_pairs: set[tuple[str, str]] = set()
    max_comp = int(os.environ.get("FD_PUBLIC_COMP_LIMIT", "8"))
    public_comps = competitions[:max_comp]
    fd_map = _fd_competition_map(public_comps)
    for comp in public_comps:
        code = str(comp.get("code") or "")
        name = str(comp.get("name") or "")
        area = str(comp.get("area", {}).get("name") or "")
        if code:
            allowed_codes.add(code)
        if name and area:
            allowed_pairs.add((_normalize_comp(name), _normalize_comp(area)))
    fixtures.extend(_fetch_upcoming_fixtures_fd_all(config.football_data_token, window_hours))
    fixtures.extend(_fetch_upcoming_fixtures_api_football(config.api_football_key, window_hours))
    fixtures.extend(_fetch_upcoming_fixtures_sportradar(config.sportradar_api_key, window_hours, SR_MAX_FIXTURES))
    fixtures = _dedupe_fixtures(fixtures)
    raw_fixtures = fixtures[:]
    if SR_ENABLE_PROB and config.sportradar_api_key:
        has_sr = any(str(item.get("id") or "").startswith("sr:sport_event:") for item in fixtures)
        if has_sr:
            prob_map = _fetch_upcoming_probabilities_sportradar()
            if prob_map:
                for item in fixtures:
                    event_id = item.get("id")
                    if event_id and str(event_id) in prob_map:
                        item["sr_probabilities"] = prob_map[str(event_id)]
    now = datetime.now(BUDAPEST_TZ)
    fixtures = [
        item
        for item in fixtures
        if (dt := _parse_match_dt(item))
        and (local := dt.astimezone(BUDAPEST_TZ)) >= now
        and local < (now + timedelta(hours=window_hours))
    ]
    if allowed_codes or allowed_pairs:
        filtered = []
        for item in fixtures:
            comp_code = str(item.get("comp_code") or "")
            comp_name = str(item.get("comp_name") or "")
            comp_country = str(item.get("comp_country") or "")
            fd_code = ""
            if comp_code and comp_code in allowed_codes:
                filtered.append(item)
                continue
            if comp_name and comp_country:
                key = (_normalize_comp(comp_name), _normalize_comp(comp_country))
                fd_code = fd_map.get(key, "")
                if fd_code:
                    item["fd_code"] = fd_code
                    filtered.append(item)
                    continue
            if comp_name and not _is_youth_comp(comp_name) and _allowed_comp_match(comp_name, comp_country):
                filtered.append(item)
        if not filtered:
            filtered = []
            for item in fixtures:
                comp_name = str(item.get("comp_name") or item.get("competition") or "")
                comp_country = str(item.get("comp_country") or "")
                if not comp_name or _is_youth_comp(comp_name):
                    continue
                if _allowed_comp_match(comp_name, comp_country):
                    filtered.append(item)
        fixtures = filtered
    else:
        filtered = []
        for item in fixtures:
            comp_name = str(item.get("comp_name") or item.get("competition") or "")
            comp_country = str(item.get("comp_country") or "")
            if not comp_name or _is_youth_comp(comp_name):
                continue
            if _allowed_comp_match(comp_name, comp_country):
                filtered.append(item)
        fixtures = filtered
    if not fixtures:
        fixtures = raw_fixtures
    if allowed_codes:
        comp_codes: list[str] = []
        for item in fixtures:
            code = str(item.get("fd_code") or item.get("comp_code") or "")
            if code and code in allowed_codes and code not in comp_codes:
                comp_codes.append(code)
        standings_limit = int(os.environ.get("FD_STANDINGS_LIMIT", "6"))
        for code in comp_codes[:standings_limit]:
            standings_by_comp[code] = _fetch_standings_fd(config.football_data_token, code)
    sr_season_ids: dict[str, str] = {}
    for item in fixtures:
        comp_code = str(item.get("comp_code") or "")
        season_id = str(item.get("sr_season_id") or "")
        if comp_code and season_id and comp_code not in sr_season_ids:
            sr_season_ids[comp_code] = season_id
    sr_limit = int(os.environ.get("SR_STANDINGS_LIMIT", "4"))
    for comp_code, season_id in list(sr_season_ids.items())[:sr_limit]:
        standings_by_comp[comp_code] = _fetch_standings_sportradar(season_id)
    # Persist league standings so we can render without burning API quota.
    try:
        cache = DiskCache(_cache_dir())
        cache_day = datetime.now(BUDAPEST_TZ).strftime("%Y-%m-%d")
        for code, rows in standings_by_comp.items():
            if rows:
                cache.set(cache_day, f"standings_{code}", rows)
    except Exception:
        pass
    if STAT_ONLY_MAX_FIXTURES > 0 and len(fixtures) > STAT_ONLY_MAX_FIXTURES:
        fixtures = fixtures[:STAT_ONLY_MAX_FIXTURES]
    offline = _load_offline_fixtures()
    if offline:
        existing_keys = { _match_key(item) for item in fixtures }
        for match in offline:
            key = _match_key(match)
            if key in existing_keys:
                continue
            fixtures.append(match)
            existing_keys.add(key)
    return fixtures, standings_by_comp


def _fetch_standings_fd(token: str, comp_code: str, limit: int = 10) -> list[dict]:
    if not token or not comp_code:
        return []
    cache_key = f"{comp_code}:{limit}"
    cached = _cache_get(_FD_STANDINGS_CACHE, cache_key, FD_STANDINGS_TTL_SECONDS)
    if isinstance(cached, list):
        return cached
    # Try disk cache first to avoid remote calls when quota is tight.
    try:
        cache = DiskCache(_cache_dir())
        cache_day = datetime.now(BUDAPEST_TZ).strftime("%Y-%m-%d")
        cached_disk = cache.get(cache_day, f"standings_{comp_code}")
        if isinstance(cached_disk, list) and cached_disk:
            _cache_set(_FD_STANDINGS_CACHE, cache_key, cached_disk)
            return cached_disk[:limit]
    except Exception:
        pass
    if _backoff_active("football-data"):
        return []
    try:
        response = _http_get(
            f"https://api.football-data.org/v4/competitions/{comp_code}/standings",
            headers={"X-Auth-Token": token},
            timeout=12,
        )
        if response.status_code == 429:
            _note_backoff("football-data")
            return []
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
                rows.append(
                    {
                        "position": position,
                        "team": team,
                        "points": points,
                        "played": row.get("playedGames"),
                        "won": row.get("won"),
                        "draw": row.get("draw"),
                        "lost": row.get("lost"),
                        "goals_for": row.get("goalsFor"),
                        "goals_against": row.get("goalsAgainst"),
                    }
                )
        _cache_set(_FD_STANDINGS_CACHE, cache_key, rows)
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



def _stake_from_score(score: float | None, roi: float | None = None) -> float | None:
    if score is None:
        return None
    base = max(0.0, score - 0.6)
    stake = min(2.0, max(0.5, base * 5.0))
    if roi is not None:
        if roi >= 0:
            factor = min(1.25, 1.0 + roi)
        else:
            factor = max(0.7, 1.0 + roi)
        stake *= factor
    return round(min(2.0, max(0.5, stake)), 2)



def _build_best_combo(picks: list[dict], target: float) -> dict | None:
    picks = [item for item in picks if isinstance(item.get("odds"), (int, float)) and item.get("odds") > 1.01]
    picks = sorted(picks, key=lambda item: item["score"], reverse=True)[:30]
    best = None
    nearest = None
    for i in range(len(picks)):
        for j in range(i + 1, len(picks)):
            if picks[i].get("match_key") == picks[j].get("match_key"):
                continue
            score, combined_odds = _combine_score(picks[i], picks[j], target)
            if not (1.85 <= combined_odds <= 2.15):
                distance = abs(combined_odds - target)
                if nearest is None or distance < nearest[0]:
                    nearest = (distance, combined_odds, score, picks[i], picks[j])
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
    if nearest:
        _, combined_odds, score, left, right = nearest
        return {
            "matches": [left, right],
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
        response = _http_get(
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
        result_map_day: dict[tuple[str, str, str], tuple[int, int]] = {}
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
                result_map_day[(str(commence_time)[:10], str(home_team), str(away_team))] = result

        for row in picks:
            pick_id, _, commence_time, home_team, away_team, market_key, outcome, line = row
            result = result_map.get((str(commence_time), str(home_team), str(away_team)))
            if not result:
                result = result_map_day.get((str(commence_time)[:10], str(home_team), str(away_team)))
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


def _settle_pick_from_score(pick: dict, home_goals: int, away_goals: int) -> str | None:
    market_key = str(pick.get("market_key") or "").lower()
    outcome = str(pick.get("outcome") or "")
    line_val = pick.get("line")
    if not isinstance(line_val, (int, float)):
        line_val = _parse_line_from_outcome(outcome)
    # Reuse the full evaluator so every supported market is settled consistently.
    return _evaluate_pick(market_key, outcome, line_val, int(home_goals), int(away_goals))


def _settle_previous_day_picks(db) -> None:
    if not config.football_data_token:
        return
    prev_day = (datetime.now(BUDAPEST_TZ).date() - timedelta(days=1)).isoformat()
    cursor = db.connection.execute(
        """
        SELECT id, home_team, away_team, market_key, outcome
        FROM saved_picks
        WHERE status != 'settled' AND day_key = ?
        ORDER BY created_at DESC
        LIMIT 40
        """,
        (prev_day,),
    )
    rows = cursor.fetchall()
    if not rows:
        return
    headers = {"X-Auth-Token": config.football_data_token}
    for pick_id, home_team, away_team, market_key, outcome in rows:
        home_id = _team_id_map().get(_normalize_name(home_team))
        away_id = _team_id_map().get(_normalize_name(away_team))
        if not home_id or not away_id:
            continue
        try:
            resp = _http_get(
                f"https://api.football-data.org/v4/teams/{home_id}/matches",
                headers=headers,
                params={"status": "FINISHED", "dateFrom": prev_day, "dateTo": prev_day, "limit": "20"},
                timeout=15,
            )
        except Exception:
            continue
        if resp.status_code != 200:
            continue
        matches = resp.json().get("matches", [])
        match_row = next(
            (
                m
                for m in matches
                if int(m.get("homeTeam", {}).get("id", -1)) == int(home_id)
                and int(m.get("awayTeam", {}).get("id", -1)) == int(away_id)
            ),
            None,
        )
        if not match_row:
            continue
        score = match_row.get("score", {}).get("fullTime", {})
        home_goals = score.get("home")
        away_goals = score.get("away")
        if not isinstance(home_goals, int) or not isinstance(away_goals, int):
            continue
        result = _settle_pick_from_score(
            {"market_key": market_key, "outcome": outcome},
            home_goals,
            away_goals,
        )
        if not result:
            continue
        now = datetime.now(timezone.utc).isoformat()
        db.connection.execute(
            """
            UPDATE saved_picks
            SET status = ?, settled_at = ?, result = ?
            WHERE id = ?
            """,
            ("settled", now, result, int(pick_id)),
        )
    db.connection.commit()


def _save_pick(db, payload: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    odds_val = float(payload.get("odds", 0.0) or 0.0)
    has_odds = 1 if odds_val > 1.01 else 0
    source = payload.get("source")
    if not source:
        source = "odds" if has_odds else "no_odds"
    day_key = payload.get("day_key") or datetime.now(BUDAPEST_TZ).strftime("%Y-%m-%d")
    commence_time = payload.get("commence_time") or day_key
    db.connection.execute(
        """
        INSERT OR IGNORE INTO saved_picks
        (created_at, sport_key, commence_time, home_team, away_team, market_key, outcome, line, odds, score, risk, status, source, has_odds, day_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now,
            payload.get("sport_key", ""),
            commence_time,
            payload.get("home_team", ""),
            payload.get("away_team", ""),
            payload.get("market_key", ""),
            payload.get("outcome", ""),
            payload.get("line"),
            odds_val,
            float(payload.get("score", 0.0)),
            payload.get("risk", "yellow"),
            "pending",
            source,
            has_odds,
            day_key,
        ),
    )
    db.connection.commit()


def _list_saved_picks(db, limit: int | None = None) -> list[dict]:
    max_rows = limit if isinstance(limit, int) and limit > 0 else int(os.environ.get("SAVED_PICKS_LIMIT", "1000"))
    cursor = db.connection.execute(
        """
        SELECT id, created_at, commence_time, home_team, away_team, market_key, outcome, odds, status, result, source, day_key
        FROM saved_picks
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (max_rows,),
    )
    rows = cursor.fetchall()
    results = []
    for pick_id, created_at, commence_time, home_team, away_team, market_key, outcome, odds, status, result, source, day_key in rows:
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
                "id": int(pick_id),
                "created_at": str(created_at)[:16],
                "commence_time": str(commence_time or ""),
                "home_team": home_team,
                "away_team": away_team,
                "market_key": market_key,
                "market_label": _market_label(market_key),
                "outcome": outcome,
                "odds": float(odds),
                "status": status,
                "result_label": result_label,
                "source": str(source or ""),
                "day_key": str(day_key or ""),
            }
        )
    return results


def _day_results(db, days: int = 60) -> list[dict]:
    limit_days = max(1, int(days))
    cursor = db.connection.execute(
        """
        SELECT day_key, result, status
        FROM saved_picks
        WHERE day_key IS NOT NULL AND day_key != ''
        ORDER BY day_key DESC, created_at DESC
        LIMIT ?
        """,
        (limit_days * 8,),
    )
    buckets: dict[str, dict[str, int]] = {}
    for day_key, result, status in cursor.fetchall():
        key = str(day_key or "")
        if not key:
            continue
        entry = buckets.setdefault(key, {"wins": 0, "losses": 0, "pushes": 0, "settled": 0, "total": 0})
        entry["total"] += 1
        if status == "settled":
            entry["settled"] += 1
            if result == "win":
                entry["wins"] += 1
            elif result == "lose":
                entry["losses"] += 1
            elif result == "push":
                entry["pushes"] += 1
    out: list[dict] = []
    for day_key, entry in buckets.items():
        if entry["wins"] >= 2:
            status_label = "gyoztes"
        elif entry["settled"] >= 2:
            status_label = "nem jott"
        else:
            status_label = "nyitott"
        out.append({"day_key": day_key, "status_label": status_label, **entry})
    out.sort(key=lambda item: item["day_key"], reverse=True)
    return out[:limit_days]


def _auto_save_picks(db, picks: list[dict]) -> None:
    if not picks:
        return
    day_key = datetime.now(BUDAPEST_TZ).strftime("%Y-%m-%d")
    for pick in picks[:2]:
        if not pick:
            continue
        commence_time = pick.get("commence_time") or ""
        payload = {
            "sport_key": pick.get("sport_key", ""),
            "commence_time": commence_time or day_key,
            "home_team": pick.get("home_team", ""),
            "away_team": pick.get("away_team", ""),
            "market_key": pick.get("market_key", ""),
            "outcome": pick.get("outcome", ""),
            "line": pick.get("line"),
            "odds": pick.get("odds", 0.0),
            "score": pick.get("score", 0.0),
            "risk": pick.get("risk", "yellow"),
            "source": "odds" if isinstance(pick.get("odds"), (int, float)) and pick.get("odds") > 1.01 else "no_odds",
            "day_key": day_key,
        }
        _save_pick(db, payload)
    _update_learning_weights(db)


def _saved_picks_summary(picks: list[dict]) -> dict[str, object]:
    total = len(picks)
    wins = 0
    losses = 0
    pushes = 0
    profit = 0.0
    settled = 0
    for pick in picks:
        result = pick.get("result_label")
        odds = float(pick.get("odds") or 0.0)
        if result == "nyert":
            wins += 1
            settled += 1
            profit += max(0.0, odds - 1.0)
        elif result == "vesztett":
            losses += 1
            settled += 1
            profit -= 1.0
        elif result == "visszajaro":
            pushes += 1
            settled += 1
    roi = (profit / settled) if settled > 0 else 0.0
    return {
        "total": total,
        "settled": settled,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "profit": profit,
        "roi": roi,
    }


def _saved_picks_summary_range(picks: list[dict], days: int) -> dict[str, object]:
    if days <= 0:
        return _saved_picks_summary(picks)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    filtered = []
    for pick in picks:
        created_at = pick.get("created_at")
        if not created_at:
            continue
        try:
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if dt >= cutoff:
            filtered.append(pick)
    return _saved_picks_summary(filtered)


def _saved_picks_summary_by_source(db) -> dict[str, dict[str, object]]:
    cursor = db.connection.execute(
        """
        SELECT source, odds, result
        FROM saved_picks
        WHERE status = 'settled'
        """
    )
    rows = cursor.fetchall()
    by_source: dict[str, list[dict]] = {}
    for source, odds, result in rows:
        by_source.setdefault(str(source or "unknown"), []).append(
            {"odds": odds, "result_label": "nyert" if result == "win" else ("vesztett" if result == "lose" else "visszajaro")}
        )
    summary = {}
    for source, picks in by_source.items():
        summary[source] = _saved_picks_summary(picks)
    return summary


def _daily_roi_series(db, days: int = 30) -> dict[str, list[dict]]:
    start_day = datetime.now(BUDAPEST_TZ).date().isoformat()
    cursor = db.connection.execute(
        """
        SELECT day_key, source, odds, result
        FROM saved_picks
        WHERE status = 'settled' AND day_key >= ?
        """,
        (start_day,),
    )
    rows = cursor.fetchall()
    agg: dict[tuple[str, str], dict[str, float]] = {}
    for day_key, source, odds, result in rows:
        day = str(day_key or "")
        src = str(source or "unknown")
        key = (day, src)
        entry = agg.setdefault(key, {"profit": 0.0, "settled": 0.0})
        try:
            odds_val = float(odds)
        except Exception:
            odds_val = 0.0
        if result == "win":
            entry["profit"] += max(0.0, odds_val - 1.0)
            entry["settled"] += 1.0
        elif result == "lose":
            entry["profit"] -= 1.0
            entry["settled"] += 1.0
        elif result == "push":
            entry["settled"] += 1.0
    series: dict[str, list[dict]] = {"odds": [], "no_odds": []}
    for (day, src), entry in sorted(agg.items()):
        settled = entry.get("settled", 0.0)
        roi = (entry.get("profit", 0.0) / settled) if settled else 0.0
        bucket = "odds" if src == "odds" else "no_odds"
        series[bucket].append({"day": day, "roi": roi, "settled": int(settled)})
    return series


def _market_roi_by_source(db) -> dict[str, dict[str, float]]:
    cursor = db.connection.execute(
        """
        SELECT source, market_key, odds, result
        FROM saved_picks
        WHERE status = 'settled'
        """
    )
    rows = cursor.fetchall()
    stats: dict[str, dict[str, dict[str, float]]] = {}
    for source, market_key, odds, result in rows:
        source_key = str(source or "unknown")
        market = str(market_key or "")
        entry = stats.setdefault(source_key, {}).setdefault(market, {"profit": 0.0, "settled": 0.0})
        try:
            odds_val = float(odds)
        except Exception:
            odds_val = 0.0
        if result == "win":
            entry["profit"] += max(0.0, odds_val - 1.0)
            entry["settled"] += 1.0
        elif result == "lose":
            entry["profit"] -= 1.0
            entry["settled"] += 1.0
        elif result == "push":
            entry["settled"] += 1.0
    out: dict[str, dict[str, float]] = {}
    for source_key, markets in stats.items():
        out[source_key] = {}
        for market, entry in markets.items():
            settled = entry.get("settled", 0.0)
            out[source_key][market] = (entry.get("profit", 0.0) / settled) if settled else 0.0
    return out


def _load_adaptive_weights() -> dict[str, object]:
    path = _DATA_DIR / "adaptive_weights.json"
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    # Avoid recursion with _load_weights when the adaptive file is missing.
    return {"weights": {}, "updated_at": ""}


def _save_adaptive_weights(payload: dict) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with (_DATA_DIR / "adaptive_weights.json").open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2)
    except Exception:
        pass


def _apply_aggressive_learning(final_weights: dict[str, float], roi_map: dict[str, float]) -> dict[str, float]:
    updated = dict(final_weights)
    lr = float(os.environ.get("AGGRESSIVE_LR", "0.25"))
    for key in ("value", "prob", "news"):
        roi = float(roi_map.get(key, 0.0))
        bump = max(-0.2, min(0.2, roi * lr))
        updated[key] = max(0.05, min(0.85, updated.get(key, 0.2) + bump))
    total = sum(updated.values()) or 1.0
    for key in list(updated.keys()):
        updated[key] = updated[key] / total
    return updated


def _merge_adaptive_weights(base: dict[str, object], adaptive: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key in ("final", "final_market", "market_overrides", "model", "model_market"):
        if key in adaptive and isinstance(adaptive.get(key), dict):
            if key not in merged or not isinstance(merged.get(key), dict):
                merged[key] = {}
            if key in {"final", "model"}:
                merged[key].update(adaptive[key])
                continue
            for sub_key, sub_val in adaptive[key].items():
                if isinstance(sub_val, dict):
                    entry = merged[key].get(sub_key, {})
                    if not isinstance(entry, dict):
                        entry = {}
                    entry.update(sub_val)
                    merged[key][sub_key] = entry
                else:
                    merged[key][sub_key] = sub_val
    return merged


def _update_learning_weights(db) -> dict[str, object]:
    base = _load_adaptive_weights()
    weights = base.get("weights") if isinstance(base.get("weights"), dict) else {}
    market_roi = _market_roi_by_source(db)
    odds_roi = market_roi.get("odds", {})
    if odds_roi:
        final_weights = dict(weights.get("final", {"value": 0.5, "prob": 0.3, "news": 0.2}))
        weights["final"] = _apply_aggressive_learning(final_weights, odds_roi)
    payload = {"weights": weights, "updated_at": datetime.now(timezone.utc).isoformat()}
    _save_adaptive_weights(payload)
    return payload


def _market_roi_map(db) -> dict[str, float]:
    now = time.time()
    cached_at = float(_MARKET_ROI_CACHE.get("ts", 0.0))
    if now - cached_at < 3600:
        data = _MARKET_ROI_CACHE.get("data")
        if isinstance(data, dict):
            return data
    cursor = db.connection.execute(
        """
        SELECT market_key, odds, result
        FROM saved_picks
        WHERE status = 'settled'
        """
    )
    rows = cursor.fetchall()
    stats: dict[str, dict[str, float]] = {}
    for market_key, odds, result in rows:
        if not market_key:
            continue
        key = str(market_key)
        entry = stats.setdefault(key, {"profit": 0.0, "settled": 0.0})
        try:
            odds_val = float(odds)
        except Exception:
            odds_val = 0.0
        if result == "win":
            entry["profit"] += max(0.0, odds_val - 1.0)
            entry["settled"] += 1.0
        elif result == "lose":
            entry["profit"] -= 1.0
            entry["settled"] += 1.0
        elif result == "push":
            entry["settled"] += 1.0
    roi_map: dict[str, float] = {}
    for key, entry in stats.items():
        settled = entry.get("settled", 0.0)
        roi_map[key] = (entry.get("profit", 0.0) / settled) if settled else 0.0
    _MARKET_ROI_CACHE["ts"] = now
    _MARKET_ROI_CACHE["data"] = roi_map
    return roi_map


def _market_roi_boost(market_key: str, roi_map: dict[str, float]) -> float:
    roi = float(roi_map.get(market_key, 0.0))
    return max(-0.05, min(0.05, roi))


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


def _is_placeholder_pick(pick: dict | None) -> bool:
    return isinstance(pick, dict) and pick.get("match_key") == "placeholder"


def _persist_pick_snapshot(
    db,
    odds_data: dict | None,
    best_pick: dict | None,
    best_combo: dict | None,
    target_matches: list[dict],
    odds_count: int,
    odds_error: str | None,
    rss_items: list[dict],
    refresh_usage: dict[str, int] | None,
) -> tuple[dict, list[dict]]:
    best_pick, target_matches = _enforce_tip_presence(best_pick, target_matches)
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
            "refresh_usage": refresh_usage or {},
        },
    )
    return best_pick, target_matches


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


def _validate_pick_run(best_pick: dict | None, target_matches: list[dict]) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not best_pick:
        issues.append("nincs_best_pick")
    if _is_placeholder_pick(best_pick):
        issues.append("placeholder_pick")
    if len(target_matches or []) < 2:
        issues.append("keves_tipp")
    markets = {str(item.get("market_key") or "") for item in (target_matches or []) if item.get("market_key")}
    if len(target_matches or []) >= 2 and len(markets) <= 1:
        issues.append("azonos_piac")
    for item in target_matches or []:
        notice = str(item.get("notice") or "")
        if "KORLATOZOTT" in notice.upper():
            issues.append("korlatozott_adat")
            break
    return (len(issues) == 0), issues


def _record_pick_run(db, best_pick: dict | None, target_matches: list[dict], odds_error: str | None) -> None:
    try:
        now_utc = datetime.now(timezone.utc).isoformat()
        day_key = datetime.now(BUDAPEST_TZ).strftime("%Y-%m-%d")
        valid, issues = _validate_pick_run(best_pick, target_matches)
        payload = {
            "odds_error": odds_error,
            "best_pick": {
                "home_team": (best_pick or {}).get("home_team"),
                "away_team": (best_pick or {}).get("away_team"),
                "market_key": (best_pick or {}).get("market_key"),
                "outcome": (best_pick or {}).get("outcome"),
                "score": (best_pick or {}).get("score"),
                "model_prob": (best_pick or {}).get("model_prob"),
            },
            "targets": [
                {
                    "home_team": item.get("home_team"),
                    "away_team": item.get("away_team"),
                    "market_key": item.get("market_key"),
                    "outcome": item.get("outcome"),
                    "score": item.get("score"),
                    "model_prob": item.get("model_prob"),
                    "notice": item.get("notice"),
                }
                for item in (target_matches or [])[:4]
            ],
        }
        db.connection.execute(
            """
            INSERT INTO pick_runs (created_at, day_key, payload, valid, issues)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                now_utc,
                day_key,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                1 if valid else 0,
                json.dumps(issues, ensure_ascii=False),
            ),
        )
        db.connection.commit()
    except Exception:
        pass


def _list_pick_runs(db, limit: int = 40) -> list[dict]:
    try:
        cursor = db.connection.execute(
            """
            SELECT created_at, day_key, payload, valid, issues
            FROM pick_runs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        rows: list[dict] = []
        for created_at, day_key, payload, valid, issues in cursor.fetchall():
            try:
                data = json.loads(payload)
            except Exception:
                data = {}
            try:
                issues_list = json.loads(issues) if issues else []
            except Exception:
                issues_list = []
            rows.append(
                {
                    "created_at": created_at,
                    "day_key": day_key,
                    "valid": bool(valid),
                    "issues": issues_list if isinstance(issues_list, list) else [],
                    "odds_error": data.get("odds_error"),
                    "best_pick": data.get("best_pick") if isinstance(data.get("best_pick"), dict) else {},
                    "targets": data.get("targets") if isinstance(data.get("targets"), list) else [],
                }
            )
        return rows
    except Exception:
        return []


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
        response = _http_get(
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
    use_cache = keys is None
    if use_cache:
        cached_age = time.time() - float(_ODDS_CACHE.get("ts", 0.0))
        if cached_age < ODDS_CACHE_TTL_SECONDS:
            return list(_ODDS_CACHE.get("matches", [])), _ODDS_CACHE.get("error")
    keys = keys or _fetch_sports_keys(api_key)
    max_sports = int(os.environ.get("ODDS_MAX_SPORTS", "0"))
    if max_sports <= 0:
        max_sports = 3
    else:
        max_sports = min(max_sports, 3)
    if max_sports > 0:
        keys = keys[:max_sports]
    markets = ",".join(_market_keys())
    for key in keys:
        try:
            response = _http_get(
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
                fallback = _http_get(
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
    if use_cache:
        _ODDS_CACHE["ts"] = time.time()
        _ODDS_CACHE["matches"] = matches
        _ODDS_CACHE["error"] = _ODDS_LAST_ERROR
    return matches, _ODDS_LAST_ERROR

# HTML template

@app.route('/')
def dashboard():
    global _LAST_PAYLOAD
    t0 = time.perf_counter()
    try:
        if request.args.get("refresh") == "1":
            _render_and_cache(force=True)
        elif not _RESPONSE_CACHE.get("html"):
            # Try a fast render from cached picks without remote calls.
            try:
                with app.app_context():
                    context, payload = _render_dashboard(
                        "tips",
                        refresh_requested=False,
                        render=False,
                        force_refresh=False,
                        allow_remote=False,
                    )
                    html = render_template_string(_get_template(), **context)
                with _RESPONSE_LOCK:
                    _RESPONSE_CACHE["html"] = html
                    _RESPONSE_CACHE["ts"] = time.time()
                    global _LAST_PAYLOAD
                    _LAST_PAYLOAD = payload
            except Exception:
                _trigger_refresh_async()
        with _RESPONSE_LOCK:
            html = _RESPONSE_CACHE.get("html", "")
        if html and _LAST_PAYLOAD:
            comps = _LAST_PAYLOAD.get("competitions") or []
            no_odds = _LAST_PAYLOAD.get("target_matches_no_odds") or []
            best_pick = _LAST_PAYLOAD.get("best_pick") or {}
            if _is_placeholder_pick(best_pick):
                _render_and_cache(force=True)
                with _RESPONSE_LOCK:
                    html = _RESPONSE_CACHE.get("html", "")
            elif not comps or not no_odds:
                with app.app_context():
                    context, _ = _render_dashboard("tips", refresh_requested=False, render=False, force_refresh=False)
                    html = render_template_string(_get_template(), **context)
                with _RESPONSE_LOCK:
                    _RESPONSE_CACHE["html"] = html
                    _RESPONSE_CACHE["ts"] = time.time()
        if not html:
            html = (
                "<html><head><meta http-equiv=\"refresh\" content=\"5\"></head>"
                "<body>Frissites folyamatban...</body></html>"
            )
        dt = (time.perf_counter() - t0) * 1000
        if dt > 5:
            print(f"[SLOW] dashboard() {dt:.1f} ms")
        return html
    except Exception:
        print("[ERROR] dashboard render failed")
        print(traceback.format_exc())
        return ("Internal Server Error", 500)


@app.route("/api/dashboard")
def api_dashboard():
    try:
        if request.args.get("refresh") == "1":
            _render_and_cache(force=True)
            payload = _LAST_PAYLOAD or {}
            return jsonify(payload)
        if _LAST_PAYLOAD:
            return jsonify(_LAST_PAYLOAD)
        _trigger_refresh_async()
        return jsonify({"status": "warming"}), 202
    except Exception:
        print("[ERROR] api_dashboard render failed")
        print(traceback.format_exc())
        return ("Internal Server Error", 500)


def _render_dashboard(
    active_tab: str,
    refresh_requested: bool,
    render: bool = True,
    force_refresh: bool = False,
    allow_remote: bool = True,
):
    _reload_env()
    global _REMOTE_CALLS
    _REMOTE_CALLS = 0
    if not allow_remote:
        refresh_requested = False
        force_refresh = False
    target_odds = 2.0
    window_hours = 24

    db = connect(config.db_url)
    db.ensure_schema()
    cached = _load_cached_picks(db)
    market_roi = _market_roi_map(db)
    cached_updated_at = cached.get("updated_at") if cached else None
    placeholder_refresh = False
    if cached and cached_updated_at:
        cached_dt = _parse_iso_datetime(cached_updated_at)
        if cached_dt and not _same_local_day(cached_dt, BUDAPEST_TZ):
            refresh_requested = True
    if cached and _is_placeholder_pick(cached.get("best_pick")):
        refresh_requested = True
        placeholder_refresh = True
    if not refresh_requested:
        if not cached:
            refresh_requested = True
        elif AUTO_REFRESH_SECONDS > 0 and cached_updated_at:
            cached_dt = _parse_iso_datetime(cached_updated_at)
            if cached_dt:
                age = (datetime.now(timezone.utc) - cached_dt).total_seconds()
                if age >= AUTO_REFRESH_SECONDS:
                    refresh_requested = True
    if refresh_requested and not force_refresh and not placeholder_refresh and cached_updated_at and REFRESH_COOLDOWN_SECONDS > 0:
        cached_dt = _parse_iso_datetime(cached_updated_at)
        if cached_dt:
            age = (datetime.now(timezone.utc) - cached_dt).total_seconds()
            if age < REFRESH_COOLDOWN_SECONDS:
                refresh_requested = False
    if refresh_requested and not force_refresh and not placeholder_refresh and cached_updated_at and MIN_API_REFRESH_SECONDS > 0:
        cached_dt = _parse_iso_datetime(cached_updated_at)
        if cached_dt:
            age = (datetime.now(timezone.utc) - cached_dt).total_seconds()
            if age < MIN_API_REFRESH_SECONDS and _same_local_day(cached_dt, BUDAPEST_TZ):
                refresh_requested = False
    matches = list_matches(db)
    points_map = table(matches)
    form_scores = _build_form_scores(matches)
    table_scores = _build_table_scores(points_map)

    competitions = []
    fallback_competitions = [
        {"code": "BSA", "name": "Campeonato Brasileiro Serie A"},
        {"code": "ELC", "name": "Championship"},
        {"code": "PL", "name": "Premier League"},
        {"code": "CL", "name": "UEFA Champions League"},
        {"code": "EC", "name": "European Championship"},
        {"code": "FL1", "name": "Ligue 1"},
        {"code": "BL1", "name": "Bundesliga"},
        {"code": "SA", "name": "Serie A"},
        {"code": "DED", "name": "Eredivisie"},
        {"code": "PPL", "name": "Primeira Liga"},
        {"code": "CLI", "name": "Copa Libertadores"},
        {"code": "PD", "name": "Primera Division"},
        {"code": "WC", "name": "FIFA World Cup"},
    ]
    recent_matches = []
    standings = []
    if cached and not refresh_requested and not force_refresh:
        competitions = fallback_competitions
    else:
        if not FAST_MODE and allow_remote:
            competitions = _fetch_competitions_fd(config.football_data_token)
        if not competitions:
            competitions = fallback_competitions
    allowed_codes = {str(comp.get("code") or "") for comp in competitions if comp.get("code")}
    primary = _primary_competition(competitions)
    if allow_remote and primary and not (cached and not refresh_requested and not force_refresh):
        recent_matches = _fetch_recent_matches_fd(config.football_data_token, primary)
        standings = _fetch_standings_fd(config.football_data_token, primary)

    odds_data = None
    therundown_map: dict[str, dict] = {}
    therundown_events: list[dict] = []
    therundown_candidates: list[dict] = []
    target_matches = []
    rss_items: list[dict] = []
    news_blocks: list[dict] = []
    odds_count = 0
    odds_error = None
    best_pick = None
    best_combo = None
    refresh_usage: dict[str, int] = {}
    odds_pool_matches: list[dict] = []
    diag_counts = {"comp_24": 0, "all_24": 0, "api_football_24": 0, "window_from": "", "window_to": "", "window_source": "system"}
    standings_by_comp: dict[str, list[dict]] = {}

    if refresh_requested and allow_remote:
        cached_updated_at = None
        _reset_sportradar_budget()
        pre_month, _ = _api_usage_counts()
        try:
            diag_counts = _diagnostics_fixtures(config.football_data_token, competitions, window_hours)
            use_therundown = False
            if _therundown_enabled():
                td_logger = logging.getLogger("therundown_web")
                td_client = _therundown_client()
                cache = DiskCache(_cache_dir())
                today = datetime.now(BUDAPEST_TZ).date()
                date_str = today.isoformat()
                date_str_next = (today + timedelta(days=1)).isoformat()
                events_cache = cache.get(date_str, "therundown_events")
                if events_cache is None or force_refresh:
                    events_cache = []
                    for sport_id in _therundown_sport_ids():
                        try:
                            events_cache.extend(td_client.events_for_date(sport_id, date_str))
                            events_cache.extend(td_client.events_for_date(sport_id, date_str_next))
                        except Exception as exc:
                            td_logger.warning("TheRundown events fetch failed: %s", exc)
                    cache.set(date_str, "therundown_events", events_cache)
                else:
                    refresh_requested = False
                therundown_events = list(events_cache or [])
                for event in therundown_events:
                    teams = td_client.event_teams(event)
                    if not teams:
                        continue
                    event_date = td_client.iso_date(
                        str(
                            event.get("event_date")
                            or event.get("event_date_utc")
                            or event.get("date_event")
                            or event.get("date")
                            or ""
                        ),
                        date_str,
                    )
                    home_name = teams[0]
                    away_name = teams[1]
                    payload = {
                        "home": home_name,
                        "away": away_name,
                        "line_id": td_client.event_line_id(event),
                        "markets": td_client.markets_from_event(event),
                    }
                    therundown_map[_therundown_event_key(event_date, home_name, away_name)] = payload
                    if event_date != date_str:
                        therundown_map[_therundown_event_key(date_str, home_name, away_name)] = payload
                    payload["date"] = event_date
                    home_tokens = set(_token_set(home_name))
                    away_tokens = set(_token_set(away_name))
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
                use_therundown = bool(therundown_map)

            if use_therundown:
                fixtures, standings_by_comp = _fetch_public_fixtures(competitions, window_hours)
                eligible = []
                eligible_with_odds = []
                for match in fixtures:
                    home_team = match.get("home_team", "")
                    away_team = match.get("away_team", "")
                    if not home_team or not away_team:
                        continue
                    match_dt = _parse_match_dt(match)
                    if not match_dt:
                        continue
                    match_date = match_dt.astimezone(BUDAPEST_TZ).date().isoformat()
                    key = _therundown_event_key(match_date, home_team, away_team)
                    therundown_event = therundown_map.get(key)
                    if not therundown_event:
                        therundown_event = _find_therundown_event(
                            match_date, home_team, away_team, therundown_candidates
                        )
                        if therundown_event:
                            therundown_map[key] = therundown_event
                        else:
                            continue
                    bookmakers = _therundown_markets_to_bookmakers(
                        therundown_event.get("markets") or {}, home_team, away_team
                    )
                    if bookmakers:
                        match["bookmakers"] = bookmakers
                    match["therundown_markets"] = therundown_event.get("markets") or {}
                    match["therundown_line_id"] = therundown_event.get("line_id")
                    eligible.append(match)
                for match in eligible:
                    if match.get("bookmakers"):
                        continue
                    line_id = match.get("therundown_line_id")
                    if line_id:
                        _therundown_update_match_odds(match, str(line_id), td_client)
                for match in eligible:
                    markets = match.get("therundown_markets") or _build_odds_markets_from_match(match)
                    match["odds_markets"] = markets
                    if markets and any(
                        markets.get(key)
                        for key in ("1x2", "over_under", "btts", "double_chance")
                    ):
                        eligible_with_odds.append(match)
                odds_count = len(eligible_with_odds)
                odds_pool_matches = [
                    {
                        "home_team": match.get("home_team"),
                        "away_team": match.get("away_team"),
                        "competition": _match_competition(match),
                        "commence_time": str(match.get("commence_time") or match.get("utc") or match.get("date") or ""),
                    }
                    for match in eligible_with_odds[:8]
                ]
                if eligible_with_odds:
                    eligible = eligible_with_odds
                if not eligible:
                    odds_error = "Nincs elerheto oddsos meccs (TheRundown)"
                    fallback_matches = []
                    for event in therundown_events:
                        teams = td_client.event_teams(event)
                        if not teams:
                            continue
                        line_id = td_client.event_line_id(event)
                        if not line_id:
                            continue
                        event_date = td_client.iso_date(
                            str(
                                event.get("event_date")
                                or event.get("event_date_utc")
                                or event.get("date_event")
                                or event.get("date")
                                or ""
                            ),
                            date_str,
                        )
                        markets = td_client.markets_from_event(event)
                        if not markets or not any(
                            markets.get(key) for key in ("1x2", "over_under", "btts", "double_chance")
                        ):
                            continue
                        home_team, away_team = teams
                        fallback_matches.append(
                            {
                                "home_team": home_team,
                                "away_team": away_team,
                                "competition": {"name": _therundown_sport_name(str(event.get("sport_id") or ""))},
                                "commence_time": event.get("event_date") or event.get("event_date_utc"),
                                "therundown_markets": markets,
                                "odds_markets": markets,
                                "therundown_line_id": line_id,
                            }
                        )
                    if fallback_matches:
                        eligible = fallback_matches
                        odds_count = len(fallback_matches)
                        odds_pool_matches = [
                            {
                                "home_team": match.get("home_team"),
                                "away_team": match.get("away_team"),
                                "competition": _match_competition(match),
                                "commence_time": str(match.get("commence_time") or ""),
                            }
                            for match in fallback_matches[:8]
                        ]
                if not eligible:
                    fixtures, standings_by_comp = _fetch_public_fixtures(competitions, window_hours)
                    if fixtures:
                        rss_team_names = []
                        for item in fixtures:
                            home = item.get("home_team")
                            away = item.get("away_team")
                            if home:
                                rss_team_names.append(str(home))
                            if away:
                                rss_team_names.append(str(away))
                        rss_items = _fetch_rss_items(rss_team_names)
                    else:
                        rss_items = []
                    picks = _build_stat_only_picks(fixtures, standings_by_comp, rss_items, market_roi)
                    if not picks:
                        fallback_hours = 168
                        fixtures, standings_by_comp = _fetch_public_fixtures(competitions, fallback_hours)
                        if fixtures:
                            rss_team_names = []
                            for item in fixtures:
                                home = item.get("home_team")
                                away = item.get("away_team")
                                if home:
                                    rss_team_names.append(str(home))
                                if away:
                                    rss_team_names.append(str(away))
                            rss_items = _fetch_rss_items(rss_team_names)
                        else:
                            rss_items = []
                        picks = _build_stat_only_picks(fixtures, standings_by_comp, rss_items, market_roi, fallback_hours)
                        if picks:
                            odds_error = "TheRundown odds nincs (stat-only fallback)"
                    if not picks:
                        api_fixtures = _fetch_upcoming_fixtures_api_football(config.api_football_key, window_hours)
                        if api_fixtures:
                            rss_team_names = []
                            for item in api_fixtures:
                                home = item.get("home_team")
                                away = item.get("away_team")
                                if home:
                                    rss_team_names.append(str(home))
                                if away:
                                    rss_team_names.append(str(away))
                            rss_items = _fetch_rss_items(rss_team_names) if rss_team_names else []
                            picks = _build_stat_only_picks(api_fixtures, {}, rss_items, market_roi, 168)
                    if not picks:
                        sr_fixtures = _fetch_upcoming_fixtures_sportradar(config.sportradar_api_key, window_hours, SR_MAX_FIXTURES)
                        if sr_fixtures:
                            rss_team_names = []
                            for item in sr_fixtures:
                                home = item.get("home_team")
                                away = item.get("away_team")
                                if home:
                                    rss_team_names.append(str(home))
                                if away:
                                    rss_team_names.append(str(away))
                            rss_items = _fetch_rss_items(rss_team_names) if rss_team_names else []
                            picks = _build_stat_only_picks(sr_fixtures, {}, rss_items, market_roi, 168)
                    if not picks:
                        odds_error = "Nincs elerheto meccs 24 oras ablakban"
                    odds_count = 0
                    best_pick, target_matches = _select_stat_picks(picks, limit=2)
                    best_combo = None
                    post_month, _ = _api_usage_counts()
                    refresh_usage = {
                        key: max(0, post_month.get(key, 0) - pre_month.get(key, 0))
                        for key in set(pre_month) | set(post_month)
                    }
            elif not config.odds_api_key:
                odds_error = "Odds API kulcs hianyzik (odds nelkuli ajanlas)"
                data = []
                eligible = []
                fixtures, standings_by_comp = _fetch_public_fixtures(competitions, window_hours)
                if fixtures:
                    rss_team_names = []
                    for item in fixtures:
                        home = item.get("home_team")
                        away = item.get("away_team")
                        if home:
                            rss_team_names.append(str(home))
                        if away:
                            rss_team_names.append(str(away))
                    rss_items = _fetch_rss_items(rss_team_names)
                else:
                    rss_items = []
                picks = _build_stat_only_picks(fixtures, standings_by_comp, rss_items, market_roi)
                if not picks:
                    fallback_hours = 24
                    fixtures, standings_by_comp = _fetch_public_fixtures(competitions, fallback_hours)
                    if fixtures:
                        rss_team_names = []
                        for item in fixtures:
                            home = item.get("home_team")
                            away = item.get("away_team")
                            if home:
                                rss_team_names.append(str(home))
                            if away:
                                rss_team_names.append(str(away))
                        rss_items = _fetch_rss_items(rss_team_names)
                    else:
                        rss_items = []
                    picks = _build_stat_only_picks(fixtures, standings_by_comp, rss_items, market_roi)
                    if picks:
                        odds_error = "Odds API kulcs hianyzik (odds nelkuli ajanlas, 24 oras ablak)"
                if not picks:
                    odds_error = "Nincs elerheto meccs 24 oras ablakban"
                odds_count = 0
                best_pick, target_matches = _select_stat_picks(picks, limit=2)
                best_combo = None
                post_month, _ = _api_usage_counts()
                refresh_usage = {
                    key: max(0, post_month.get(key, 0) - pre_month.get(key, 0))
                    for key in set(pre_month) | set(post_month)
                }
                best_pick, target_matches = _persist_pick_snapshot(
                    db,
                    odds_data,
                    best_pick,
                    best_combo,
                    target_matches,
                    odds_count,
                    odds_error,
                    rss_items,
                    refresh_usage,
                )
            else:
                data, odds_error = _fetch_odds_matches(config.odds_api_key)
                eligible = []
                use_odds = bool(data) and not odds_error
                if not use_odds:
                    if not odds_error:
                        odds_error = "Odds API adat nem elerheto (odds nelkuli ajanlas)"
                    fixtures, standings_by_comp = _fetch_public_fixtures(competitions, window_hours)
                    if fixtures:
                        rss_team_names = []
                        for item in fixtures:
                            home = item.get("home_team")
                            away = item.get("away_team")
                            if home:
                                rss_team_names.append(str(home))
                            if away:
                                rss_team_names.append(str(away))
                        rss_items = _fetch_rss_items(rss_team_names)
                    else:
                        rss_items = []
                    picks = _build_stat_only_picks(fixtures, standings_by_comp, rss_items, market_roi)
                    if not picks:
                        fallback_hours = 24
                        fixtures, standings_by_comp = _fetch_public_fixtures(competitions, fallback_hours)
                        if fixtures:
                            rss_team_names = []
                            for item in fixtures:
                                home = item.get("home_team")
                                away = item.get("away_team")
                                if home:
                                    rss_team_names.append(str(home))
                                if away:
                                    rss_team_names.append(str(away))
                            rss_items = _fetch_rss_items(rss_team_names)
                        else:
                            rss_items = []
                        picks = _build_stat_only_picks(fixtures, standings_by_comp, rss_items, market_roi)
                        if picks:
                            odds_error = "Odds API adat nem elerheto (24 oras ablak)"
                    if not picks:
                        odds_error = "Nincs elerheto meccs 24 oras ablakban"
                    odds_count = 0
                    best_pick, target_matches = _select_stat_picks(picks, limit=2)
                    best_combo = None
                    post_month, _ = _api_usage_counts()
                    refresh_usage = {
                        key: max(0, post_month.get(key, 0) - pre_month.get(key, 0))
                        for key in set(pre_month) | set(post_month)
                    }
                    best_pick, target_matches = _persist_pick_snapshot(
                        db,
                        odds_data,
                        best_pick,
                        best_combo,
                        target_matches,
                        odds_count,
                        odds_error,
                        rss_items,
                        refresh_usage,
                    )
                if use_odds and data:
                    eligible_fallback: list[dict] = []
                    for match in data:
                        home_team = match.get("home_team", "")
                        away_team = match.get("away_team", "")
                        if _is_friendly_comp(code=str(match.get("sport_key") or ""), sport_title=str(match.get("sport_title") or "")):
                            continue
                        is_cup = _is_cup_comp(str(match.get("sport_title") or ""), str(match.get("sport_key") or ""))
                        comp_code = _sport_key_to_comp(str(match.get("sport_key") or ""))
                        if allowed_codes and (not comp_code or comp_code not in allowed_codes):
                            continue
                        if not _within_hours(match, window_hours):
                            continue
                        is_derby = RISK_EXCLUDE_DERBY and _is_rivalry(home_team, away_team)
                        if (RISK_EXCLUDE_CUP and is_cup) or is_derby:
                            eligible_fallback.append(match)
                            continue
                        eligible.append(match)
                    if not eligible and eligible_fallback:
                        eligible = eligible_fallback[:]
            if eligible:
                odds_count = len(eligible)
                if not odds_pool_matches:
                    odds_pool_matches = [
                        {
                            "home_team": match.get("home_team"),
                            "away_team": match.get("away_team"),
                            "competition": match.get("sport_title") or match.get("sport_key"),
                            "commence_time": str(match.get("commence_time") or ""),
                        }
                    for match in eligible[:8]
                ]
            # mindig toltsuk fel az odds nelkuli boxot, ha nincs oddsos match
            if not target_matches and not eligible:
                fallback_fixtures, standings_by_comp = _fetch_public_fixtures(competitions, window_hours)
                if fallback_fixtures:
                    rss_team_names = []
                    for item in fallback_fixtures:
                        home = item.get("home_team")
                        away = item.get("away_team")
                        if home:
                            rss_team_names.append(str(home))
                        if away:
                            rss_team_names.append(str(away))
                    rss_items = _fetch_rss_items(rss_team_names)
                else:
                    rss_items = []
                fallback_picks = _build_stat_only_picks(fallback_fixtures, standings_by_comp, rss_items, market_roi)
                if fallback_picks:
                    best_pick, target_matches = _select_stat_picks(fallback_picks, limit=2)
                if eligible:
                    rss_team_names = []
                    for item in eligible:
                        home = item.get("home_team")
                        away = item.get("away_team")
                        if home:
                            rss_team_names.append(str(home))
                        if away:
                            rss_team_names.append(str(away))
                    rss_items = _fetch_rss_items(rss_team_names)
                else:
                    rss_items = []
                picks: list[dict] = []
                filtered_picks: list[dict] = []
                for match in eligible:
                    match_dt = _parse_match_dt(match)
                    if not match_dt:
                        continue
                    match_local = match_dt.astimezone(BUDAPEST_TZ)
                    if not _within_next_24h(match_local, datetime.now(BUDAPEST_TZ)):
                        continue
                    picks.extend(_build_picks_for_match(match, target_odds, rss_items, db, form_scores, table_scores, market_roi))
                if picks:
                    filtered_picks = _filter_picks_by_risk(picks)
                    best_combo = _build_best_combo(filtered_picks, target_odds)
                    best_pick = max(filtered_picks, key=lambda item: item["score"])
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
                        local_matches = list_matches(db)
                        home_stats = get_team_stats(db, home_team, matches=local_matches)
                        away_stats = get_team_stats(db, away_team, matches=local_matches)
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
                    target_source = filtered_picks if filtered_picks else picks
                    target_matches = _select_picks_near_odds(target_source, target_odds, limit=2)
                    target_matches = [_enrich_pick(item) for item in target_matches]
                    if use_therundown:
                        picks_to_update = []
                        if best_pick:
                            picks_to_update.append(best_pick)
                        if target_matches:
                            picks_to_update.extend(target_matches)
                        for pick in picks_to_update:
                            if not pick:
                                continue
                            home_norm = _normalize_team_name(pick.get("home_team", ""))
                            away_norm = _normalize_team_name(pick.get("away_team", ""))
                            match = next(
                                (
                                    item
                                    for item in eligible
                                    if _normalize_team_name(item.get("home_team", "")) == home_norm
                                    and _normalize_team_name(item.get("away_team", "")) == away_norm
                                ),
                                None,
                            )
                            if not match:
                                continue
                            line_id = match.get("therundown_line_id")
                            if line_id:
                                _therundown_update_match_odds(match, str(line_id), td_client)
                                markets = _build_odds_markets_from_match(match)
                                if markets:
                                    pick["odds"] = _odds_for_pick(pick.get("market_key", ""), pick.get("outcome", ""), markets)
                if best_combo and best_combo.get("matches"):
                    best_combo["matches"] = [_enrich_pick(item) for item in best_combo["matches"]]
            post_month, _ = _api_usage_counts()
            refresh_usage = {
                key: max(0, post_month.get(key, 0) - pre_month.get(key, 0))
                for key in set(pre_month) | set(post_month)
            }
            if _is_placeholder_pick(best_pick) and cached:
                cached_best = cached.get("best_pick")
                cached_targets = cached.get("target_matches") or []
                if cached_best and not _is_placeholder_pick(cached_best):
                    best_pick = _enrich_pick(cached_best)
                    target_matches = [_enrich_pick(item) for item in cached_targets] if cached_targets else []
                    odds_count = int(cached.get("odds_count", odds_count) or 0)
                    odds_error = cached.get("odds_error", odds_error)
                    odds_pool_matches = cached.get("odds_pool_matches", odds_pool_matches) or odds_pool_matches
            if _is_placeholder_pick(best_pick):
                fallback_fixtures, fallback_standings = _fetch_public_fixtures(competitions, window_hours)
                fallback_picks = _build_stat_only_picks(fallback_fixtures, fallback_standings, rss_items, market_roi)
                if fallback_picks:
                    best_pick, target_matches = _select_stat_picks(fallback_picks, limit=2)
            _store_cached_picks(
                db,
                {
                    "odds_data": odds_data,
                    "best_pick": best_pick,
                    "best_combo": best_combo,
                    "target_matches": target_matches,
                    "odds_count": odds_count,
                    "odds_error": odds_error,
                    "odds_pool_matches": odds_pool_matches,
                    "rss_sources": ", ".join(sorted({item.get("source", "") for item in rss_items if item.get("source")})),
                    "refresh_usage": refresh_usage,
                },
            )
            _record_pick_run(db, best_pick, target_matches or [], odds_error)
            if target_matches:
                if SR_ENABLE_MAPPING:
                    _fetch_mappings_sportradar()
                if SR_ENABLE_PUSH:
                    _fetch_push_feed_sportradar()
                if SR_ENABLE_LIVE:
                    _fetch_live_schedules_sportradar()
                    _fetch_live_summaries_sportradar()
                    _fetch_live_timeline_delta_sportradar()
        except Exception:
            print("[ERROR] refresh failed")
            print(traceback.format_exc())
        finally:
            best_pick, target_matches = _enforce_tip_presence(best_pick, target_matches)
        _settle_saved_picks(db, config.odds_api_key)
        _settle_previous_day_picks(db)
    else:
        if cached:
            odds_data = cached.get("odds_data")
            best_combo = cached.get("best_combo")
            raw_target_matches = cached.get("target_matches", [])
            odds_count = cached.get("odds_count", 0)
            odds_error = cached.get("odds_error")
            odds_pool_matches = cached.get("odds_pool_matches", [])
            cached_updated_at = cached.get("updated_at")
            refresh_usage = cached.get("refresh_usage", {}) if cached else {}
            best_pick, target_matches = _enforce_tip_presence(cached.get("best_pick"), raw_target_matches)
            best_pick = _enrich_pick(best_pick) if best_pick else None
            target_matches = [_enrich_pick(item) for item in target_matches] if target_matches else []
    if allow_remote:
        fallback_fixtures, fallback_standings = _fetch_public_fixtures(competitions, 24)
    else:
        fallback_fixtures, fallback_standings = [], {}
    target_matches = _normalize_target_matches(
        best_pick,
        target_matches,
        best_combo,
        fallback_fixtures,
        fallback_standings,
        rss_items,
        market_roi,
    )
    if allow_remote and not best_pick and odds_pool_matches:
        pseudo = []
        live_fixtures, live_standings = _fetch_public_fixtures(competitions, 24)
        for item in odds_pool_matches:
            home = item.get("home_team")
            away = item.get("away_team")
            match = None
            if home and away:
                home_norm = _normalize_team_name(home)
                away_norm = _normalize_team_name(away)
                match = next(
                    (
                        m
                        for m in live_fixtures
                        if _normalize_team_name(m.get("home_team", "")) == home_norm
                        and _normalize_team_name(m.get("away_team", "")) == away_norm
                    ),
                    None,
                )
                if not match:
                    match = _best_fixture_match(home, away, live_fixtures)
            comp_name = ""
            if match and match.get("competition"):
                comp_name = _match_competition(match)
            if not comp_name:
                comp_name = str(item.get("competition") or "")
            comp_code = match.get("comp_code") if match else ""
            if not comp_code:
                comp_code = _comp_name_to_fd_code(comp_name) or ""
            pseudo.append(
                {
                    "home_team": home,
                    "away_team": away,
                    "competition": (match.get("competition") if match else item.get("competition")),
                    "comp_code": comp_code,
                    "fd_code": (match.get("fd_code") if match else comp_code),
                    "sport_key": (match.get("sport_key") if match else ""),
                    "home_id": (match.get("home_id") if match else None),
                    "away_id": (match.get("away_id") if match else None),
                    "season": (match.get("season") if match else None),
                    "sr_season_id": (match.get("sr_season_id") if match else None),
                    "commence_time": (match.get("commence_time") if match else item.get("commence_time") or ""),
                }
            )
        standings_source = live_standings if live_standings else {}
        pseudo_picks = _build_stat_only_picks(pseudo, standings_source, rss_items, market_roi, 24)
        if pseudo_picks:
            best_pick, target_matches = _select_stat_picks(pseudo_picks, limit=2)
            target_matches = _normalize_target_matches(
                best_pick,
                target_matches,
                best_combo,
                fallback_fixtures,
                fallback_standings,
                rss_items,
                market_roi,
            )
    if _is_placeholder_pick(best_pick):
        fallback_fixtures, fallback_standings = _fetch_public_fixtures(competitions, 168)
        fallback_picks = _build_stat_only_picks(fallback_fixtures, fallback_standings, rss_items, market_roi, 168)
        if fallback_picks:
            best_pick, target_matches = _select_stat_picks(fallback_picks, limit=2)
            target_matches = _normalize_target_matches(best_pick, target_matches, best_combo)
    if not standings and standings_by_comp:
        # Reuse already-fetched league standings instead of calling the API again.
        primary = _primary_competition(competitions)
        primary_code = str((primary or {}).get("code") or "")
        if primary_code and primary_code in standings_by_comp:
            standings = standings_by_comp.get(primary_code) or []
        if not standings:
            for rows in standings_by_comp.values():
                if rows:
                    standings = rows
                    break
    if not standings:
        # Fall back to any table entries discovered during team summary fetches.
        inferred_rows: list[dict] = []
        seen: set[str] = set()
        for pick in (target_matches or [])[:4]:
            for side in ("home_summary", "away_summary"):
                summary = pick.get(side) if isinstance(pick, dict) else None
                table_entry = summary.get("table_entry") if isinstance(summary, dict) else None
                if not isinstance(table_entry, dict):
                    continue
                team = str(table_entry.get("team") or "")
                if not team or team in seen:
                    continue
                seen.add(team)
                inferred_rows.append(
                    {
                        "position": table_entry.get("position"),
                        "team": team,
                        "points": table_entry.get("points"),
                        "played": table_entry.get("played"),
                        "goals_for": table_entry.get("goals_for"),
                        "goals_against": table_entry.get("goals_against"),
                    }
                )
        inferred_rows.sort(key=lambda row: (row.get("position") is None, row.get("position") or 999))
        if inferred_rows:
            standings = inferred_rows
    _settle_previous_day_picks(db)
    saved_picks = _list_saved_picks(db)
    saved_summary = _saved_picks_summary(saved_picks)
    saved_summary_15d = _saved_picks_summary_range(saved_picks, 15)
    saved_summary_30d = _saved_picks_summary_range(saved_picks, 30)
    saved_summary_60d = _saved_picks_summary_range(saved_picks, 60)
    saved_summary_90d = _saved_picks_summary_range(saved_picks, 90)
    saved_summary_180d = _saved_picks_summary_range(saved_picks, 180)
    saved_by_source = _saved_picks_summary_by_source(db)
    pick_runs = _list_pick_runs(db)
    day_results = _day_results(db)
    stake_pct = _stake_from_score(best_pick.get("score") if best_pick else None, saved_summary_30d.get("roi"))

    configured_sources = ", ".join(sorted({item.get("label", "") for item in _load_rss_sources() if item.get("label")}))
    rss_sources = configured_sources
    if cached_updated_at and not rss_sources:
        rss_sources = cached.get("rss_sources", "") if cached else ""
    tip_list = target_matches or ([best_pick] if best_pick else [])
    news_blocks = _news_blocks(tip_list, rss_items)
    efficiency_score = _efficiency_score(tip_list, rss_items, cached_updated_at) if tip_list else 0.0
    api_quota = _api_quota_snapshot()
    api_items = [
        {
            "key": "odds",
            "icon": "API",
            "name": "Odds API",
            "meta": "Odds feed - manualis frissites",
            "configured": bool(config.odds_api_key),
            "status": "ok" if config.odds_api_key else "bad",
            "label": "Beallitva" if config.odds_api_key else "Hianyzik",
        },
        {
            "key": "therundown",
            "icon": "RD",
            "name": "TheRundown",
            "meta": "RapidAPI odds",
            "configured": _therundown_enabled(),
            "status": "ok" if _therundown_enabled() else "bad",
            "label": "Beallitva" if _therundown_enabled() else "Hianyzik",
        },
        {
            "key": "football_data",
            "icon": "FD",
            "name": "Football Data",
            "meta": "Meccsek, tabella, eredmenyek",
            "configured": bool(config.football_data_token),
            "status": "ok" if config.football_data_token else "bad",
            "label": "Beallitva" if config.football_data_token else "Hianyzik",
        },
        {
            "key": "api_football",
            "icon": "AF",
            "name": "API-Football",
            "meta": "Reszletes meccs/forma",
            "configured": bool(config.api_football_key),
            "status": "ok" if config.api_football_key else "bad",
            "label": "Beallitva" if config.api_football_key else "Hianyzik",
        },
        {
            "key": "sportradar",
            "icon": "SR",
            "name": "Sportradar",
            "meta": "Meccslista (fallback)",
            "configured": bool(config.sportradar_api_key),
            "status": "ok" if config.sportradar_api_key else "bad",
            "label": "Beallitva" if config.sportradar_api_key else "Hianyzik",
        },
        {
            "key": "open_meteo",
            "icon": "WX",
            "name": "Idojaras",
            "meta": "Open-Meteo - kulcs nelkul",
            "configured": True,
            "status": "ok",
            "label": "OK",
        },
        {
            "key": "rss",
            "icon": "NEWS",
            "name": "Hirek",
            "meta": f"RSS osszefoglalo - {rss_sources or 'n/a'}",
            "configured": True,
            "status": "ok" if rss_items else "warn",
            "label": "OK" if rss_items else "Korlatozott",
        },
        {
            "key": "translate",
            "icon": "TR",
            "name": "Forditas",
            "meta": "Google Translate - kulcs nelkul",
            "configured": True,
            "status": "ok",
            "label": "OK",
        },
        {
            "key": "clubelo",
            "icon": "ELO",
            "name": "ClubElo",
            "meta": "Csapaterosseg (clubelo.com)",
            "configured": True,
            "status": "ok",
            "label": "OK",
        },
    ]
    for item in api_items:
        quota = api_quota.get(item["key"], {"used": 0, "limit": None, "remaining": None})
        header = quota.get("header") or {}
        h_remaining = header.get("remaining")
        h_limit = header.get("limit")
        h_used = header.get("used")
        window = header.get("window")
        remaining = h_remaining if h_remaining is not None else quota.get("remaining")
        limit = h_limit if h_limit is not None else quota.get("limit")
        used = h_used if h_used is not None else quota.get("used", 0)
        used_today = int(quota.get("used_today", 0) or 0)
        refresh_used = int(refresh_usage.get(item["key"], 0) or 0)
        limit_text = str(limit) if limit is not None else "n/a"
        remaining_text = str(remaining) if remaining is not None else "n/a"
        if window and remaining is not None:
            remaining_text = f"{remaining_text} ({window})"
        item["quota_text"] = (
            f"Limit: {limit_text}, maradek: {remaining_text}, felhasznalt: {used}, "
            f"ma: {used_today}, frissites: +{refresh_used}"
        )
    api_total = len(api_items)
    api_online = sum(1 for item in api_items if item.get("status") in {"ok"})
    _auto_save_picks(db, target_matches or [])
    target_matches_odds = [pick for pick in (target_matches or []) if (pick.get("odds") or 0) > 1.01]
    if not target_matches_odds and target_matches:
        if target_matches[0].get("odds") and target_matches[0].get("odds") > 1.01:
            target_matches_odds = target_matches[:2]
    target_matches_no_odds = [pick for pick in (target_matches or []) if not ((pick.get("odds") or 0) > 1.01)]
    active_odds = target_matches_odds[:2]
    active_no_odds = target_matches_no_odds[:2]
    if not odds_pool_matches or not target_matches_odds:
        odds_count = 0
    daily_roi = _daily_roi_series(db)
    context = {
        "odds_configured": bool(config.odds_api_key),
        "football_configured": bool(config.football_data_token),
        "competitions": competitions,
        "odds": odds_data,
        "best_pick": best_pick,
        "best_combo": best_combo,
        "odds_count": odds_count,
        "odds_pool_matches": odds_pool_matches,
        "target_matches": target_matches,
        "target_matches_odds": target_matches_odds,
        "target_matches_no_odds": target_matches_no_odds,
        "active_odds": active_odds,
        "active_no_odds": active_no_odds,
        "recent_matches": recent_matches,
        "standings": standings,
        "_match_reasons": _match_reasons,
        "rss_items": rss_items,
        "rss_sources": rss_sources,
        "configured_sources": configured_sources,
        "news_blocks": news_blocks,
        "api_items": api_items,
        "api_status_online": api_online,
        "api_status_total": api_total,
        "saved_picks": saved_picks,
        "saved_summary": saved_summary,
        "saved_summary_15d": saved_summary_15d,
        "saved_summary_30d": saved_summary_30d,
        "saved_summary_60d": saved_summary_60d,
        "saved_summary_90d": saved_summary_90d,
        "saved_summary_180d": saved_summary_180d,
        "saved_by_source": saved_by_source,
        "pick_runs": pick_runs,
        "day_results": day_results,
        "daily_roi": daily_roi,
        "stake_pct": stake_pct,
        "diag_counts": diag_counts,
        "cached_updated_at": cached_updated_at,
        "odds_error": odds_error,
        "active_tab": active_tab,
        "refresh_requested": refresh_requested,
        "efficiency_score": efficiency_score,
        "backtest_mode": _backtest_mode_enabled(),
        "today_date": datetime.now(BUDAPEST_TZ).strftime("%Y-%m-%d"),
    }
    payload = {
        "target_matches": [
            {
                "home": pick.get("home_team"),
                "away": pick.get("away_team"),
                "score": round(pick.get("score") or 0.0, 2),
                "value": round((pick.get("value") or 0.0), 3),
            }
            for pick in target_matches[:4]
        ],
        "standings": [
            {
                "team": team.get("team"),
                "points": team.get("points"),
            }
            for team in standings[:5]
        ],
        "diag": {
            "comp_24": diag_counts.get("comp_24"),
            "all_24": diag_counts.get("all_24"),
            "api_football_24": diag_counts.get("api_football_24"),
            "window_from": diag_counts.get("window_from"),
            "window_to": diag_counts.get("window_to"),
            "window_source": diag_counts.get("window_source"),
        },
        "rss_count": len(rss_items),
        "saved": len(saved_picks),
        "timestamp": cached_updated_at,
    }
    if not render:
        return context, payload
    return render_template_string(_get_template(), **context)


_start_background_refresh()


@app.route("/health")
def health():
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "fast_mode": FAST_MODE,
    }


@app.route("/api/stats")
def api_stats():
    db = connect(config.db_url)
    db.ensure_schema()
    saved = _list_saved_picks(db)
    return jsonify(
        {
            "summary_all": _saved_picks_summary(saved),
            "summary_30d": _saved_picks_summary_range(saved, 30),
            "summary_90d": _saved_picks_summary_range(saved, 90),
            "by_source": _saved_picks_summary_by_source(db),
            "daily_roi": _daily_roi_series(db),
        }
    )


@app.route("/api/odds/<action>")
def therundown_odds(action: str):
    if not _backtest_mode_enabled():
        return jsonify({"error": "BACKTEST_MODE disabled"}), 403
    if not _therundown_enabled():
        return jsonify({"error": "RapidAPI not configured"}), 400
    date_str = request.args.get("date") or datetime.now(BUDAPEST_TZ).strftime("%Y-%m-%d")
    event_id = request.args.get("event_id", "")
    cache = DiskCache(_cache_dir())
    client = _therundown_client()

    if action == "openers":
        data = {}
        for sport_id in os.environ.get("THERUNDOWN_SPORT_IDS", os.environ.get("THERUNDOWN_SPORT_ID_SOCCER", "16")).split(","):
            sport_id = sport_id.strip()
            if not sport_id:
                continue
            data[sport_id] = client.openers(sport_id, date_str)
        path = cache.set(date_str, "therundown_openers", data)
        return jsonify({"ok": True, "cache_path": path})
    if action == "closing":
        data = {}
        for sport_id in os.environ.get("THERUNDOWN_SPORT_IDS", os.environ.get("THERUNDOWN_SPORT_ID_SOCCER", "16")).split(","):
            sport_id = sport_id.strip()
            if not sport_id:
                continue
            data[sport_id] = client.closing(sport_id, date_str)
        path = cache.set(date_str, "therundown_closing", data)
        return jsonify({"ok": True, "cache_path": path})
    if action == "delta":
        last_id = request.args.get("last_id", "1")
        try:
            data = client.delta_changed_events(last_id)
        except Exception as exc:
            data = {"error": str(exc), "last_id": last_id}
        path = cache.set(date_str, "therundown_delta", data)
        return jsonify({"ok": True, "cache_path": path})
    if action == "lines":
        if not event_id:
            return jsonify({"error": "event_id missing"}), 400
        data = client.lines_historical(event_id)
        path = cache.set(date_str, f"therundown_lines_{event_id}", data)
        return jsonify({"ok": True, "cache_path": path})

    return jsonify({"error": "unknown action"}), 400


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


@app.route("/api/manual_pick", methods=["POST"])
def api_manual_pick():
    db = connect(config.db_url)
    db.ensure_schema()
    data = request.get_json(silent=True) or {}
    payload = {
        "sport_key": data.get("sport_key", ""),
        "commence_time": data.get("commence_time", ""),
        "home_team": data.get("home_team", ""),
        "away_team": data.get("away_team", ""),
        "market_key": data.get("market_key", ""),
        "outcome": data.get("outcome", ""),
        "line": data.get("line"),
        "odds": data.get("odds", "0"),
        "score": data.get("score", "0"),
        "risk": data.get("risk", "yellow"),
    }
    try:
        if payload["line"] is not None:
            payload["line"] = float(payload["line"])
    except Exception:
        payload["line"] = None
    _save_pick(db, payload)
    return jsonify({"ok": True})


def _find_saved_pick_id(db, payload: dict) -> int | None:
    cursor = db.connection.execute(
        """
        SELECT id
        FROM saved_picks
        WHERE sport_key = ?
          AND commence_time = ?
          AND home_team = ?
          AND away_team = ?
          AND market_key = ?
          AND outcome = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (
            payload.get("sport_key", ""),
            payload.get("commence_time", ""),
            payload.get("home_team", ""),
            payload.get("away_team", ""),
            payload.get("market_key", ""),
            payload.get("outcome", ""),
        ),
    )
    row = cursor.fetchone()
    return int(row[0]) if row else None


@app.route("/api/manual_settle", methods=["POST"])
def api_manual_settle():
    db = connect(config.db_url)
    db.ensure_schema()
    data = request.get_json(silent=True) or {}
    pick_id = data.get("id")
    result = str(data.get("result") or "").strip().lower()
    if result not in {"win", "lose", "push"}:
        return jsonify({"error": "result must be win/lose/push"}), 400
    if pick_id is None:
        pick_id = _find_saved_pick_id(db, data)
    if pick_id is None:
        return jsonify({"error": "pick not found"}), 404
    now = datetime.now(timezone.utc).isoformat()
    db.connection.execute(
        """
        UPDATE saved_picks
        SET status = ?, settled_at = ?, result = ?
        WHERE id = ?
        """,
        ("settled", now, result, int(pick_id)),
    )
    db.connection.commit()
    return jsonify({"ok": True, "id": int(pick_id)})

if __name__ == '__main__':
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug)

