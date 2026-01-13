from collections import defaultdict

from soccer_bot.repo import Match


def match_points(match: Match) -> dict[str, int]:
    if match.home_score > match.away_score:
        return {match.home_team: 3, match.away_team: 0}
    if match.home_score < match.away_score:
        return {match.home_team: 0, match.away_team: 3}
    return {match.home_team: 1, match.away_team: 1}


def table(matches: list[Match]) -> dict[str, int]:
    points: dict[str, int] = defaultdict(int)
    for match in matches:
        for team, score in match_points(match).items():
            points[team] += score
    return dict(points)