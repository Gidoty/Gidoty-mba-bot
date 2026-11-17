# Paper data source check, export and replay

Status: on a branch (PR 2). Nothing here changes bot behaviour. The owner runs every step.

## Finding from the code
`trader.process_account` builds the exchange client with `sandbox=effective_is_paper`. A paper account therefore reads candles from the Binance Spot Testnet (`testnet.binance.vision`) or the Futures Demo (`demo-fapi.binance.com`), not from mainnet. Live accounts read `api.binance.com` / `fapi.binance.com`. `python -m mba_bot.paper_source_check` prints this from the installed code and ccxt (no keys, no network).
Consequence: the "2 wins in 27 paper trades" were decided on testnet/demo candles. Those can differ from mainnet. The replay below measures how much.

## Steps (read-only)
1. Source check, offline: `python -m mba_bot.paper_source_check`. Add `--compare` to fetch public hourly candles from both hosts and report close differences (public endpoints, no key, GET only; run only when you choose to).
2. Export paper trades, either way:
   * `python -m mba_bot.paper_export --out paper_trades.csv`
   * or `sqlite3 -readonly <db file> < docs/ops/paper_trades_export.sql > paper_trades.csv`
   The export has no e-mail, no full account id (4 characters only), no keys. It never overwrites a file.
3. Replay against mainnet hourly candles (the research downloader store, `root/klines/1h/<SYMBOL>.csv`):
   `python -m mba_bot.paper_replay --trades paper_trades.csv --klines <root>/klines/1h --csv replay.csv`
   Per trade it reports whether the entry price sits inside the mainnet hour, the mainnet closed-candle EMA signal, whether the stop was touched afterwards, and paper R against mainnet-replay R. The summary gives wins on both and how many trades agree.
4. Paste the summary (not the CSV rows) back for review.

## Rollback
Nothing to roll back. Delete the CSV files.

## NOT verified
* No real paper data or mainnet data was read in this session. Tools are tested on synthetic data only.
* The replay is approximate: hourly candles, Spot paper trades against USD-M Futures candles, fees, funding and slippage ignored. It shows whether a result is plausible on mainnet, not what mainnet would have paid.
* The `--compare` run was never executed (no network to Binance from the sandbox).
