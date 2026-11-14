# Phase 3: strategy guards, pre-registration

Status: registered before any real-data run. Nothing in this file has been measured on real market data. Plan hash: `fbf33597c9941a0d`
(`python -m mba_bot.research guards plan` prints it). Rules file SHA-256: `3338f7d35d429705dd25ab4d4f701e47dd4dfddc69f102bdfca82aebf29bec3d`
(`src/mba_bot/strategy_guard_rules.py`, byte-identical to the file the live code uses; see `docs/safety/strategy_guards.md` on the guards branch).

This is a NEW registration. The Phase 1 plan (`6f29fad5f4cd6e33`), the Phase 2 plan (`a3a0ff0672445ea7`), the Phase 2 code fingerprint
(`c39cd20e12619f0b`) and the Design 4 rules are unchanged. No registered or fingerprinted file was edited: the guards run in a subclass of the
engine that is swapped in for one run (`phase3_guards.guards_active`). Tests pin all four values.

## Question

Does each live guard lower drawdown without removing most of the edge, on the 6 most recent months of data? The answer for a guard can be "no"
or "cannot tell". A guard that is not eligible is not carried forward.

## Data split

* Window: the last 6 months of the downloaded store, for BTC, ETH, BNB and XRP (USDT-M).
* Tune window: the first 4 months. Every arm runs here, and only here.
* Embargo: 3 days between the tune window and the sealed window.
* Sealed window: the last 2 months. Read once, by `guards confirm`, through the lockbox and the look ledger. A second look needs `--second-look`
  and the result is labelled contaminated.

## Arms (18)

BASE (no guards); EMA gap 0.05, 0.1, 0.2, 0.3 ATR; stop cooldown 4, 12, 24 hours; fee guard margin 2, 3, 5; daily loss 2, 3, 5 percent;
pause after 3, 4, 5 losses in a row; expectancy pause over the last 20 trades. Each arm turns on one guard. The grids are fixed here and are not
widened after seeing results.

## Eligibility rule (fixed)

A value is eligible only if, on the tune window, it (a) keeps at least 50 percent of the baseline trades, (b) loses no more than 0.02 R of
expectancy per trade against baseline, and (c) does not raise max drawdown above baseline. Per guard family the eligible value with the lowest
drawdown wins; a tie goes to the higher expectancy. If none is eligible the family carries nothing. The chosen values are combined into one
`SELECTED` arm.

## Sealed window report

Baseline against `SELECTED` (and each selected guard alone), plus a 1.5x cost stress for both. A two-month window is short: it cannot show a
lasting effect, and one good result may be luck. `confirm` refuses to run if the plan hash, the rules hash, the data hash or the sealed range
differ from what the tuning recorded.

## What differs from the live guards

The backtest has one account and no clock for owner alerts. Pauses are simulated from the trade list. A loss-streak pause ends after its set
hours; the expectancy pause never ends inside a run (the live one needs the owner to release it). The fee guard uses the same proxy for expected
move as the live code. Cross-account correlation is not modelled. Position caps are untested here because the backtest has one position per
pair already.

## Not covered

`rsi_mr_v1` and the Donchian breakout do not exist yet. Their rows read "NOT MEASURED (the strategies do not exist)" and are built, with their
own guards and their own pre-registered runs, when each strategy is built.

## Owner steps (the owner runs these; nothing here touches the VPS or a secret)

1. Download 6 months of data with the existing downloader (read-only public market data). Check `python -m mba_bot.research guards plan`
   prints the plan hash above.
2. `python -m mba_bot.research guards tune --root research_data --out research_out/guards --expect-plan-hash fbf33597c9941a0d`
3. Read `research_out/guards/tune_report.md`. Paste it (no secrets are in it).
4. Only after that: `python -m mba_bot.research guards confirm --confirm --reason "<why>"`. This can be done once.
