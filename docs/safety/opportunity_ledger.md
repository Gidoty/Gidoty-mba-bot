# Opportunity ledger, tier probe, opportunity report, entry-timing analysis

Status: the ledger ships OFF (`OPPORTUNITY_LEDGER=false`). The probe and the report are read-only commands that do nothing until the owner runs them. No entry rule, guard, size, stop or switch changed, and no migration was added.

The evidence below about the owner's `decision_log` (account 27624db9, 18 to 20 Nov) is AS REPORTED BY THE OWNER. This environment cannot read the VPS or its database, so none of it was re-checked here.

## A. Tier probe: `python -m mba_bot.tiers_probe`

Run it on the server, from the bot's directory, with the bot's own environment:

    cd /opt/mba-bot && PYTHONPATH=src .venv/bin/python -m mba_bot.tiers_probe                                    # lists active Futures accounts
    cd /opt/mba-bot && PYTHONPATH=src .venv/bin/python -m mba_bot.tiers_probe --account 27624db9 --pair XRP/USDT  # reads that pair

* Without `--account` it only lists the active Futures accounts (short id, pair, Auto or manual, whether a live key is on file, opted-in strategies) and contacts nobody. With `--account` it needs at least 4 characters of the id and exactly one match.
* It reads the brackets through the entry check's own building blocks: `exchange._read_once` twice (once through ccxt's parser, once through the raw endpoint that accepts either answer shape), then `exchange.read_maintenance_tiers` exactly as the entry check calls it.
* It prints only: which attempt worked, the shape of Binance's answer (list of N, single object, mapping of N), the number of brackets, the first and last bracket's floor, cap and rate, whether the plausibility check passes (and the first failing check if not), and the cause when a read failed. Messages go through `scrub_exchange_text`, which removes key-like tokens and cuts to 200 characters.
* It never prints a key, a signature, a header, a balance, the account e-mail or the full account id. A crash prints the exception class name only.
* It cannot place, change or cancel anything. The client is wrapped (`ReadOnlyClient`): only `market`, `load_markets`, `fetch_leverage_tiers`, `fapiPrivateGetLeverageBracket`, `parse_market_leverage_tiers` and `close` are reachable; any other attribute (an order, a cancel, a leverage or margin change, a balance read, any POST, PUT or DELETE endpoint) raises before a request exists. A test pins the list and a second test scans the source for write-method names.
* What it sends to Binance: it uses the account's STORED credentials, decrypted inside the existing path (`trader._resolve_credentials`, the key never leaves it), and sends 1 public read (`GET exchangeInfo`, via `load_markets`) and up to 4 SIGNED READ requests: `GET leverageBracket` once through ccxt, once raw, then the entry check's own read (which tries twice if the first fails). Nothing else. A read key needs the same IP permission it needs for the bot itself (UNVERIFIED which Binance permission `leverageBracket` requires).
* The database is opened read-only (`PRAGMA query_only`).

## B. Opportunity ledger

`OPPORTUNITY_LEDGER=true` writes rows into the existing `decision_log` table with `stage = "opportunity"`. No new table, no migration, and every existing consumer that filters `stage in (signal, entry, auto_scan)` is unaffected. The one consumer that read "the last 10 decisions" without a stage filter (`pilot_status`) now excludes the ledger.

One row per account, strategy, pair and closed candle for every evaluation. Outcomes: `no_signal`, `signal`, `not_taken`.

| strategy | no-signal codes | signal codes | numbers stored (`detail_json`) |
|---|---|---|---|
| EMA (`ema_crossover_v1`) | `ema_no_cross`, `ema_flat_market_filter`, `ema_insufficient_history` | `ema_cross_up`, `ema_cross_down` | fast and slow EMA, gap in ATR, ATR% |
| Donchian | `donchian_inside_channel`, `donchian_wick_close_inside`, `donchian_no_volume_confirmation`, `donchian_atr_extreme`, `donchian_short_on_spot`, `donchian_volume_unavailable`, `donchian_indicator_unavailable`, `donchian_insufficient_history` | `donchian_breakout_long`, `donchian_breakout_short` | channel high and low, volume ratio, ATR%, ATR% average |
| RSI | `rsi_no_cross`, `rsi_trending_market_adx`, `rsi_short_on_spot`, `rsi_indicator_unavailable`, `rsi_insufficient_history` | `rsi_cross_up`, `rsi_cross_down` | RSI, previous RSI, ADX, ATR% |

`price` is the closed candle's close. A test reads the reason strings out of `strategy.py`, `donchian.py` and `rsi_mr.py` and fails if a new no-signal reason has no code.

`not_taken` rows, written where the code already knows the reason: `signal_stale` (signal older than the entry window), `not_best_ranked` (Auto Select picked another pair; the row carries the picked pair), `auto_skip_insufficient_capital` (Auto Select dropped a pair for capital), `strategy_not_evaluated` (see finding 2). The existing `entry`-stage rows (`maintenance_tiers_*`, `stop_beyond_safe_liquidation_distance`, and so on) stay where they are and the report merges them.

