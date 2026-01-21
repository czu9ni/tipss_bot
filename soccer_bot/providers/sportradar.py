from dataclasses import dataclass

from soccer_bot.providers.base import Fixture, MatchStats, StatsProvider, TeamStanding
from soccer_bot.utils import HttpClient, normalize_team


@dataclass
class SportradarStatsProvider(StatsProvider):
    api_key: str
    api_base: str
    client: HttpClient
    name: str = "sportradar"

    def _get(self, path: str) -> dict:
        url = f"{self.api_base.rstrip('/')}/{path.lstrip('/')}"
        resp = self.client.get(url, headers={"x-api-key": self.api_key, "accept": "application/json"})
        if resp.status_code != 200:
            raise RuntimeError(f"Sportradar error {resp.status_code}: {path}")
        return resp.json()

    def fetch_fixtures(self, date_str: str) -> list[Fixture]:
        data = self._get(f"schedules/{date_str}/schedules.json")
        events = data.get("schedules") or data.get("sport_events") or []
        fixtures: list[Fixture] = []
        for item in events:
            event = item.get("sport_event") if isinstance(item, dict) else None
            if not isinstance(event, dict):
                continue
            context = event.get("sport_event_context") or {}
            comp = context.get("competition") or {}
            season = context.get("season") or {}
            competitors = event.get("competitors") or []
            home = next((c for c in competitors if c.get("qualifier") == "home"), {})
            away = next((c for c in competitors if c.get("qualifier") == "away"), {})
            if not home or not away:
                continue
            fixtures.append(
                Fixture(
                    id=str(event.get("id")),
                    home_team=str(home.get("name") or ""),
                    away_team=str(away.get("name") or ""),
                    commence_time=str(event.get("start_time") or ""),
                    competition_id=str(comp.get("id") or ""),
                    competition_name=str(comp.get("name") or ""),
                    season=str(season.get("id") or ""),
                )
            )
        return fixtures

    def fetch_standings(self, competition_id: str, season: str) -> list[TeamStanding]:
        if not season:
            return []
        data = self._get(f"seasons/{season}/standings.json")
        standings = data.get("standings") or []
        rows: list[TeamStanding] = []
        for entry in standings:
            for group in entry.get("groups") or []:
                for row in group.get("standings") or []:
                    team = (row.get("competitor") or {}).get("name")
                    if not team:
                        continue
                    rows.append(
                        TeamStanding(
                            team=str(team),
                            points=row.get("points"),
                            position=row.get("rank"),
                            played=row.get("played"),
                            won=row.get("win"),
                            draw=row.get("draw"),
                            lost=row.get("loss"),
                            goals_for=row.get("goals_for"),
                            goals_against=row.get("goals_against"),
                        )
                    )
        if rows:
            return rows
        # Fallback: derive table from season summaries (if standings not provided)
        data = self._get(f"seasons/{season}/summaries.json")
        summaries = data.get("summaries") or []
        table: dict[str, dict] = {}
        for item in summaries:
            event = item.get("sport_event") or {}
            status = item.get("sport_event_status") or {}
            if status.get("status") not in {"closed", "ended"}:
                continue
            competitors = event.get("competitors") or []
            home = next((c for c in competitors if c.get("qualifier") == "home"), {})
            away = next((c for c in competitors if c.get("qualifier") == "away"), {})
            home_name = home.get("name")
            away_name = away.get("name")
            home_score = status.get("home_score")
            away_score = status.get("away_score")
            if not home_name or not away_name:
                continue
            if not isinstance(home_score, int) or not isinstance(away_score, int):
                continue
            for team in (home_name, away_name):
                key = normalize_team(str(team))
                if key not in table:
                    table[key] = {
                        "team": team,
                        "points": 0,
                        "played": 0,
                        "won": 0,
                        "draw": 0,
                        "lost": 0,
                        "goals_for": 0,
                        "goals_against": 0,
                    }
            home_row = table[normalize_team(str(home_name))]
            away_row = table[normalize_team(str(away_name))]
            home_row["played"] += 1
            away_row["played"] += 1
            home_row["goals_for"] += home_score
            home_row["goals_against"] += away_score
            away_row["goals_for"] += away_score
            away_row["goals_against"] += home_score
            if home_score > away_score:
                home_row["won"] += 1
                away_row["lost"] += 1
                home_row["points"] += 3
            elif away_score > home_score:
                away_row["won"] += 1
                home_row["lost"] += 1
                away_row["points"] += 3
            else:
                home_row["draw"] += 1
                away_row["draw"] += 1
                home_row["points"] += 1
                away_row["points"] += 1

        if not table:
            data = self._get(f"seasons/{season}/competitors.json")
            competitors = data.get("season_competitors") or []
            if not competitors:
                return []
            for idx, comp in enumerate(sorted(competitors, key=lambda c: str(c.get("name") or "")), start=1):
                team_name = comp.get("name")
                if not team_name:
                    continue
                rows.append(
                    TeamStanding(
                        team=str(team_name),
                        points=0,
                        position=idx,
                        played=0,
                        won=0,
                        draw=0,
                        lost=0,
                        goals_for=0,
                        goals_against=0,
                    )
                )
            return rows
        sorted_rows = sorted(
            table.values(),
            key=lambda row: (row["points"], row["goals_for"] - row["goals_against"], row["goals_for"]),
            reverse=True,
        )
        for idx, row in enumerate(sorted_rows, start=1):
            rows.append(
                TeamStanding(
                    team=str(row["team"]),
                    points=row["points"],
                    position=idx,
                    played=row["played"],
                    won=row["won"],
                    draw=row["draw"],
                    lost=row["lost"],
                    goals_for=row["goals_for"],
                    goals_against=row["goals_against"],
                )
            )
        return rows

    def fetch_match_events(self, fixture_id: str) -> list[dict]:
        data = self._get(f"sport_events/{fixture_id}/timeline.json")
        return data.get("timeline") or []

    def fetch_match_stats(self, fixture_id: str) -> MatchStats:
        data = self._get(f"sport_events/{fixture_id}/summary.json")
        summary = data.get("statistics") or {}
        home = summary.get("home") or {}
        away = summary.get("away") or {}
        return MatchStats(
            home_corners=home.get("corner_kicks"),
            away_corners=away.get("corner_kicks"),
            home_cards=home.get("cards_given"),
            away_cards=away.get("cards_given"),
        )
