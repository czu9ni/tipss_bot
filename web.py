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
            <h1 class="display-4">⚽ Soccer Bot AI Dashboard</h1>
            <p class="lead">Advanced Football Predictions & Analytics</p>
        </div>
    </div>

    <div class="container my-5">
        <div class="row">
            <div class="col-md-6">
                <div class="card mb-4">
                    <div class="card-header bg-primary text-white">
                        <h5 class="mb-0">🔑 API Status</h5>
                    </div>
                    <div class="card-body">
                        <p><strong>Odds API:</strong> {{ odds_key[:4] }}... ✅</p>
                        <p><strong>Football Data:</strong> {{ football_key[:4] }}... ✅</p>
                        <p><strong>Weather API:</strong> {{ weather_key[:4] }}... ✅</p>
                        <p><strong>News API:</strong> {{ news_key[:4] }}... ✅</p>
                    </div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="card mb-4">
                    <div class="card-header bg-success text-white">
                        <h5 class="mb-0">🏆 Active Competitions</h5>
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
                        <h5 class="mb-0">🎯 Live Match Odds & AI Prediction</h5>
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
                            <h5 class="ai-tip">🤖 AI Recommendation: {{ odds.tip }}</h5>
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
            <div class="col-md-6">
                <div class="card mb-4">
                    <div class="card-header bg-info text-white">
                        <h5 class="mb-0">📊 Recent Matches</h5>
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
                        <h5 class="mb-0">🏅 League Standings</h5>
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
                                {% for i, (team, points) in enumerate(points_table, 1) %}
                                <tr>
                                    <td>{{ i }}</td>
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
                # Enhanced AI tip with weather and news factors
                home_prob = 1 / home_odds
                away_prob = 1 / away_odds
                draw_prob = 1 / draw_odds
                home_score = home_prob * 0.4 + (1 if home_odds < 2.5 else 0) * 0.1
                away_score = away_prob * 0.4 + (1 if away_odds < 2.5 else 0) * 0.1
                draw_score = draw_prob * 0.2

                # Weather factor (mock for Manchester)
                weather_factor = 0
                if 'Manchester' in home_team or 'Manchester' in away_team:
                    try:
                        weather_response = requests.get(f"https://api.openweathermap.org/data/2.5/weather?q=Manchester&appid={config.weather_api_key}", timeout=5)
                        if weather_response.status_code == 200:
                            weather_data = weather_response.json()
                            if 'rain' in weather_data.get('weather', [{}])[0].get('main', '').lower():
                                weather_factor = -0.1  # Rain reduces scoring
                    except:
                        pass

                # News factor (mock sentiment)
                news_factor = 0
                try:
                    news_response = requests.get(f"https://newsapi.org/v2/everything?q={home_team}&apiKey={config.news_api_key}&pageSize=5", timeout=5)
                    if news_response.status_code == 200:
                        news_data = news_response.json()
                        articles = news_data.get('articles', [])
                        negative_words = ['injury', 'suspension', 'loss', 'defeat']
                        positive_words = ['win', 'victory', 'goal']
                        sentiment = 0
                        for article in articles:
                            title = article.get('title', '').lower()
                            if any(word in title for word in negative_words):
                                sentiment -= 0.02
                            if any(word in title for word in positive_words):
                                sentiment += 0.02
                        news_factor = sentiment
                except:
                    pass

                # Final scores with factors
                home_final = home_score + weather_factor + news_factor
                away_final = away_score + weather_factor + news_factor
                draw_final = draw_score

                if home_final > away_final and home_final > draw_final:
                    tip = f"Home win (AI: prob {home_prob:.2f}, weather {weather_factor:.2f}, news {news_factor:.2f})"
                elif away_final > home_final and away_final > draw_final:
                    tip = f"Away win (AI: prob {away_prob:.2f}, weather {weather_factor:.2f}, news {news_factor:.2f})"
                else:
                    tip = f"Draw (AI: prob {draw_prob:.2f}, weather {weather_factor:.2f}, news {news_factor:.2f})"
                odds_data = {
                    'home_team': home_team,
                    'away_team': away_team,
                    'home_odds': home_odds,
                    'draw_odds': draw_odds,
                    'away_odds': away_odds,
                    'home_prob': home_prob,
                    'draw_prob': draw_prob,
                    'away_prob': away_prob,
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