The ledger does not recompute anything for the strategies: it reads the signal objects the strategies already return, so the numbers are the ones that decided. The one extra call is the EMA signal for pairs scanned in Auto Select, a pure function the scan already ran.

Safety:
* Every ledger call sits inside its own `try/except`; a failure logs at debug and never reaches the cycle. Rows are `session.add`ed only (no commit, no flush, no network), so they ride the cycle's own commit.
* Entry decisions are identical with the ledger on and off: `test_entry_decisions_are_identical_with_the_ledger_on_and_off` runs paper Donchian, paper RSI, a paper flat market, a live Futures EMA entry and a live flat Spot account twice (off, on) and compares the trades, every entry-stage decision row, the account state and every order sent to the exchange.
* The telemetry cap (300 rows per account per day) is not shared: entry-stage rows can never be starved by the ledger.

### Volume

One row per key (account, strategy, pair, candle, outcome, reason) however many 5-minute rounds see the same candle (in-memory marks, plus a database check once per key after a restart). On the 1-hour timeframe:

* manual account, EMA only: 24 rows per day; with Donchian and RSI opted in: 72 per day.
* Auto Select account, EMA scanning the four pairs: 96 per day, plus up to 3 `not_best_ranked` rows per signal, plus 48 `strategy_not_evaluated` rows per day if Donchian and RSI are opted in and live. About 150 per day, a few more on signal days.
* 100 accounts all on three strategies: roughly 7,200 rows per day, about 216,000 over the 30 days kept. At roughly 250 bytes a row that is about 55 MB. (ESTIMATE from the row shape, not measured.)
* Cap: `OPPORTUNITY_LEDGER_DAILY_CAP` (default 1500) rows per account per UTC day, one warning per account per day when reached. Retention: `OPPORTUNITY_LEDGER_KEEP_DAYS` (default 30); once per UTC day per process it deletes up to 5,000 ledger rows older than that, ledger rows only.

Write cost: one indexed existence check per new key after a process start, and one insert. No new locks held across a network call.

### Rollback

Set `OPPORTUNITY_LEDGER=false` and restart. Existing rows stay and are readable by the report. `DELETE FROM decision_log WHERE stage = 'opportunity'` removes them (owner decision, not run here).

## C. Report: `python -m mba_bot.opportunity_report --hours 24 [--account 27624db9]`

Per account (8 characters) and strategy: candles evaluated, signals, entries opened, and every non-entry with its reason, grouped and counted, with plain words. Evaluation counts come from the ledger; entries opened and refusals from the entry-stage rows (which need `TRADE_TELEMETRY`). It opens the database read-only, calls no exchange, writes nothing, and prints no secret, e-mail or full id. It exits 2 with the exception class only if the database is missing (it never creates one).

The existing daily summary (`trader._maybe_send_daily_summary`) gets the same facts as plain text lines ("Entry checks, last 24 hours: ...") appended only when the ledger is on and there is something to say. No new alert type. That summary is only sent on days with at least one closed trade, so quiet days are covered by the report command.

## Findings from reading the code (not from the VPS)

