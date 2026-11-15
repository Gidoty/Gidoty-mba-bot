# Downloading 6 months of 1h candles for the two backtests

The backtests `python -m mba_bot.rsi_backtest` and `python -m mba_bot.donchian_backtest` read `research_data\klines\1h\<SYMBOL>.csv`. This downloader writes exactly those files. It
is the data downloader of the parked research branch `ccr-7642644c-2jb7jq` (`src/mba_bot/research/data.py`, byte-identical), with its command line cut down to the `download` command.

## What it does and does not do

* Public Binance **USD-M Futures** market data only (`https://fapi.binance.com`), plain HTTPS `GET`, five read-only endpoints (klines, premium index klines, funding rate, exchange info,
  server time). No API key, no signature, no `.env`, no order endpoint, no account endpoint. Any other path is refused before a request is made.
* It needs **no pip install**: it uses only the Python standard library and the repository's own `strategy.py`.
* Only CLOSED candles are stored (a candle still forming is dropped). Re-running resumes: it appends only what is newer than the last row of each file.
* The candles are USD-M **Futures** candles. The backtests use one candle set for both markets (only the fees differ), so the Spot figures are Spot fees on Futures prices. Say so when reading them.

## The command (Windows PowerShell, Python 3.13)

Run from the repository folder (the one that contains `src`):

```powershell
cd C:\path\to\Gidoty-mba-bot
py -3.13 -m venv .venv
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m mba_bot.research download --root research_data --years 0.6 --pairs "BTC/USDT,ETH/USDT,BNB/USDT,XRP/USDT" --hourly-only --no-funding --no-premium
```

* Keep the quotes around the pairs: PowerShell treats an unquoted comma list as an array.
* `--years 0.6` is about 219 days. The backtests need 6 months (182.6 days) plus 14 days of warm-up, so 0.6 is enough and 0.45 is refused by the runners (a test pins both).
* `--hourly-only --no-funding --no-premium` is what keeps it to the four files the backtests read. Without `--hourly-only` it also downloads 5-minute candles (a lot more data); funding and premium
  files are not read by either backtest.
* To check the Python without activating the venv, use `.\.venv\Scripts\python.exe` as above. If you prefer to activate it: `.\.venv\Scripts\Activate.ps1` (if PowerShell refuses, run
  `Set-ExecutionPolicy -Scope Process Bypass` first).

Expected result: four progress lines (for example `BTCUSDT klines 1h: +5259 (total 5259)`), a JSON line with about 17 requests and 4 files, and `manifest: research_data\manifest.json`.
Check the files exist:

```powershell
Get-ChildItem research_data\klines\1h
```

Then, with the same `PYTHONPATH`:

```powershell
.\.venv\Scripts\python.exe -m mba_bot.rsi_backtest plan
.\.venv\Scripts\python.exe -m mba_bot.donchian_backtest plan
```

and follow the owner steps in `docs/research/rsi_mr_preregistration.md` and `docs/research/donchian_preregistration.md`.

## Rate and size limits

* Pause between requests: at least 0.6 seconds. A page is 1500 candles (a call of that size weighs 10 against Binance's 2400 per minute per IP). 0.6 years of 1h candles for four symbols is about
  4 pages each, so **about 17 requests, roughly 15 seconds**.
* On HTTP 429 or 418 it waits (it honours `Retry-After`), up to 6 attempts, then stops with an error. Also on server errors (5xx). If the used-weight header reaches 1500 it waits 30 seconds.
* Size: about 5,260 rows per symbol, roughly 0.4 to 0.6 MB per file, **about 2 to 3 MB in total** (the `manifest.json` and the exchange-info snapshot are a few KB).
* `research_data` stays outside git (do not commit it).

## What is NOT verified

The downloader has never been run against Binance from the environment that built this change (the market-data host is blocked there). It is tested only against a fake transport that serves
generated data in Binance's wire format. If Binance refuses your region (HTTP 451 or 403) the command stops with `unexpected HTTP <code>`; nothing else is attempted.
