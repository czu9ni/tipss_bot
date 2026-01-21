from dataclasses import dataclass
from typing import Protocol


@dataclass
class Fixture:
    id: str
    home_team: str
    away_team: str
    commence_time: str
    competition_id: str
    competition_name: str
    season: str


@dataclass
class TeamStanding:
    team: str
    points: int | None
    position: int | None
    played: int | None
    won: int | None
    draw: int | None
    lost: int | None
    goals_for: int | None
    goals_against: int | None


@dataclass
class MatchStats:
    home_corners: float | None
    away_corners: float | None
    home_cards: float | None
    away_cards: float | None


class StatsProvider(Protocol):
    name: str

    def fetch_fixtures(self, date_str: str) -> list[Fixture]:
        ...

    def fetch_standings(self, competition_id: str, season: str) -> list[TeamStanding]:
        ...

    def fetch_match_events(self, fixture_id: str) -> list[dict]:
        ...

    def fetch_match_stats(self, fixture_id: str) -> MatchStats:
        ...


class OddsProvider(Protocol):
    name: str

    def fetch_odds(self, date_str: str) -> dict[str, dict]:
        ...
