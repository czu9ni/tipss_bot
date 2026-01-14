# tipss_bot

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
