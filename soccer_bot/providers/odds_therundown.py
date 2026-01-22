from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from soccer_bot.utils import HttpClient, normalize_team_name


@dataclass
class TheRundownClient:
    base_url: str
    api_key: str
    api_host: str
    client: HttpClient

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        headers = {
            "x-rapidapi-key": self.api_key,
            "x-rapidapi-host": self.api_host,
        }
        resp = self.client.get(url, params=params, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"TheRundown error {resp.status_code}: {path}")
        return resp.json()

    @staticmethod
    def iso_date(value: str | None, fallback: str) -> str:
        if not value:
            return fallback
        return value.split("T", 1)[0]

    @staticmethod
    def event_teams(event: dict) -> tuple[str, str] | None:
        teams = event.get("teams") or []
        home = next((t for t in teams if t.get("is_home")), None)
        away = next((t for t in teams if t.get("is_away")), None)
        if home and away:
            return str(home.get("name") or ""), str(away.get("name") or "")
        return None

    @staticmethod
    def event_line_id(event: dict) -> str | None:
        lines = event.get("lines") or {}
        for _, item in lines.items():
            line_id = item.get("line_id")
            if line_id:
                return str(line_id)
        return None

    @staticmethod
    def american_to_decimal(value: float | int | None) -> float | None:
        if not isinstance(value, (int, float)):
            return None
        val = float(value)
        if val == 0 or abs(val) < 10:
            return None
        if abs(val) >= 100:
            if val > 0:
                return round((val / 100.0) + 1.0, 3)
            return round((100.0 / abs(val)) + 1.0, 3)
        return None

    @classmethod
    def event_key(cls, date_str: str, home: str, away: str) -> str:
        return f"{date_str}|{normalize_team_name(home)}|{normalize_team_name(away)}"

    @classmethod
    def markets_from_event(cls, event: dict) -> dict[str, dict]:
        markets: dict[str, dict] = {}
        lines = event.get("lines") or {}
        for _, item in lines.items():
            moneyline = item.get("moneyline") or {}
            totals = item.get("total") or item.get("totals") or {}
            spread = item.get("spread") or {}
            ml_home = cls.american_to_decimal(moneyline.get("moneyline_home"))
            ml_away = cls.american_to_decimal(moneyline.get("moneyline_away"))
            ml_draw = cls.american_to_decimal(moneyline.get("moneyline_draw"))
            if ml_home or ml_away or ml_draw:
                markets["1x2"] = {"home": ml_home, "away": ml_away, "draw": ml_draw}
            total_over = cls.american_to_decimal(totals.get("total_over_money"))
            total_under = cls.american_to_decimal(totals.get("total_under_money"))
            if total_over or total_under:
                markets["over_under"] = {"over_2.5": total_over, "under_2.5": total_under}
            spread_home = cls.american_to_decimal(spread.get("point_spread_home_money"))
            spread_away = cls.american_to_decimal(spread.get("point_spread_away_money"))
            if spread_home or spread_away:
                markets["spread"] = {"home": spread_home, "away": spread_away}
            if markets:
                break
        return markets

    @classmethod
    def markets_from_moneyline(cls, data: dict) -> dict[str, dict]:
        ml = (data.get("moneyline_periods") or {}).get("period_full_game") or []
        if not ml:
            return {}
        row = ml[0]
        return {
            "1x2": {
                "home": cls.american_to_decimal(row.get("moneyline_home")),
                "away": cls.american_to_decimal(row.get("moneyline_away")),
                "draw": cls.american_to_decimal(row.get("moneyline_draw")),
            }
        }

    @classmethod
    def markets_from_totals(cls, data: dict) -> dict[str, dict]:
        totals = (data.get("total_periods") or {}).get("period_full_game") or []
        if not totals:
            return {}
        row = totals[0]
        return {
            "over_under": {
                "over_2.5": cls.american_to_decimal(row.get("total_over_money")),
                "under_2.5": cls.american_to_decimal(row.get("total_under_money")),
            }
        }

    @classmethod
    def markets_from_spread(cls, data: dict) -> dict[str, dict]:
        spread = (data.get("spread_periods") or {}).get("period_full_game") or []
        if not spread:
            return {}
        row = spread[0]
        return {
            "spread": {
                "home": cls.american_to_decimal(row.get("point_spread_home_money")),
                "away": cls.american_to_decimal(row.get("point_spread_away_money")),
            }
        }

    def dates_with_odds(self, sport_id: str, offset: int = 300, fmt: str = "date") -> list[str]:
        data = self._get(f"sports/{sport_id}/dates", params={"format": fmt, "offset": str(offset)})
        return list(data.get("dates") or [])

    def events_for_date(
        self,
        sport_id: str,
        date_str: str,
        include_scores: bool = True,
        include_all_periods: bool = True,
        affiliate_ids: str = "1,2,3",
        offset: int = 0,
    ) -> list[dict]:
        include = "scores" if include_scores else ""
        params = {"include": include, "affiliate_ids": affiliate_ids, "offset": str(offset)}
        data = self._get(f"sports/{sport_id}/events/{date_str}", params=params)
        return list(data.get("events") or [])

    def moneyline(self, line_id: str, include_all_periods: bool = True) -> dict:
        params = {"include": "all_periods"} if include_all_periods else None
        return self._get(f"lines/{line_id}/moneyline", params=params)

    def totals(self, line_id: str, include_all_periods: bool = True) -> dict:
        params = {"include": "all_periods"} if include_all_periods else None
        return self._get(f"lines/{line_id}/total", params=params)

    def spread(self, line_id: str, include_all_periods: bool = True) -> dict:
        params = {"include": "all_periods"} if include_all_periods else None
        return self._get(f"lines/{line_id}/spread", params=params)

    def delta_changed_events(self, last_id: str, include_all_periods: bool = True) -> dict:
        params = {"last_id": last_id}
        if include_all_periods:
            params["include"] = "all_periods"
        return self._get("delta", params=params)

    def openers(self, sport_id: str, date_str: str, offset: int = 300) -> dict:
        params = {"offset": str(offset), "include": "scores&include=all_periods"}
        return self._get(f"sports/{sport_id}/openers/{date_str}", params=params)

    def closing(self, sport_id: str, date_str: str, offset: int = 300) -> dict:
        params = {"offset": str(offset), "include": "scores&include=all_periods"}
        return self._get(f"sports/{sport_id}/closing/{date_str}", params=params)

    def lines_historical(self, line_id: str) -> dict:
        moneyline = self.moneyline(line_id, include_all_periods=True)
        totals = self.totals(line_id, include_all_periods=True)
        spread = self.spread(line_id, include_all_periods=True)
        return {"moneyline": moneyline, "totals": totals, "spread": spread}
