from dataclasses import dataclass

from soccer_bot.providers.base import OddsProvider
from soccer_bot.utils import HttpClient, normalize_team


@dataclass
class TheOddsApiProvider(OddsProvider):
    api_key: str
    client: HttpClient
    name: str = "the_odds_api"

    def fetch_odds(self, date_str: str) -> dict[str, dict]:
        odds_map: dict[str, dict] = {}
        markets = "h2h,totals,btts,double_chance"
        url = "https://api.the-odds-api.com/v4/sports/soccer/odds"
        resp = self.client.get(url, params={"apiKey": self.api_key, "regions": "eu", "markets": markets})
        if resp.status_code != 200:
            raise RuntimeError(f"The Odds API error: {resp.status_code}")
        for match in resp.json():
            home = match.get("home_team")
            away = match.get("away_team")
            if not home or not away:
                continue
            key = f"{normalize_team(home)}|{normalize_team(away)}"
            bookmakers = match.get("bookmakers") or []
            markets_out: dict[str, dict] = {}
            if bookmakers:
                for market in bookmakers[0].get("markets") or []:
                    key_name = str(market.get("key") or "")
                    outcomes = market.get("outcomes") or []
                    if key_name == "h2h":
                        parsed = {}
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
                            markets_out["1x2"] = parsed
                    elif key_name == "totals":
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
                            markets_out["over_under"] = parsed
                    elif key_name == "btts":
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
                            markets_out["btts"] = parsed
                    elif key_name == "double_chance":
                        parsed = {}
                        for outcome in outcomes:
                            name = str(outcome.get("name") or "").lower().replace(" ", "")
                            price = outcome.get("price")
                            if not isinstance(price, (int, float)):
                                continue
                            if name in {"homedraw", "1x"}:
                                parsed["1x"] = float(price)
                            elif name in {"drawaway", "x2"}:
                                parsed["x2"] = float(price)
                            elif name in {"homeaway", "12"}:
                                parsed["12"] = float(price)
                        if parsed:
                            markets_out["double_chance"] = parsed
            odds_map[key] = {"markets": markets_out}
        return odds_map
