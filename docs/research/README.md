# Trading Intelligence Foundation (Phase 0)

Phase 0 changes **no trading behaviour**. It adds (1) optional telemetry that is off by default and (2) offline research
tooling that the trading service never imports. Its purpose is to answer, with data, whether the current EMA 20/50 + ATR
strategy has a positive expectancy after costs, and to give every later change a fair way to prove itself.

## What is in the box

| Piece | Where | Touches the live service? |
|---|---|---|
| Per-trade context and a bounded decision log | `src/mba_bot/telemetry.py`, tables `trade_telemetry`, `decision_log`, flag `TRADE_TELEMETRY` (default **off**) | Only when the flag is on. Observation only, never raises, never flushes or commits. |
| Market features at decision time | `src/mba_bot/features.py` | Called only by telemetry. |
| Read-only data downloader and quality gate | `src/mba_bot/research/data.py`, `quality.py` | No. Public endpoints, GET only, no credentials. |
| Backtester V2 | `src/mba_bot/research/engine.py` | No. Reuses production strategy, risk and exchange-rule code unchanged. |
| Metrics, regimes, baseline report | `metrics.py`, `regimes.py`, `reporting.py` | No. |
| Walk-forward, lockbox, experiment ledger, deflated Sharpe, pass gate | `validation.py`, `experiments.py` | No. |
| Live and paper results (read-only database) | `live_report.py` | No. Opens SQLite with `mode=ro`. |

The trading service does not import `mba_bot.research` (pinned by a test), and the research package never imports
credentials, the database layer, configuration, the scheduler or order code (also pinned by a test).

## Telemetry

* Off by default. With `TRADE_TELEMETRY` unset the trade a cycle opens is identical to before (pinned by a test that runs both ways).
* `trade_telemetry` (one row per trade): the signal as it looked at decision time (signal price, ATR, EMAs, candle time), the sizing inputs
  (capital, risk, planned quantity, notional, stop, account leverage setting), Auto rank and score, about twenty market features
  (ADX, efficiency ratio, ATR percentile, returns, realised volatility, volume ratios, how far through the hourly candle the decision was taken),
  and the strategy id/version/parameters. Slippage, R-multiple and hold time are derived later from the trade row itself, so they are not duplicated.
* `decision_log`: one row per actionable moment (a crossover seen, a crossover rejected by the flat-market filter, a Spot-unusable bearish cross,
  an entry accepted or refused with a short reason code, an Auto scan pick or capital skip). Idle polls write nothing, the same decision on the same
  candle is written once, and each account is capped at 300 rows a day. Only numbers and short codes are stored.
* Exact fees and funding are **not** recorded by the trading path. The columns exist (`entry_fee_quote`, `exit_fee_quote`, `funding_quote`) but are empty;
  reports use a modelled fee and say so. Filling them needs a credentialed, supervised tool; it was deliberately not built in Phase 0.
* To turn it on (a deployment decision, not part of this phase): `alembic upgrade head`, set `TRADE_TELEMETRY=1`, restart. To turn it off: unset and restart.

## Getting historical data (read-only)

The downloader needs only `ccxt==4.5.88` installed (the research code imports the production strategy and exchange-rule modules, which need it).
It sends plain unsigned GET requests to `https://fapi.binance.com` for klines, premium-index klines (the basis proxy), funding history and exchangeInfo.
It never reads `.env`, never loads a key, and has no code path that places an order.

Run it in a **separate directory** so nothing under `/opt/mba-bot` is touched. The live bot shares this machine, so check it has room first and
run everything at the lowest priority:

```bash
free -m          # the baseline needs roughly 0.3 GB per worker (about 0.7 GB for the Auto Select runs)
df -h ~          # the data is about 250 MB
nproc

mkdir -p ~/mba-research && cd ~/mba-research
git clone --branch ccr-7642644c-2jb7jq --single-branch <the same repository URL you deploy from> code
python3 -m venv venv && ./venv/bin/pip install --quiet ccxt==4.5.88
export PYTHONPATH=$PWD/code/src

# 3 years of 1h and 5m candles, funding and premium index for the ten supported pairs.
# About 25 minutes, one request every 0.6 seconds. Re-running resumes where it stopped.
nohup nice -n 19 ionice -c3 ./venv/bin/python -m mba_bot.research download --root data --years 3 > download.log 2>&1 &
tail -f download.log          # Ctrl+C stops watching, not the download

./venv/bin/python -m mba_bot.research quality --root data            # exit code 1 if any file is corrupt

# CPU-heavy (about 20 minutes with one worker). Keep it small on the live machine, or copy ./data to another computer and run it there.
nice -n 19 ionice -c3 ./venv/bin/python -m mba_bot.research baseline --root data --out out --capital 1000 --workers 1
```

Send back `out/baseline.md`, `out/baseline.json`, `data/quality.json` and `data/manifest.json`. They contain prices and statistics only.
To look at the bot's own recorded trades afterwards (read-only, opens the SQLite file with `mode=ro`):
`./venv/bin/python -m mba_bot.research live --database /path/to/the/bot/database.db`.

## Reading the baseline

* **Two modes.** `production_replay` re-creates what the service does today (5-minute polls, the still-forming hourly candle).
  `closed_candle` reads the same signal only from a closed candle. The gap between them is the measured value of that fix, before anyone builds it.
* **E[R] net** is the average result per trade in units of the planned risk, after fees, slippage and funding. Positive and statistically distinguishable
  from zero is the question; the bootstrap interval shows how much the sample can say.
* **Cost sensitivity** repeats everything at 1.5x and 2x fees and slippage, because those numbers are assumptions.
* **Development vs holdout.** The original four pairs are the development set. The six added pairs are the holdout: they may be reported, never tuned on.
* Nothing is tuned in the baseline, so it carries no selection bias of its own.

## Rules for every later experiment

1. Write the hypothesis and `PassCriteria` first (`research/validation.py`). Do not edit them after seeing results.
2. Run through `experiments.evaluate_candidate`. It uses only the time before the lockbox and only development pairs, records every configuration
   in the ledger, computes the deflated Sharpe ratio against the number of configurations actually tried, and returns pass, fail or incomplete.
3. Open the lockbox once, deliberately (`Lockbox.open(..., confirm=True)`), and only for a candidate that already passed. A second opening marks it contaminated.
4. Shadow-test before any real-money flag.

## Known limits (also printed in every report)

Costs are assumptions. Historical exchange filters are not available, so the latest snapshot is used throughout. Poll phase is fixed to 5-minute
boundaries. Liquidation is not simulated. Bootstrap intervals assume independent trades. One market cycle is a small sample.


## Phase 1 (disciplined experiments)

`docs/research/phase1_preregistration.md` holds the registered plan, the survival screen and the exact commands. In short:
`python -m mba_bot.research phase1 plan` prints the plan hash, `phase1 run` executes every registered candidate on the development pairs and
period and writes `phase1_report.md`, and `phase1 lockbox --candidate ID --confirm --reason "..."` opens the sealed final months once.
The engine used is the Phase 0 engine plus research-only policies; with every policy at its default it reproduces Phase 0 exactly
(`tests/test_research_phase0_regression.py`). Nothing in Phase 1 touches the trading service.

## Phase 2 (confirmation)

`phase2_preregistration.md` freezes the arms, statistics and pass/fail rules (plan hash and code fingerprint inside) before the older
history is downloaded. Commands: `python -m mba_bot.research phase2 plan | resources | download | run | report`. `download` and `run`
refuse to start unless the hash, the fingerprint and the SHA-256 of `phase1_results.json` match the registered values. The older store is
one-shot evidence; see the registration for what that means and for the limitations.