1. **Account 27624db9's EMA rejections are about the bracket read, not about timing.** As reported: every actionable EMA signal since 18 Nov ended in `maintenance_tiers_unavailable`. That code name is the legacy one (`TIERS_UNAVAILABLE`); PR 76 split it into `maintenance_tiers_read_failed`, `_invalid` and `_no_match`. If the server still logs the old name, it may still be running the code before PR 76 (the owner reported main 9e30561 deployed; the probe will say which case it is). Run the probe for 27624db9 on SOL/USDT, XRP/USDT and DOGE/USDT first. The shape of Binance's answer for a single symbol is UNVERIFIED here: real ccxt 4.5.88 returns `{}` from `fetch_leverage_tiers` for a single-object answer, and the raw attempt exists for that case.
2. **On Auto Select, Donchian and RSI are only looked at when EMA found a signal.** `process_account` returns before `_live_donchian_entry` and `_live_rsi_entry` when the Auto scan finds no actionable EMA signal. That explains why the owner sees no Donchian or RSI row for 27624db9: they were not evaluated on those cycles, not "evaluated and quiet". When EMA does find a signal, they are evaluated on the one pair Auto Select picked, not on the pairs where they might have fired. The ledger now says so with `strategy_not_evaluated` rows. This PR does not change that behaviour: changing it is an entry-rule change and an owner decision (question 2 in the PR).
3. **The extra Futures pairs (SOL, DOGE, SUI, LINK, ADA, LTC) are manual-only.** Auto Select scans BTC, ETH, BNB and XRP (`config.auto_pairs()`). As reported, the DOGE and SOL signals were on an account on auto selection; the code's explanation is that the account's configured pair was SOL or DOGE at the time, or the signals came through another path. UNVERIFIED: which pair and which path, since the decision_log rows were not readable here. The ledger rows carry `pair` and `sel` (`auto` or `manual`) so the next report settles it.
4. **BTC skipped for insufficient capital on 15 and 16 Nov** is the existing `auto_skip` behaviour (a pair whose smallest order does not fit the account's capital is skipped). The ledger now records it as `auto_skip_insufficient_capital`.

## D. Entry timing (analysis only, nothing implemented)

How many tries does a signal get? From the code:

* The EMA entry is allowed when `0 <= age <= EMA_ENTRY_WINDOW_MINUTES` (default 10), age being now minus the signal candle's close time (`_ema_entry_stale`; `test_window_edge` pins 10 minutes inside and 11 outside). New strategies use `NEW_STRATEGY_ENTRY_WINDOW_MINUTES`, also 10.
* `run_poll_forever` runs a round, then sleeps `POLL_INTERVAL_SECONDS` (300). The period is 300 seconds PLUS the round's duration D, not a fixed 5 minutes. A round that takes 30 seconds gives a 330-second period.
* A closed window of W seconds with rounds every P seconds holds floor(W/P) rounds, plus one with probability frac(W/P). With W = 600:
  * D = 0: P = 300, exactly 2 tries; a third only when a round lands on the close second and exactly 10 minutes later (never in practice, the second needs D = 0).
  * D = 30 s: P = 330, 1 try (18%) or 2 tries (82%).
  * D = 60 s: P = 360, 1 or 2 tries (67% for 2).
  * D = 120 s (the cycle watchdog): P = 420, 1 or 2 tries (43% for 2).
* So a signal gets one or two tries, never three. The owner's SOL case (00:00 failed, 00:05 failed, 00:10 `signal_stale`) fits: the third round starts a few seconds after the 10-minute mark, so it is stale by the round duration. That is an inference from the code; the decision_log timestamps were not readable here.
* Consequence: any transient failure in the first try (a bracket read, a rate limit, a network blip) leaves exactly one more try. Failing both, as on 20 Nov, forfeits the signal.

Proposal (not built): a fast loop that runs only in the first 10 minutes after each hourly close, every 60 seconds (about 10 rounds), then the normal 5-minute loop for the rest of the hour. Each round runs the same `process_account` (one code path), with this added guard in the entry step only:

* Price-drift guard: do not enter when the live price has moved more than 0.25 ATR beyond the signal candle's close in the entry direction (long: price > close + 0.25 x ATR; short: price < close - 0.25 x ATR). Reason: the stop sits 2 ATR away (`ATR_STOP_MULTIPLIER = 2.0`), so 0.25 ATR of drift gives up 12.5% of the stop distance as reward for the same risk. 0.25 is a proposal to be backtested on the paper record, not a tested value.
* A signal that was entered, or refused for a reason that cannot change in minutes (kill switch, daily loss limit), is not retried; one that failed for a transient reason (`maintenance_tiers_read_failed`, a network error) is.

Extra Binance requests (per account per hour, counting what the code requests per round: one candle request per strategy per pair, plus the bracket read on an entry attempt; 1-hour timeframe, `OHLCV_LIMIT` 200):

| account | candle requests per round | now: 2 tries | proposed: ~10 tries | extra per hour |
|---|---|---|---|---|
| manual, EMA only | 1 | 2 | 10 | +8 |
| manual, EMA + Donchian + RSI | 3 | 6 | 30 | +24 |
| Auto Select, EMA over 4 pairs | 4 | 8 | 40 | +32 |
| Auto Select, EMA + Donchian + RSI | up to 6 | 12 | 60 | +48 |

Worst case, a persistently failing signal on an Auto Select account with all strategies: each of the 10 rounds also makes up to 2 bracket reads (first read plus the retry): 10 x (6 + 2) = 80 requests in the 10 minutes against 2 x 8 = 16 now, so +64 per hour. Orders add 3 to 5 requests only on an actual entry.

Rate limit: Binance limits requests per IP by weight, and the accounts all share the VPS's IP. The weights and the per-minute budget are UNVERIFIED here (Binance documentation was not reachable from this environment; from memory a 200-candle request costs weight 2 and the USD-M limit is on the order of 2,400 per minute). Per-account arithmetic: N accounts x (candle requests per round) per 60-second round. For 100 accounts on three strategies that is 300 requests (about 600 weight, if weight 2) in each fast round, a quarter of the remembered budget. Safer design: fetch each pair's public candles ONCE per round and share them across accounts (candles do not depend on the account), which makes the load depend on pairs, not accounts. That needs a shared fetch layer that does not exist today.

Decision for the owner: whether to build it (question 3 in the PR), and the drift fraction.
