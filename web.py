from flask import Flask, render_template_string, request
from soccer_bot.config import load_config
from soccer_bot.db import connect
from soccer_bot.repo import Match, add_match, get_team_stats, list_matches
from soccer_bot.scoring import table
import html
import json
import os
import re
import time
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
_TEAM_LOCATION_OVERRIDES: dict[str, dict] | None = None
_SPORTS_CACHE: dict[str, object] = {"fetched_at": 0.0, "keys": []}
SPORTS_CACHE_TTL_SECONDS = 3600


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
            items.append({"title": title, "summary": description})
        if items:
            return items
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", ns):
            title = _strip_html(entry.findtext("atom:title", default="", namespaces=ns))
            summary = _strip_html(entry.findtext("atom:summary", default="", namespaces=ns))
            items.append({"title": title, "summary": summary})
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
        "odds_distance": 0.5,
        "implied_prob": 0.2,
        "news": 0.2,
        "weather": 0.05,
        "stats": 0.05,
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
    match: dict, target: float, news_items: list[dict], db
) -> dict | None:
    outcomes = _extract_h2h_outcomes(match)
    best = _best_outcome(outcomes, target)
    if not best:
        return None
    home_team = match.get("home_team", "")
    away_team = match.get("away_team", "")
    implied_prob = 1 / best["price"]
    odds_score = max(0.0, 1.0 - best["distance"] / target)
    news_score = _news_factor(news_items, home_team, away_team)
    weather_score = _weather_factor(home_team)
    stats = get_team_stats(db, home_team)
    stats_factor = stats["win_rate"] * 0.1
    weights = _load_weights()
    total = (
        odds_score * weights["odds_distance"]
        + implied_prob * weights["implied_prob"]
        + news_score * weights["news"]
        + weather_score * weights["weather"]
        + stats_factor * weights["stats"]
    )
    return {
        "home_team": home_team,
        "away_team": away_team,
        "outcome": best["name"],
        "odds": best["price"],
        "distance": best["distance"],
        "score": total,
    }
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
    matches: list[dict], target: float, news_items: list[dict], db, limit: int = 2
) -> list[dict]:
    candidates = []
    for match in matches:
        scored = _score_match(match, target, news_items, db)
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
    <title>Soccer Bot - Professional AI Tips Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        .hero { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 60px 0; }
        .card { border: none; box-shadow: 0 4px 8px rgba(0,0,0,0.1); transition: transform 0.2s; }
        .card:hover { transform: translateY(-5px); }
        .tip-highlight { background-color: #e8f5e8; border-left: 5px solid #28a745; }
        .odds-table th { background-color: #f8f9fa; }
        .badge { font-size: 0.9em; }
        .ai-tip { font-size: 1.2em; font-weight: bold; }
    </style>
</head>
<body>
    <div class="hero text-center">
        <div class="container">
            <h1 class="display-4">Soccer Bot AI Dashboard</h1>
            <p class="lead">Advanced Football Predictions & Analytics</p>
        </div>
    </div>

    <div class="container my-5">
        <div class="row">
            <div class="col-md-6">
                <div class="card mb-4">
                    <div class="card-header bg-primary text-white">
                        <h5 class="mb-0">API Status</h5>
                    </div>
                    <div class="card-body">
                        <p><strong>Odds API:</strong> {{ "Configured" if odds_configured else "Missing" }}</p>
                        <p><strong>Football Data:</strong> {{ "Configured" if football_configured else "Missing" }}</p>
                        <p><strong>Weather:</strong> Open-Meteo (no key)</p>
                        <p><strong>News:</strong> RSS feeds (no key)</p>
                    </div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="card mb-4">
                    <div class="card-header bg-success text-white">
                        <h5 class="mb-0">Active Competitions</h5>
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
                        <h5 class="mb-0">Live Match Odds & AI Prediction</h5>
                    </div>
                    <div class="card-body">
                        {% if odds %}
                        <h6 class="text-center mb-3">{{ odds.home_team }} vs {{ odds.away_team }}</h6>
                        <table class="table table-striped odds-table text-center">
                            <thead>
                                <tr>
                                    <th>Home Win</th>
                                    <th>Draw</th>
                                    <th>Away Win</th>
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
                            <h5 class="ai-tip">AI Recommendation: {{ odds.tip }}</h5>
                        </div>
                        <canvas id="oddsChart" width="400" height="200"></canvas>
                        {% else %}
                        <p class="text-muted text-center">No live odds available at the moment.</p>
                        {% endif %}
                    </div>
                </div>
            </div>
        </div>

        <div class="row">
            <div class="col-12">
                <div class="card mb-4">
                    <div class="card-header bg-secondary text-white">
                        <h5 class="mb-0">Two Matches Near 2.00 Odds</h5>
                    </div>
                    <div class="card-body">
                        <form method="get" class="text-center mb-3">
                            <input type="hidden" name="tips" value="1">
                            <button class="btn btn-success" type="submit">Show 2 matches near 2.00</button>
                        </form>
                        {% if tips_requested %}
                            {% if target_matches %}
                                <table class="table table-striped text-center">
                                    <thead>
                                        <tr>
                                            <th>Match</th>
                                            <th>Outcome</th>
                                            <th>Odds</th>
                                            <th>Score</th>
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
                                <p class="text-muted text-center">No matches found near 2.00 odds.</p>
                            {% endif %}
                        {% endif %}
                    </div>
                </div>
            </div>
        </div>

        <div class="row">
            <div class="col-md-6">
                <div class="card mb-4">
                    <div class="card-header bg-info text-white">
                        <h5 class="mb-0">Recent Matches</h5>
                    </div>
                    <div class="card-body">
                        <table class="table table-hover">
                            <thead>
                                <tr>
                                    <th>Home</th>
                                    <th>Away</th>
                                    <th>Score</th>
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
                        <h5 class="mb-0">League Standings</h5>
                    </div>
                    <div class="card-body">
                        <table class="table table-hover">
                            <thead>
                                <tr>
                                    <th>Pos</th>
                                    <th>Team</th>
                                    <th>Pts</th>
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
                    labels: ['Home Win', 'Draw', 'Away Win'],
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

    db = connect(config.db_url)
    db.ensure_schema()
    matches = list_matches(db)
    if not matches:
        sample_matches = [
            Match("Team A", "Team B", 2, 1, "2023-01-01"),
            Match("Team A", "Team C", 1, 1, "2023-01-02"),
            Match("Team B", "Team C", 0, 3, "2023-01-03"),
        ]
        for match in sample_matches:
            try:
                add_match(db, match)
            except Exception:
                pass  # Already exists
        matches = list_matches(db)
    points_table = sorted(table(matches).items(), key=lambda x: x[1], reverse=True)

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
    try:
        if tips_requested:
            data = _fetch_odds_matches(config.odds_api_key)
        else:
            data = _fetch_odds_matches(config.odds_api_key, keys=["soccer_epl"])
        if data:
            rss_items = _fetch_rss_items()
            scored_candidates = []
            for match in data:
                scored = _score_match(match, target_odds, rss_items, db)
                if scored:
                    scored_candidates.append((scored["score"], match))
            match = max(scored_candidates, key=lambda item: item[0])[1] if scored_candidates else data[0]
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
                        "Home win (AI: prob "
                        f"{home_prob:.2f}, stats {stats_factor:.2f}, "
                        f"weather {weather_factor:.2f}, news {news_factor:.2f})"
                    )
                elif away_final > home_final and away_final > draw_final:
                    tip = (
                        "Away win (AI: prob "
                        f"{away_prob:.2f}, stats {stats_factor:.2f}, "
                        f"weather {weather_factor:.2f}, news {news_factor:.2f})"
                    )
                else:
                    tip = (
                        "Draw (AI: prob "
                        f"{draw_prob:.2f}, stats {stats_factor:.2f}, "
                        f"weather {weather_factor:.2f}, news {news_factor:.2f})"
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
                    data, target_odds, rss_items, db, limit=2
                )
    except Exception:
        pass

    return render_template_string(TEMPLATE,
                                  odds_configured=bool(config.odds_api_key),
                                  football_configured=bool(config.football_data_token),
                                  competitions=competitions,
                                  odds=odds_data,
                                  tips_requested=tips_requested,
                                  target_matches=target_matches,
                                  matches=matches,
                                  points_table=points_table)

if __name__ == '__main__':
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug)
