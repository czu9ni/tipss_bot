# tipss_bot

## Setup

Telepitsd a csomagokat:

```bash
pip install -r requirements.txt
```

Masold a `.env.example`-t `.env`-be, es toltsd ki az API kulcsokat:

- `FOOTBALL_DATA_TOKEN` (kobtelezo)
- `STATS_PROVIDER` (alap: `api_football`)
- `ODDS_PROVIDER` (alap: `the_odds_api`)
- `API_FOOTBALL_KEY`
- `ODDS_API_KEY` (kompatibilis: `THE_ODDS_API_KEY`)
- `SPORTRADAR_API_KEY` + `SPORTRADAR_API_BASE` (opcionalis)
- `STATS_PROVIDER_FALLBACK` (alap: `sportradar`)
- `CACHE_DIR` (alap: `data/cache`)
- `TIMEZONE` (alap: `Europe/Budapest`)
- `SOCCER_DB_URL` (sqlite)
- `RAPIDAPI_KEY` + `RAPIDAPI_HOST` + `THERUNDOWN_BASE_URL` + `THERUNDOWN_SPORT_ID_SOCCER`
- `BACKTEST_MODE` (1 = odds backtest gombok a dashboardon)

## Napi futtatas (CLI)

```bash
python -m soccer_bot run --date 2026-01-21
```

Kulon lepesek:

```bash
python -m soccer_bot fetch --date 2026-01-21
python -m soccer_bot pick --date 2026-01-21
python -m soccer_bot report --date 2026-01-21
```

Cache kihagyas:

```bash
python -m soccer_bot run --date 2026-01-21 --no-cache
```

## Odds backtest (TheRundown)

```bash
python -m soccer_bot odds openers --date 2026-01-21
python -m soccer_bot odds closing --date 2026-01-21
python -m soccer_bot odds delta --date 2026-01-21
python -m soccer_bot odds lines --event-id 123456
```

## Verification

```bash
python verify_all.py --skip-e2e
python verify_all.py --date 2026-01-21
```

## Troubleshooting

- Hianyzik a stat API kulcs: a CLI leall es jelzi a hibat.
- Hianyzik az odds kulcs: a CLI figyelmeztet, es odds nelkul fut.
- Offline mod: ha van cache a datumhoz, ujra fut cache-bol.

## Backtest

CSV formatum:
`date,home_team,away_team,home_odds,draw_odds,away_odds,result[,news_score,weather_score,stats_factor]`

Pelda parancs:
```bash
python -m soccer_bot.backtest --csv data/backtest.csv
```

## Elo backtest (Odds API)

Pelda parancs:
```bash
python -m soccer_bot.backtest_live --days 3 --max-sports 8
```

Megjegyzes: az Odds API scores vegpontja 1-3 napra enged visszanezni.
Ha nincs befejezett meccs, az eredmeny 0 lesz.

## Web inditas (Windows)

```bash
start.cmd
```
