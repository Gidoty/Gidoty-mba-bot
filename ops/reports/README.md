# Owner-run reports

## take_profit_reach_report.py

Read-only. Shows, for the closed live and practice trades that have a recorded best price (`peak_price`) and an initial stop, how far each trade ran (1, 1.5, 2, 3, 4, 5 R), and what the result would have been if a take profit had fired at each level. The hypothetical results use a stated cost ESTIMATE and assume the stop and the bot's own exits are unchanged: they are not a measurement.

```
python3 ops/reports/take_profit_reach_report.py --db /path/to/mba_bot.db --owner-email YOU@EXAMPLE.COM
```

* The database is opened read-only (`mode=ro` and `PRAGMA query_only`). Nothing is created, changed or locked. No key is needed and nothing from the bot is imported.
* Prints aggregates by kind (live or practice), strategy and pair, and (only with `--owner-email`) your own live trades one by one under a short trade code. It prints no account id, email, key, customer name or other customer data. The email is used only to select your rows.
* `--fee-bps` (default 5) and `--slip-rt-bps` (default 11) change the cost estimate.
* Trades without a best price or an initial stop are counted and skipped.
