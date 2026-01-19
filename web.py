from flask import Flask, render_template_string, request, redirect, url_for, jsonify
from flask_compress import Compress
from soccer_bot.config import load_config
from soccer_bot.db import connect
from soccer_bot.repo import Match, get_team_stats, list_matches
from soccer_bot.offline_stats import build_team_summary
from soccer_bot.scoring import match_points, table
import html
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


def _server_time_utc() -> datetime | None:
    if _SERVER_TIME_CACHE["value"] and (time.time() - _SERVER_TIME_CACHE["ts"]) < 3600:
        return _SERVER_TIME_CACHE["value"]
    global _SERVER_TIME_SOURCE
    key = config.api_football_key
    if key:
        try:
            response = _HTTP.get(
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
            response = _HTTP.get(
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
        response = _HTTP.get(
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
_CACHE_KEY = "latest_picks"
_CACHE_RESET_LOCK = threading.Lock()
SPORTS_CACHE_TTL_SECONDS = 3600
FD_COMP_TTL_SECONDS = int(os.environ.get("FD_COMP_TTL_SECONDS", "21600"))
FD_RECENT_TTL_SECONDS = int(os.environ.get("FD_RECENT_TTL_SECONDS", "600"))
FD_STANDINGS_TTL_SECONDS = int(os.environ.get("FD_STANDINGS_TTL_SECONDS", "1800"))
FD_FIXTURES_TTL_SECONDS = int(os.environ.get("FD_FIXTURES_TTL_SECONDS", "120"))
AF_FIXTURES_TTL_SECONDS = int(os.environ.get("AF_FIXTURES_TTL_SECONDS", "120"))
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
_FD_COMP_CACHE: dict[str, dict[str, object]] = {}
_FD_RECENT_CACHE: dict[str, dict[str, object]] = {}
_FD_STANDINGS_CACHE: dict[str, dict[str, object]] = {}
_FD_FIXTURES_CACHE: dict[str, dict[str, object]] = {}
_FDCO_TABLE_CACHE: dict[str, dict[str, object]] = {}
_AF_FIXTURES_CACHE: dict[str, dict[str, object]] = {}
_ODDS_CACHE: dict[str, object] = {"ts": 0.0, "matches": [], "error": None}
_TEAM_PPG_CACHE: dict[int, dict[str, object]] = {}
_TEAM_SUMMARY_CACHE: dict[str, dict[str, object]] = {}
_TEAM_RECENT_CACHE: dict[str, dict[str, object]] = {}
_TRANSLATE_CACHE: dict[str, dict[str, object]] = {}
_TEAM_ID_CACHE: dict[str, dict[str, object]] = {}
_FIXTURE_STATS_CACHE: dict[str, dict[str, object]] = {}
_TEAM_SQUAD_CACHE: dict[str, dict[str, object]] = {}
_ODDS_MARKETS_DEFAULT = "h2h,totals,btts,team_totals,spreads,draw_no_bet,double_chance,alternate_totals,alternate_team_totals,alternate_spreads"
BACKGROUND_REFRESH_SECONDS = int(os.environ.get("BACKGROUND_REFRESH_SECONDS", "600"))
OFFLINE_FIXTURES_TTL_SECONDS = int(os.environ.get("OFFLINE_FIXTURES_TTL_SECONDS", "3600"))
TEAM_SQUAD_TTL_SECONDS = int(os.environ.get("TEAM_SQUAD_TTL_SECONDS", "2592000"))
TEAM_SQUAD_MAX_FETCH_PER_MONTH = int(os.environ.get("TEAM_SQUAD_MAX_FETCH_PER_MONTH", "30"))

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


TEMPLATE = _load_template()


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
        now = time.time()
        try:
            if not FAST_MODE:
                if RSS_REFRESH_SECONDS > 0 and (now - _LAST_RSS_FETCH) >= RSS_REFRESH_SECONDS:
                    _fetch_rss_items()
                    _LAST_RSS_FETCH = now
                if config.football_data_token and STANDINGS_REFRESH_SECONDS > 0 and (now - _LAST_STANDINGS_FETCH) >= STANDINGS_REFRESH_SECONDS:
                    competitions = _fetch_competitions_fd(config.football_data_token)
                    if competitions:
                        _fetch_public_fixtures(competitions, 24)
                    _LAST_STANDINGS_FETCH = now
                if config.odds_api_key and ODDS_REFRESH_SECONDS > 0 and (now - _LAST_ODDS_FETCH) >= ODDS_REFRESH_SECONDS:
                    _fetch_odds_matches(config.odds_api_key)
                    _LAST_ODDS_FETCH = now
        except Exception:
            pass
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
    with app.app_context():
        context, payload = _render_dashboard(active_tab, refresh_requested=True, render=False)
    payload_hash = _stable_hash(payload)
    now = time.time()
    if not force and _LAST_RENDER_HASH == payload_hash and (now - _LAST_RENDER_TS) < throttle:
        return False
    with app.app_context():
        html = render_template_string(TEMPLATE, **context)
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
        "final": {"value": 0.5, "prob": 0.3, "news": 0.2},
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
    _WEIGHTS_CACHE = defaults
    return defaults


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
        response = _HTTP.get(
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
        response = _HTTP.get(
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
            teams_resp = _HTTP.get(
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
        response = _HTTP.get(
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
        response = _HTTP.get(f"http://api.clubelo.com/{norm}", timeout=15)
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
        response = _HTTP.get(
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
    cache_key = f"{_normalize_team_name(team_name)}:{team_id or ''}"
    cached = _cache_get(_TEAM_SUMMARY_CACHE, cache_key, TEAM_SUMMARY_TTL_SECONDS)
    if isinstance(cached, dict):
        return cached
    team_norm = _normalize_team_name(team_name)
    comp_hint = _TEAM_COMP_HINT.get(team_norm)
    league_code = _fdco_league_code(comp_hint)
    season_year = _season_for_date(datetime.now(timezone.utc).date().isoformat())
    season_code = _fdco_season_code(season_year)
    corners_avg = None
    cards_avg = None
    team_id_af = team_id
    team_id_fd = _team_id_map().get(_normalize_name(team_name))
    if team_id_af is None:
        team_id_af = _api_football_team_id(config.api_football_key, team_name)
    table_entry: dict[str, object] | None = None
    if comp_hint:
        standings = _fetch_standings(comp_hint)
        table_entry = _standings_highlight(standings, team_name)
        if not table_entry:
            table_entry = None
        if team_id_fd is None and table_entry and table_entry.get("team_id"):
            team_id_fd = table_entry.get("team_id")
    if team_id_fd is None and team_id is not None and comp_hint:
        team_id_fd = team_id
    matches = _team_recent_matches_api_football(config.api_football_key, team_id_af, limit=5)
    source = "api-football" if matches else "football-data"
    if not matches:
        matches = _team_recent_matches_fd(config.football_data_token, team_id_fd, limit=5)
    if not matches and league_code:
        matches, corners_avg, cards_avg, table_entry = _fdco_team_summary(team_name, league_code, season_year, limit=5)
        if matches:
            source = "football-data.co.uk"
            total = max(1, len(matches))
            wins = sum(1 for row in matches if row.get("result") == "W")
            goals_for = sum(row.get("gf", 0) for row in matches)
            btts_hits = sum(1 for row in matches if row.get("gf", 0) > 0 and row.get("ga", 0) > 0)
            over25_hits = sum(1 for row in matches if (row.get("gf", 0) + row.get("ga", 0)) >= 3)
            result = {
                "team": team_name,
                "win_rate": wins / total,
                "goals_for_avg": goals_for / total,
                "btts_rate": btts_hits / total,
                "over25_rate": over25_hits / total,
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
    total = len(matches)
    wins = sum(1 for row in matches if row.get("result") == "W")
    goals_for = sum(row.get("gf", 0) for row in matches)
    btts_hits = sum(1 for row in matches if row.get("gf", 0) > 0 and row.get("ga", 0) > 0)
    over25_hits = sum(1 for row in matches if (row.get("gf", 0) + row.get("ga", 0)) >= 3)
    corners_avg = None
    cards_avg = None
    if matches and config.api_football_key and AF_STATS_ENABLED:
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
        "win_rate": wins / total,
        "goals_for_avg": goals_for / total,
        "btts_rate": btts_hits / total,
        "over25_rate": over25_hits / total,
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


def _enrich_pick(pick: dict) -> dict:
    if not pick:
        return pick
    comp_hint = pick.get("fd_code") or pick.get("comp_code") or _sport_key_to_comp(pick.get("sport_key", ""))
    if comp_hint and pick.get("home_team") and pick.get("away_team"):
        _TEAM_COMP_HINT[_normalize_team_name(pick.get("home_team", ""))] = str(comp_hint)
        _TEAM_COMP_HINT[_normalize_team_name(pick.get("away_team", ""))] = str(comp_hint)
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
        comp_code = pick.get("fd_code") or _sport_key_to_comp(pick.get("sport_key", ""))
        standings = _fetch_standings(comp_code)
    pick["standings"] = standings
    pick["home_standing"] = _standings_highlight(standings, pick.get("home_team", ""), pick.get("home_id"))
    pick["away_standing"] = _standings_highlight(standings, pick.get("away_team", ""), pick.get("away_id"))
    pick["weather"] = _weather_details(pick.get("home_team", ""))
    return _ensure_pick_fields(pick)


def _ensure_pick_fields(pick: dict | None) -> dict | None:
    if not pick:
        return None
    pick.setdefault("value", 0.0)
    pick.setdefault("model_prob", 0.0)
    pick.setdefault("implied_prob", 0.0)
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
        "team": "Frissítés alatt",
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
        "home_team": "Frissítés alatt",
        "away_team": "Adatgyűjtés folyamatban",
        "competition": "Frissítés",
        "market_key": "h2h",
        "market_label": "Általános",
        "outcome": "Javaslat készül",
        "line": 0.0,
        "story": "Adatok feltöltése folyamatban, kérjük várjon.",
        "weather": {},
        "home_summary": _placeholder_summary(),
        "away_summary": _placeholder_summary(),
        "home_standing": _placeholder_standing(),
        "away_standing": _placeholder_standing(),
    }
    return _ensure_pick_fields(pick) or pick


def _enforce_tip_presence(best_pick: dict | None, target_matches: list[dict]) -> tuple[dict, list[dict]]:
    normalized_targets: list[dict] = []
    for item in target_matches:
        ensured = _ensure_pick_fields(item)
        if ensured:
            normalized_targets.append(ensured)
    best_pick = _ensure_pick_fields(best_pick)
    if normalized_targets and not best_pick:
        best_pick = normalized_targets[0]
    if not normalized_targets:
        placeholder = _placeholder_pick()
        normalized_targets = [placeholder]
        if not best_pick:
            best_pick = placeholder
    if not best_pick:
        best_pick = _placeholder_pick()
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
    sources = _load_rss_sources()
    if team_names:
        sources.extend(_team_rss_sources(team_names))
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
            response = _HTTP.get(url, timeout=8)
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
        response = _HTTP.get(
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
        items = _news_items_for_match(home, away, rss_items, limit=limit)
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


def _news_items_for_match(home: str, away: str, rss_items: list[dict], limit: int = 10) -> list[dict]:
    if not rss_items:
        return []
    home_norm = _normalize_team_name(home)
    away_norm = _normalize_team_name(away)
    stopwords = {"afc", "fc", "cf", "sc", "ac", "cd", "ud", "rc", "bc", "cc", "if"}
    raw_tokens = [t.lower() for t in re.split(r"\W+", f"{home} {away}") if len(t) > 2]
    tokens = {t for t in raw_tokens if t not in stopwords}
    team_ids = _team_id_map()
    home_id = team_ids.get(_normalize_name(home))
    away_id = team_ids.get(_normalize_name(away))
    player_tokens = _team_squad_tokens(home_id) | _team_squad_tokens(away_id)
    tokens |= {t for t in player_tokens if t and len(t) > 3}
    if not tokens:
        return []
    matched = []
    seen = set()
    for item in rss_items:
        title = (item.get("title") or "").lower()
        summary = (item.get("summary") or "").lower()
        text = f"{title} {summary}".strip()
        if not text:
            continue
        text_tokens = {t for t in re.split(r"\W+", text) if len(t) > 2}
        if tokens.intersection(text_tokens):
            key = (item.get("title"), item.get("source"))
            if key in seen:
                continue
            seen.add(key)
            matched.append(item)
        if len(matched) >= limit:
            break
    return matched


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
        response = _HTTP.get(
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
        response = _HTTP.get(
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
        response = _HTTP.get(
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
    competition = _match_competition(match)
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

    sample_home_xg, sample_away_xg = _sample_match_xg(match)
    if sample_home_xg is not None and sample_away_xg is not None:
        xg_diff_value = sample_home_xg - sample_away_xg
    else:
        xg_diff_value = exp_home - exp_away
    xg_diff = max(-1.0, min(1.0, xg_diff_value / 2.5))
    lineup_home = _lineup_strength_from_data(home_team)
    lineup_away = _lineup_strength_from_data(away_team)
    lineup_diff = max(-1.0, min(1.0, lineup_home - lineup_away))
    injury_data_home = _team_injury_index_data(home_team)
    injury_data_away = _team_injury_index_data(away_team)
    data_injury_diff = max(-1.0, min(1.0, injury_data_home - injury_data_away))
    weights = _load_weights()
    logistic_kwargs = {
        "table_diff": table_diff,
        "xg_diff": xg_diff,
        "lineup_diff": lineup_diff,
        "injury_diff": data_injury_diff,
        "weather_risk": weather_factor,
        "news_sentiment_diff": news_score,
        "weights": weights,
    }

    outcome_name = str(outcome.get("name") or "")
    point_val = outcome.get("point")
    point = float(point_val) if isinstance(point_val, (int, float)) else None
    description = str(outcome.get("description") or "")

    model_prob = 0.0
    selection_label = outcome_name
    elo_strength = 0.5
    form_strength = 0.5
    table_strength = 0.5
    injury_index = max(
        (injury_home + injury_away) / 2.0,
        (injury_data_home + injury_data_away) / 2.0,
    )

    if market_key == "h2h":
        p_home, p_draw, p_away = _model_probs(elo_diff, form_diff, **logistic_kwargs)
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
        p_home, p_draw, p_away = _model_probs(elo_diff, form_diff, **logistic_kwargs)
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
        p_home, _, p_away = _model_probs(elo_diff, form_diff, **logistic_kwargs)
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
    value = model_prob - implied_prob
    final_weights = weights.get("final", {})
    value_weight = float(final_weights.get("value", 0.5))
    prob_weight = float(final_weights.get("prob", 0.3))
    news_weight = float(final_weights.get("news", 0.2))
    total = (
        value_weight * value
        + prob_weight * model_prob
        + news_weight * news_score
    )
    market_diff = max(0.0, value)
    reason_parts = []
    if elo_strength >= 0.65:
        reason_parts.append("eros csapateroseg")
    if form_strength >= 0.6:
        reason_parts.append("jobb forma")
    if xg_diff >= 0.2:
        reason_parts.append("xG elony")
    elif xg_diff <= -0.2:
        reason_parts.append("xG deficit")
    if lineup_diff >= 0.25:
        reason_parts.append("hazai kezdoero elony")
    elif lineup_diff <= -0.25:
        reason_parts.append("vendeg kezdoero elony")
    if data_injury_diff >= 0.25:
        reason_parts.append("kevesebb hazai serules")
    elif data_injury_diff <= -0.25:
        reason_parts.append("vendeg serules kockazat")
    if market_diff >= 0.02:
        reason_parts.append("ertek az oddsban")
    if weather_factor >= 0.4:
        reason_parts.append("idojaras kockazat")
    if news_score >= 0.05:
        reason_parts.append("pozitiv hirek")
    elif news_score <= -0.05:
        reason_parts.append("negativ hirek")
    if not reason_parts:
        reason_parts.append("kiegyensulyozott jelek")
    reason_text = "Magyarazat: " + ", ".join(reason_parts) + "."

    return {
        "match_key": _match_key(match),
        "home_team": home_team,
        "away_team": away_team,
        "competition": competition,
        "fd_code": match.get("fd_code"),
        "home_id": match.get("home_id"),
        "away_id": match.get("away_id"),
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
        "injury_data_diff": data_injury_diff,
        "lineup_diff": lineup_diff,
        "lineup_strength_home": lineup_home,
        "lineup_strength_away": lineup_away,
        "weather_factor": weather_factor,
        "news_score": news_score,
        "value": value,
        "model_prob": model_prob,
        "implied_prob": implied_prob,
        "xg_diff": xg_diff,
        "explain_hu": reason_text,
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
    home_team = match.get("home_team", "")
    away_team = match.get("away_team", "")
    comp_code = _sport_key_to_comp(str(match.get("sport_key") or ""))
    if comp_code and home_team and away_team:
        _TEAM_COMP_HINT[_normalize_team_name(home_team)] = str(comp_code)
        _TEAM_COMP_HINT[_normalize_team_name(away_team)] = str(comp_code)
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
    cache_key = f"{comp_code}:{limit}"
    cached = _cache_get(_FD_RECENT_CACHE, cache_key, FD_RECENT_TTL_SECONDS)
    if isinstance(cached, list):
        return cached
    try:
        response = _HTTP.get(
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
        response = _HTTP.get(
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
    _, date_from, date_to = _fixture_window(hours)
    return {
        "comp_24": comps_count,
        "all_24": all_count,
        "api_football_24": api_football_count,
        "window_from": date_from,
        "window_to": date_to,
        "window_source": _LAST_WINDOW_INFO.get("source", "system"),
        "api_football_enabled": bool(config.api_football_key),
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
            response = _HTTP.get(
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
        response = _HTTP.get(
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
        response = _HTTP.get(
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
        response = _HTTP.get(
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
        response = _HTTP.get(
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
        response = _HTTP.get(
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
            response = _HTTP.get(
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
        response = _HTTP.get(
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
        response = _HTTP.get(
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
        response = _HTTP.get(url, timeout=12)
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
        if _normalize_team_name(home) != team_norm and _normalize_team_name(away) != team_norm:
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
        is_home = _normalize_team_name(home) == team_norm
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
    table_entry = _fdco_table_data(league_code, season_code).get(team_norm)
    return matches, corners_avg, cards_avg, table_entry


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
    now = datetime.now(BUDAPEST_TZ)
    filtered: list[dict] = []
    for match in fixtures:
        match_dt = _parse_match_dt(match)
        if not match_dt:
            continue
        match_local = match_dt.astimezone(BUDAPEST_TZ)
        if _within_next_24h(match_local, now):
            filtered.append(match)
    if not filtered:
        return []
    fixtures = filtered
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
        if comp_code:
            _TEAM_COMP_HINT[_normalize_team_name(home_team)] = str(comp_code)
            _TEAM_COMP_HINT[_normalize_team_name(away_team)] = str(comp_code)
        if _is_rivalry(home_team, away_team):
            continue
        match_news_items = _news_items_for_match(home_team, away_team, news_items, limit=8)
        news_count = len(match_news_items)
        elo_home = _fast_clubelo(home_team)
        elo_away = _fast_clubelo(away_team)
        elo_diff = max(-1.0, min(1.0, (elo_home - elo_away) / 400.0))
        form_home_ppg = 1.5
        form_away_ppg = 1.5
        form_diff = 0.0
        table_home = table_scores.get(home_team, 0.5)
        table_away = table_scores.get(away_team, 0.5)
        table_diff = max(-1.0, min(1.0, table_home - table_away))

        home_btts, home_over25, home_form = _local_team_rates(home_team)
        away_btts, away_over25, away_form = _local_team_rates(away_team)
        btts_rate = (home_btts + away_btts) / 2.0
        over25_rate = (home_over25 + away_over25) / 2.0
        home_form = str(home_form or "")
        away_form = str(away_form or "")
        home_row = _standings_highlight(comp_standings, home_team, match.get("home_id"))
        away_row = _standings_highlight(comp_standings, away_team, match.get("away_id"))
        home_pos = home_row.get("position") if home_row else None
        away_pos = away_row.get("position") if away_row else None

        p_home, p_draw, p_away = _model_probs(elo_diff, form_diff)
        market_key = "h2h"
        market_label = "1X2 (odds nelkul)"
        if btts_rate >= 0.6:
            selection_label = "Mindket csapat golt szerez"
            model_prob = btts_rate
            market_key = "btts"
            market_label = "GG (odds nelkul)"
            elo_strength = 0.5
            form_strength = 0.5
            table_strength = 0.5
            injury_index = (_injury_index(match_news_items, home_team) + _injury_index(match_news_items, away_team)) / 2.0
        elif over25_rate >= 0.6:
            selection_label = "Over 2.5 gol"
            model_prob = over25_rate
            market_key = "totals"
            market_label = "Over 2.5 (odds nelkul)"
            elo_strength = 0.5
            form_strength = 0.5
            table_strength = 0.5
            injury_index = (_injury_index(match_news_items, home_team) + _injury_index(match_news_items, away_team)) / 2.0
        elif p_home >= p_draw and p_home >= p_away:
            selection_label = "Hazai gyozelem"
            model_prob = p_home
            elo_strength = (elo_diff + 1.0) / 2.0
            form_strength = (form_diff + 1.0) / 2.0
            table_strength = (table_diff + 1.0) / 2.0
            injury_index = _injury_index(match_news_items, home_team)
        elif p_away >= p_home and p_away >= p_draw:
            selection_label = "Vendeg gyozelem"
            model_prob = p_away
            elo_strength = ((-elo_diff) + 1.0) / 2.0
            form_strength = ((-form_diff) + 1.0) / 2.0
            table_strength = ((-table_diff) + 1.0) / 2.0
            injury_index = _injury_index(match_news_items, away_team)
        else:
            selection_label = "Donto"
            model_prob = p_draw
            elo_strength = 0.5
            form_strength = 0.5
            table_strength = 0.5
            injury_index = (_injury_index(match_news_items, home_team) + _injury_index(match_news_items, away_team)) / 2.0

        weather_factor = min(1.0, max(0.0, abs(_weather_factor(home_team)) * 10.0))
        news_score = _news_factor(match_news_items, home_team, away_team)
        form_strength = 0.7 * form_strength + 0.3 * table_strength
        base = 0.35
        total = (
            base
            + elo_strength * weights["elo"]
            + form_strength * weights["form"]
            + table_strength * weights["table"]
            + (1.0 - injury_index) * weights["injury"]
            + (1.0 - weather_factor) * weights["weather"]
            + max(0.0, news_score) * weights["news"]
        )
        total = max(0.0, min(1.0, total))

        reason_parts = []
        if elo_strength >= 0.65:
            reason_parts.append("eros csapateroseg")
        if form_strength >= 0.6:
            reason_parts.append("jobb forma")
        if weather_factor >= 0.4:
            reason_parts.append("idojaras kockazat")
        if news_score >= 0.05:
            reason_parts.append("pozitiv hirek")
        if btts_rate >= 0.6:
            reason_parts.append("gyakori GG a friss meccseken")
        if over25_rate >= 0.6:
            reason_parts.append("sok gol a friss meccseken")
        if not reason_parts:
            reason_parts.append("kiegyensulyozott jelek")
        detail_parts = []
        if home_form or away_form:
            detail_parts.append(f"forma {home_form or 'n/a'} / {away_form or 'n/a'}")
        if home_pos or away_pos:
            detail_parts.append(f"tabella {home_pos or 'n/a'} vs {away_pos or 'n/a'}")
        if btts_rate > 0:
            detail_parts.append(f"GG {btts_rate:.0%}")
        if over25_rate > 0:
            detail_parts.append(f"Over2.5 {over25_rate:.0%}")
        if news_count:
            detail_parts.append(f"hirek {news_count} relevans")
        reason_text = "Magyarazat: " + ", ".join(reason_parts) + "."
        if detail_parts:
            reason_text += " " + "; ".join(detail_parts) + "."

        pick = {
            "match_key": _match_key(match),
            "home_team": home_team,
            "away_team": away_team,
            "competition": _match_competition(match),
            "fd_code": match.get("fd_code"),
            "home_id": match.get("home_id"),
            "away_id": match.get("away_id"),
            "season": match.get("season"),
            "sport_key": match.get("sport_key", ""),
            "commence_time": match.get("commence_time", ""),
            "market_key": market_key,
            "market_label": market_label,
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
            "risk": _risk_label(total),
            "explain_hu": reason_text,
        }
    picks.append(pick)
    picks.sort(key=lambda item: item["score"], reverse=True)
    limit = int(os.environ.get("STAT_ONLY_PICK_LIMIT", "2"))
    if limit > 0 and len(picks) < limit:
        existing_keys = {item.get("match_key") for item in picks}
        for match in fixtures:
            key = _match_key(match)
            if key in existing_keys:
                continue
            home_team = match.get("home_team", "")
            away_team = match.get("away_team", "")
            if not home_team or not away_team:
                continue
            fallback_pick = {
                "match_key": key,
                "home_team": home_team,
                "away_team": away_team,
                "competition": _match_competition(match),
                "fd_code": match.get("fd_code"),
                "home_id": match.get("home_id"),
                "away_id": match.get("away_id"),
                "season": match.get("season"),
                "sport_key": match.get("sport_key", ""),
                "commence_time": match.get("commence_time", ""),
                "market_key": "h2h",
                "market_label": "1X2 (odds nelkul)",
                "outcome": "Alapajanlas",
                "line": None,
                "odds": None,
                "distance": 0.0,
                "score": 0.2,
                "elo_strength": 0.5,
                "form_strength": 0.5,
                "market_diff": 0.0,
                "injury_index": 0.0,
                "weather_factor": 0.0,
                "news_score": 0.0,
                "model_prob": 0.5,
                "implied_prob": None,
                "risk": "red",
                "explain_hu": "Magyarazat: keves adat, alapajanlas.",
            }
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
    fixtures = _dedupe_fixtures(fixtures)
    now = datetime.now(BUDAPEST_TZ)
    fixtures = [
        item
        for item in fixtures
        if (dt := _parse_match_dt(item)) and _within_next_24h(dt.astimezone(BUDAPEST_TZ), now)
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
            # If we cannot match the competition to known list, skip.
        fixtures = filtered
    if allowed_codes:
        comp_codes: list[str] = []
        for item in fixtures:
            code = str(item.get("fd_code") or item.get("comp_code") or "")
            if code and code in allowed_codes and code not in comp_codes:
                comp_codes.append(code)
        standings_limit = int(os.environ.get("FD_STANDINGS_LIMIT", "6"))
        for code in comp_codes[:standings_limit]:
            standings_by_comp[code] = _fetch_standings_fd(config.football_data_token, code)
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
    if _backoff_active("football-data"):
        return []
    try:
        response = _HTTP.get(
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
        response = _HTTP.get(
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


def _persist_pick_snapshot(
    db,
    odds_data: dict | None,
    best_pick: dict | None,
    best_combo: dict | None,
    target_matches: list[dict],
    odds_count: int,
    odds_error: str | None,
    rss_items: list[dict],
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
        response = _HTTP.get(
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
            response = _HTTP.get(
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
                fallback = _HTTP.get(
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
    t0 = time.perf_counter()
    try:
        if request.args.get("refresh") == "1":
            _render_and_cache(force=True)
        elif not _RESPONSE_CACHE.get("html"):
            _trigger_refresh_async()
        with _RESPONSE_LOCK:
            html = _RESPONSE_CACHE.get("html", "")
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


def _render_dashboard(active_tab: str, refresh_requested: bool, render: bool = True):
    target_odds = 2.0
    window_hours = 24

    db = connect(config.db_url)
    db.ensure_schema()
    cached = _load_cached_picks(db)
    cached_updated_at = cached.get("updated_at") if cached else None
    if not refresh_requested:
        if not cached:
            refresh_requested = True
        elif AUTO_REFRESH_SECONDS > 0 and cached_updated_at:
            cached_dt = _parse_iso_datetime(cached_updated_at)
            if cached_dt:
                age = (datetime.now(timezone.utc) - cached_dt).total_seconds()
                if age >= AUTO_REFRESH_SECONDS:
                    refresh_requested = True
                if cached_dt < _SERVER_STARTED_AT:
                    refresh_requested = True
        elif cached_updated_at:
            cached_dt = _parse_iso_datetime(cached_updated_at)
            if cached_dt and cached_dt < _SERVER_STARTED_AT:
                refresh_requested = True
    if refresh_requested and cached_updated_at and REFRESH_COOLDOWN_SECONDS > 0:
        cached_dt = _parse_iso_datetime(cached_updated_at)
        if cached_dt:
            age = (datetime.now(timezone.utc) - cached_dt).total_seconds()
            if age < REFRESH_COOLDOWN_SECONDS:
                refresh_requested = False
    if refresh_requested and cached_updated_at and MIN_API_REFRESH_SECONDS > 0:
        cached_dt = _parse_iso_datetime(cached_updated_at)
        if cached_dt:
            age = (datetime.now(timezone.utc) - cached_dt).total_seconds()
            if age < MIN_API_REFRESH_SECONDS:
                refresh_requested = False
    matches = list_matches(db)
    points_map = table(matches)
    form_scores = _build_form_scores(matches)
    table_scores = _build_table_scores(points_map)

    competitions = []
    recent_matches = []
    standings = []
    if not FAST_MODE:
        competitions = _fetch_competitions_fd(config.football_data_token)
    allowed_codes = {str(comp.get("code") or "") for comp in competitions if comp.get("code")}
    primary = _primary_competition(competitions)
    if primary:
        recent_matches = _fetch_recent_matches_fd(config.football_data_token, primary)
        standings = _fetch_standings_fd(config.football_data_token, primary)

    odds_data = None
    target_matches = []
    rss_items: list[dict] = []
    news_blocks: list[dict] = []
    odds_count = 0
    odds_error = None
    best_pick = None
    best_combo = None
    diag_counts = {"comp_24": 0, "all_24": 0, "api_football_24": 0, "window_from": "", "window_to": "", "window_source": "system"}

    if refresh_requested:
        cached_updated_at = None
        try:
            diag_counts = _diagnostics_fixtures(config.football_data_token, competitions, window_hours)
            if not config.odds_api_key:
                odds_error = "Odds API kulcs hianyzik (odds nelkuli ajanlas)"
                data = []
                eligible = []
                fixtures, standings_by_comp = _fetch_public_fixtures(competitions, window_hours)
                rss_team_names = []
                for item in fixtures:
                    home = item.get("home_team")
                    away = item.get("away_team")
                    if home:
                        rss_team_names.append(str(home))
                    if away:
                        rss_team_names.append(str(away))
                rss_items = _fetch_rss_items(rss_team_names)
                picks = _build_stat_only_picks(fixtures, standings_by_comp, rss_items)
                if not picks:
                    fallback_hours = 24
                    fixtures, standings_by_comp = _fetch_public_fixtures(competitions, fallback_hours)
                    rss_team_names = []
                    for item in fixtures:
                        home = item.get("home_team")
                        away = item.get("away_team")
                        if home:
                            rss_team_names.append(str(home))
                        if away:
                            rss_team_names.append(str(away))
                    rss_items = _fetch_rss_items(rss_team_names)
                    picks = _build_stat_only_picks(fixtures, standings_by_comp, rss_items)
                    if picks:
                        odds_error = "Odds API kulcs hianyzik (odds nelkuli ajanlas, 24 oras ablak)"
                if not picks:
                    odds_error = "Nincs elerheto meccs 24 oras ablakban"
                odds_count = len(picks)
                best_pick = _enrich_pick(picks[0]) if picks else None
                best_combo = None
                target_matches = [_enrich_pick(item) for item in picks[:2]] if picks else []
                best_pick, target_matches = _persist_pick_snapshot(
                    db,
                    odds_data,
                    best_pick,
                    best_combo,
                    target_matches,
                    odds_count,
                    odds_error,
                    rss_items,
                )
            else:
                data, odds_error = _fetch_odds_matches(config.odds_api_key)
                eligible = []
                use_odds = bool(data) and not odds_error
                if not use_odds:
                    if not odds_error:
                        odds_error = "Odds API adat nem elerheto (odds nelkuli ajanlas)"
                    fixtures, standings_by_comp = _fetch_public_fixtures(competitions, window_hours)
                    rss_team_names = []
                    for item in fixtures:
                        home = item.get("home_team")
                        away = item.get("away_team")
                        if home:
                            rss_team_names.append(str(home))
                        if away:
                            rss_team_names.append(str(away))
                    rss_items = _fetch_rss_items(rss_team_names)
                    picks = _build_stat_only_picks(fixtures, standings_by_comp, rss_items)
                    if not picks:
                        fallback_hours = 24
                        fixtures, standings_by_comp = _fetch_public_fixtures(competitions, fallback_hours)
                        rss_team_names = []
                        for item in fixtures:
                            home = item.get("home_team")
                            away = item.get("away_team")
                            if home:
                                rss_team_names.append(str(home))
                            if away:
                                rss_team_names.append(str(away))
                        rss_items = _fetch_rss_items(rss_team_names)
                        picks = _build_stat_only_picks(fixtures, standings_by_comp, rss_items)
                        if picks:
                            odds_error = "Odds API adat nem elerheto (24 oras ablak)"
                    if not picks:
                        odds_error = "Nincs elerheto meccs 24 oras ablakban"
                    odds_count = len(picks)
                    best_pick = _enrich_pick(picks[0]) if picks else None
                    best_combo = None
                    target_matches = [_enrich_pick(item) for item in picks[:2]] if picks else []
                    best_pick, target_matches = _persist_pick_snapshot(
                        db,
                        odds_data,
                        best_pick,
                        best_combo,
                        target_matches,
                        odds_count,
                        odds_error,
                        rss_items,
                    )
                if use_odds and data:
                    for match in data:
                        home_team = match.get("home_team", "")
                        away_team = match.get("away_team", "")
                        if _is_friendly_comp(code=str(match.get("sport_key") or ""), sport_title=str(match.get("sport_title") or "")):
                            continue
                        comp_code = _sport_key_to_comp(str(match.get("sport_key") or ""))
                        if allowed_codes and (not comp_code or comp_code not in allowed_codes):
                            continue
                        if not _within_hours(match, window_hours):
                            continue
                        if _is_rivalry(home_team, away_team):
                            continue
                        eligible.append(match)
            if eligible:
                odds_count = len(eligible)
                rss_team_names = []
                for item in eligible:
                    home = item.get("home_team")
                    away = item.get("away_team")
                    if home:
                        rss_team_names.append(str(home))
                    if away:
                        rss_team_names.append(str(away))
                rss_items = _fetch_rss_items(rss_team_names)
                picks: list[dict] = []
                for match in eligible:
                    match_dt = _parse_match_dt(match)
                    if not match_dt:
                        continue
                    match_local = match_dt.astimezone(BUDAPEST_TZ)
                    if not _within_next_24h(match_local, datetime.now(BUDAPEST_TZ)):
                        continue
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
        finally:
            best_pick, target_matches = _enforce_tip_presence(best_pick, target_matches)
        _settle_saved_picks(db, config.odds_api_key)
    else:
        if cached:
            odds_data = cached.get("odds_data")
            best_combo = cached.get("best_combo")
            raw_target_matches = cached.get("target_matches", [])
            odds_count = cached.get("odds_count", 0)
            odds_error = cached.get("odds_error")
            cached_updated_at = cached.get("updated_at")
            best_pick, target_matches = _enforce_tip_presence(cached.get("best_pick"), raw_target_matches)
            best_pick = _enrich_pick(best_pick) if best_pick else None
            target_matches = [_enrich_pick(item) for item in target_matches] if target_matches else []
    saved_picks = _list_saved_picks(db)
    stake_pct = _stake_from_score(best_pick.get("score") if best_pick else None)

    configured_sources = ", ".join(sorted({item.get("label", "") for item in _load_rss_sources() if item.get("label")}))
    rss_sources = configured_sources
    if cached_updated_at and not rss_sources:
        rss_sources = cached.get("rss_sources", "") if cached else ""
    news_blocks = _news_blocks(target_matches or ([best_pick] if best_pick else []), rss_items)
    context = {
        "odds_configured": bool(config.odds_api_key),
        "football_configured": bool(config.football_data_token),
        "competitions": competitions,
        "odds": odds_data,
        "best_pick": best_pick,
        "best_combo": best_combo,
        "odds_count": odds_count,
        "target_matches": target_matches,
        "recent_matches": recent_matches,
        "standings": standings,
        "_match_reasons": _match_reasons,
        "rss_items": rss_items,
        "rss_sources": rss_sources,
        "configured_sources": configured_sources,
        "news_blocks": news_blocks,
        "saved_picks": saved_picks,
        "stake_pct": stake_pct,
        "diag_counts": diag_counts,
        "cached_updated_at": cached_updated_at,
        "odds_error": odds_error,
        "active_tab": active_tab,
        "refresh_requested": refresh_requested,
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
    return render_template_string(TEMPLATE, **context)


_start_background_refresh()


@app.route("/health")
def health():
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "fast_mode": FAST_MODE,
    }


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

