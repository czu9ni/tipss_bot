from flask import Flask, render_template_string
from soccer_bot.config import load_config
from soccer_bot.db import connect
from soccer_bot.repo import Match, add_match, list_matches
from soccer_bot.scoring import table
import requests

app = Flask(__name__)

config = load_config()

# HTML template
TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Soccer Bot - Tips</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        .section { margin-bottom: 30px; }
        .tip { font-weight: bold; color: green; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background-color: #f2f2f2; }
    </style>
</head>
<body>
    <h1>Soccer Bot Dashboard</h1>

    <div class="section">
        <h2>API Keys Loaded</h2>
        <p>ODDS_API_KEY: {{ odds_key[:4] }}...</p>
        <p>FOOTBALL_DATA_TOKEN: {{ football_key[:4] }}...</p>
        <p>WEATHER_API_KEY: {{ weather_key[:4] }}...</p>
        <p>NEWS_API_KEY: {{ news_key[:4] }}...</p>
    </div>

    <div class="section">
        <h2>Competitions (Football Data API)</h2>
        <ul>
        {% for comp in competitions %}
            <li>{{ comp.name }} ({{ comp.code }})</li>
        {% endfor %}
        </ul>
    </div>

    <div class="section">
        <h2>Latest Odds & Tip</h2>
        {% if odds %}
        <p>Match: {{ odds.home_team }} vs {{ odds.away_team }}</p>
        <p>Odds: Home {{ odds.home_odds }}, Draw {{ odds.draw_odds }}, Away {{ odds.away_odds }}</p>
        <p class="tip">Tip: {{ odds.tip }}</p>
        {% else %}
        <p>No odds available.</p>
        {% endif %}
    </div>

    <div class="section">
        <h2>Sample Matches & Table</h2>
        <table>
            <tr><th>Home</th><th>Away</th><th>Score</th></tr>
            {% for match in matches %}
            <tr><td>{{ match.home_team }}</td><td>{{ match.away_team }}</td><td>{{ match.home_score }}-{{ match.away_score }}</td></tr>
            {% endfor %}
        </table>
        <h3>Points Table</h3>
        <table>
            <tr><th>Team</th><th>Points</th></tr>
            {% for team, points in points_table %}
            <tr><td>{{ team }}</td><td>{{ points }}</td></tr>
            {% endfor %}
        </table>
    </div>
</body>
</html>
"""

@app.route('/')
def dashboard():
    # Fetch competitions
    competitions = []
    try:
        headers = {"X-Auth-Token": config.football_data_token}
        response = requests.get("https://api.football-data.org/v4/competitions", headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            competitions = data.get('competitions', [])[:3]
    except:
        pass

    # Fetch odds and tip
    odds_data = None
    try:
        odds_url = "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
        params = {"apiKey": config.odds_api_key, "regions": "eu", "markets": "h2h"}
        response = requests.get(odds_url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data:
                match = data[0]
                home_team = match['home_team']
                away_team = match['away_team']
                odds = match['bookmakers'][0]['markets'][0]['outcomes']
                home_odds = next(o['price'] for o in odds if o['name'] == home_team)
                away_odds = next(o['price'] for o in odds if o['name'] == away_team)
                draw_odds = next(o['price'] for o in odds if o['name'] == 'Draw')
                tip = "Home win" if home_odds < 2.0 else "Draw or Away"
                odds_data = {
                    'home_team': home_team,
                    'away_team': away_team,
                    'home_odds': home_odds,
                    'draw_odds': draw_odds,
                    'away_odds': away_odds,
                    'tip': tip
                }
    except:
        pass

    # Sample matches and table
    db = connect(config.db_url)
    db.ensure_schema()
    sample_matches = [
        Match("Team A", "Team B", 2, 1),
        Match("Team A", "Team C", 1, 1),
        Match("Team B", "Team C", 0, 3),
    ]
    for match in sample_matches:
        try:
            add_match(db, match)
        except:
            pass  # Already exists
    matches = list_matches(db)
    points_table = sorted(table(matches).items(), key=lambda x: x[1], reverse=True)

    return render_template_string(TEMPLATE,
                                  odds_key=config.odds_api_key,
                                  football_key=config.football_data_token,
                                  weather_key=config.weather_api_key,
                                  news_key=config.news_api_key,
                                  competitions=competitions,
                                  odds=odds_data,
                                  matches=matches,
                                  points_table=points_table)

if __name__ == '__main__':
    app.run(debug=True)