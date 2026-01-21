from dataclasses import dataclass
from typing import Any

from soccer_bot.providers.base import Fixture, MatchStats, OddsProvider, StatsProvider, TeamStanding
from soccer_bot.utils import HttpClient


@dataclass
class ApiFootballStatsProvider(StatsProvider):
    api_key: str
    client: HttpClient
    name: str = "api_football"

    def fetch_fixtures(self, date_str: str) -> list[Fixture]:
        url = "https://v3.football.api-sports.io/fixtures"
        resp = self.client.get(url, params={"date": date_str}, headers={"x-apisports-key": self.api_key})
        if resp.status_code != 200:
            raise RuntimeError(f"API-Football fixtures error: {resp.status_code}")
        data = resp.json().get("response") or []
        fixtures: list[Fixture] = []
        for item in data:
            fixture = item.get("fixture", {})
            league = item.get("league", {})
            teams = item.get("teams", {})
            home = teams.get("home", {}).get("name")
            away = teams.get("away", {}).get("name")
            if not home or not away:
                continue
            fixtures.append(
                Fixture(
                    id=str(fixture.get("id")),
                    home_team=home,
                    away_team=away,
                    commence_time=str(fixture.get("date") or ""),
                    competition_id=str(league.get("id") or ""),
                    competition_name=str(league.get("name") or ""),
                    season=str(league.get("season") or ""),
                )
            )
        return fixtures

    def fetch_standings(self, competition_id: str, season: str) -> list[TeamStanding]:
        url = "https://v3.football.api-sports.io/standings"
        resp = self.client.get(
            url,
            params={"league": competition_id, "season": season},
            headers={"x-apisports-key": self.api_key},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"API-Football standings error: {resp.status_code}")
        data = resp.json().get("response") or []
        if not data:
            return []
        league = data[0].get("league", {})
        standings = league.get("standings") or []
        rows: list[TeamStanding] = []
        for group in standings:
            for row in group:
                team = row.get("team", {}).get("name")
                if not team:
                    continue
                rows.append(
                    TeamStanding(
                        team=team,
                        points=row.get("points"),
                        position=row.get("rank"),
                        played=row.get("all", {}).get("played"),
                        won=row.get("all", {}).get("win"),
                        draw=row.get("all", {}).get("draw"),
                        lost=row.get("all", {}).get("lose"),
                        goals_for=row.get("all", {}).get("goals", {}).get("for"),
                        goals_against=row.get("all", {}).get("goals", {}).get("against"),
                    )
                )
        return rows

    def fetch_match_events(self, fixture_id: str) -> list[dict]:
        url = "https://v3.football.api-sports.io/fixtures/events"
        resp = self.client.get(url, params={"fixture": fixture_id}, headers={"x-apisports-key": self.api_key})
        if resp.status_code != 200:
            raise RuntimeError(f"API-Football events error: {resp.status_code}")
        return resp.json().get("response") or []

    def fetch_match_stats(self, fixture_id: str) -> MatchStats:
        url = "https://v3.football.api-sports.io/fixtures/statistics"
        resp = self.client.get(url, params={"fixture": fixture_id}, headers={"x-apisports-key": self.api_key})
        if resp.status_code != 200:
            raise RuntimeError(f"API-Football stats error: {resp.status_code}")
        data = resp.json().get("response") or []
        home = None
        away = None
        for entry in data:
            team = entry.get("team", {})
            name = team.get("name")
            stats = entry.get("statistics") or []
            corners = None
            cards = 0.0
            for stat in stats:
                stat_type = str(stat.get("type") or "").lower()
                value = stat.get("value")
                if stat_type in {"corner kicks", "corners"}:
                    corners = float(value) if isinstance(value, (int, float)) else None
                if stat_type in {"yellow cards", "red cards", "cards"} and isinstance(value, (int, float)):
                    cards += float(value)
            if name:
                if home is None:
                    home = {"team": name, "corners": corners, "cards": cards}
                else:
                    away = {"team": name, "corners": corners, "cards": cards}
        return MatchStats(
            home_corners=home.get("corners") if home else None,
            away_corners=away.get("corners") if away else None,
            home_cards=home.get("cards") if home else None,
            away_cards=away.get("cards") if away else None,
        )


@dataclass
class ApiFootballOddsProvider(OddsProvider):
    api_key: str
    client: HttpClient
    name: str = "api_football"

    def fetch_odds(self, date_str: str) -> dict[str, dict]:
        url = "https://v3.football.api-sports.io/odds"
        bet_ids = {
            "1x2": "1",
            "double_chance": "2",
            "over_under": "5",
            "btts": "8",
        }
        odds_map: dict[str, dict] = {}
        for market_key, bet_id in bet_ids.items():
            resp = self.client.get(
                url,
                params={"date": date_str, "bookmaker": "8", "bet": bet_id},
                headers={"x-apisports-key": self.api_key},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"API-Football odds error: {resp.status_code}")
            data = resp.json().get("response") or []
            for item in data:
                fixture = item.get("fixture", {})
                fixture_id = str(fixture.get("id") or "")
                if not fixture_id:
                    continue
                markets = odds_map.setdefault(fixture_id, {}).setdefault("markets", {})
                for book in item.get("bookmakers") or []:
                    for bet in book.get("bets", []):
                        values = bet.get("values") or []
                        if market_key == "1x2":
                            parsed: dict[str, float] = {}
                            for value in values:
                                name = str(value.get("value") or "").lower()
                                odd = value.get("odd")
                                if not isinstance(odd, (int, float)):
                                    continue
                                if "home" in name:
                                    parsed["home"] = float(odd)
                                elif "away" in name:
                                    parsed["away"] = float(odd)
                                elif "draw" in name or "x" == name:
                                    parsed["draw"] = float(odd)
                            if parsed:
                                markets["1x2"] = parsed
                        elif market_key == "double_chance":
                            parsed = {}
                            for value in values:
                                name = str(value.get("value") or "").lower().replace(" ", "")
                                odd = value.get("odd")
                                if not isinstance(odd, (int, float)):
                                    continue
                                if name in {"homedraw", "1x"}:
                                    parsed["1x"] = float(odd)
                                elif name in {"drawaway", "x2"}:
                                    parsed["x2"] = float(odd)
                                elif name in {"homeaway", "12"}:
                                    parsed["12"] = float(odd)
                            if parsed:
                                markets["double_chance"] = parsed
                        elif market_key == "over_under":
                            parsed = {}
                            for value in values:
                                name = str(value.get("value") or "").lower()
                                odd = value.get("odd")
                                if not isinstance(odd, (int, float)):
                                    continue
                                if "2.5" not in name:
                                    continue
                                if "over" in name:
                                    parsed["over_2.5"] = float(odd)
                                if "under" in name:
                                    parsed["under_2.5"] = float(odd)
                            if parsed:
                                markets["over_under"] = parsed
                        elif market_key == "btts":
                            parsed = {}
                            for value in values:
                                name = str(value.get("value") or "").lower()
                                odd = value.get("odd")
                                if not isinstance(odd, (int, float)):
                                    continue
                                if name.startswith("yes"):
                                    parsed["yes"] = float(odd)
                                elif name.startswith("no"):
                                    parsed["no"] = float(odd)
                            if parsed:
                                markets["btts"] = parsed
        return odds_map
