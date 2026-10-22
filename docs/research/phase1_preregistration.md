# Phase 1 pre-registration

**Plan hash `6f29fad5f4cd6e33`** (29 candidates, 33 registered trials).
This document and the plan in `src/mba_bot/research/phase1.py` were committed before any candidate was run on real data. The runner
refuses to start (`--expect-plan-hash`) if the plan, the survival screen, the folds or the lockbox rule differ from this hash. A change
means a new experiment with a new hash and a new entry in this file; it never means editing a result.

## Question

Can the current EMA 20/50 + ATR strategy be made materially more robust without changing production, judged by after-cost expectancy,
profit factor, drawdown, stability across years and pairs, cost stress, and out-of-sample behaviour, while keeping the large winners
that make a 20-25% win rate viable? A negative answer is an acceptable result.

## Honest limits of this design (read before trusting any result)

* The hypotheses were suggested by the full-sample Phase 0 baseline, which included the months now reserved as the lockbox and all ten
  pairs. So the time-based lockbox and the six holdout pairs are weaker evidence than they would be for a hypothesis chosen blind. The
  only fully clean evidence is data that does not exist yet (a forward shadow period).
* Every candidate changes one thing. Families vary one parameter. Nothing is tuned jointly.
* 33 configurations are tried, so some will look good by chance. The screen, the walk-forward windows and the
  deflated Sharpe ratio exist for that reason.
* Costs are assumptions (5 bp fee, 3 bp slippage, 8 bp stop slippage, BTC/ETH lower), identical to the Phase 0 baseline.

## Data roles

* Development pairs: BTC, ETH, BNB, XRP. Development period: everything before the final 6 months (minus a 3-day embargo).
* Holdout pairs: SOL, DOGE, SUI, LINK, ADA, LTC. Nothing is chosen or tuned on them. The baseline's behaviour there is described (side, volatility and ADX tables), and a candidate is scored on them once, only if it survives the development screen.
* Lockbox: the final 6 months. `run` never touches it. It is opened by a separate logged command, once, for one candidate.
* Walk-forward: 365-day training, 91-day test windows tiling the development period, 1-day embargo. Rules with nothing fitted are scored on
  the trades that enter inside each test window; parameter families are also scored by choosing the member on training windows only.

## Candidates

| ID | Hypothesis | Family | Scope | Rule | Policy fields changed |
|---|---|---|---|---|---|
| H1-P3 | H1 | H1_persist | fixed | cross must persist 3 consecutive polls (15 min) | `{'confirm_polls': 3}` |
| H1-P6 | H1 | H1_persist | fixed | cross must persist 6 consecutive polls (30 min) | `{'confirm_polls': 6}` |
| H1-P9 | H1 | H1_persist | fixed | cross must persist 9 consecutive polls (45 min) | `{'confirm_polls': 9}` |
| H1-M05 | H1 | H1_margin | fixed | EMA gap at least 0.05 ATR when entering | `{'confirm_margin_atr': 0.05}` |
| H1-M10 | H1 | H1_margin | fixed | EMA gap at least 0.10 ATR when entering | `{'confirm_margin_atr': 0.1}` |
| H1-M20 | H1 | H1_margin | fixed | EMA gap at least 0.20 ATR when entering | `{'confirm_margin_atr': 0.2}` |
| H1-WC | H1 | H1_close | fixed | enter only on a closed candle (exits unchanged) | `{'wait_for_close_sides': ('long', 'short')}` |
| H2-V67 | H2 | H2_skip | fixed | skip entries above the 67th volatility percentile | `{'vol_skip_above': 0.67}` |
| H2-V80 | H2 | H2_skip | fixed | skip entries above the 80th volatility percentile | `{'vol_skip_above': 0.8}` |
| H2-V90 | H2 | H2_skip | fixed | skip entries above the 90th volatility percentile | `{'vol_skip_above': 0.9}` |
| H2-HR80 | H2 | H2_half | fixed | half the risk above the 80th volatility percentile | `{'vol_half_risk_above': 0.8}` |
| H3-SC | H3 | H3_short_close | fixed | shorts enter only on a closed candle | `{'wait_for_close_sides': ('short',)}` |
| H3-SP6 | H3 | H3_short_persist | fixed | shorts need a 30-minute persistent cross | `{'confirm_sides': ('short',), 'confirm_polls': 6}` |
| H3-SM10 | H3 | H3_short_margin | fixed | shorts need an EMA gap of 0.10 ATR | `{'confirm_sides': ('short',), 'confirm_margin_atr': 0.1}` |
| H3-ST0 | H3 | H3_short_trend | fixed | shorts only while the 30-day return is at or below 0 | `{'short_max_ret30d': 0.0}` |
| H3-ST10 | H3 | H3_short_trend | fixed | shorts only while the 30-day return is at or below -10% | `{'short_max_ret30d': -0.1}` |
| D-NOSHORT | H3 | diag_side | fixed, diagnostic | DIAGNOSTIC: no shorts (measures what shorts contribute) | `{'short_allowed': False}` |
| D-NOLONG | H3 | diag_side | fixed, diagnostic | DIAGNOSTIC: no longs (measures what longs contribute) | `{'long_allowed': False}` |
| H4-XC | H4 | H4_exit_close | fixed | act on the opposite crossover only when the hourly candle closes | `{'exit_on_close_only': True}` |
| H4-T3 | H4 | H4_trail | fixed | trail 3 ATR behind the best price once +1R | `{'trail_atr_mult': 3.0}` |
| H4-T4 | H4 | H4_trail | fixed | trail 4 ATR behind the best price once +1R | `{'trail_atr_mult': 4.0}` |
| H4-T5 | H4 | H4_trail | fixed | trail 5 ATR behind the best price once +1R | `{'trail_atr_mult': 5.0}` |
| H4-B10 | H4 | H4_be | fixed | stop to breakeven after +1.0R | `{'breakeven_at_r': 1.0}` |
| H4-B15 | H4 | H4_be | fixed | stop to breakeven after +1.5R | `{'breakeven_at_r': 1.5}` |
| H4-B20 | H4 | H4_be | fixed | stop to breakeven after +2.0R | `{'breakeven_at_r': 2.0}` |
| H5-RND | H5 | H5_auto | auto, diagnostic | NULL BENCHMARK: random choice among signalling pairs (5 seeds) | `{'auto_score': 'random'}` |
| H5-CALM | H5 | H5_auto | auto | calmest signalling pair first (lowest volatility percentile) | `{'auto_score': 'calm'}` |
| H5-MOM | H5 | H5_auto | auto | most momentum-aligned signalling pair first (72h return in ATR units) | `{'auto_score': 'momentum'}` |
| H5-COMP | H5 | H5_auto | auto | average rank of calm and momentum | `{'auto_score': 'composite'}` |

Diagnostics (`D-*`, `H5-RND`) are measured to understand the strategy or to provide a null benchmark. They can never be adopted.

## Survival screen (all must hold, development pairs, development period, base costs)

| Check | Rule |
|---|---|
| C1 | pooled expectancy improves by at least 0.03 R |
| C2 | bootstrap probability that the improvement is positive is at least 0.8 |
| C3 | not worse on at least 3 of the 4 development pairs |
| C4 | not worse in at least 60% of half-years that have 10+ baseline trades |
| C5 | keeps at least 50% of the baseline's trades |
| C6 | worst-pair maximum drawdown no more than 2.0 points above the baseline's |
| C7 | profit factor not below the baseline's |
| C8 | average R of the best 10% of trades at least 80% of the baseline's |
| C9 | out-of-sample (walk-forward test windows): pooled improvement above 0.0 and positive in at least 50% of windows |

Survivors then face: **C10** improvement of at least 0.02 R still holds at 1.5x and at 2x costs; **C11** on the six holdout pairs the
candidate is at least as good as the baseline pooled and not worse on at least 4 of 6 pairs; and the deflated Sharpe ratio
(deflated by all 33 trials). Tiers: `ROBUST` passes everything with a deflated Sharpe ratio of at least 0.95; `SUGGESTIVE` passes
C1-C11 but not the deflated Sharpe bar (worth a forward shadow test, not an adoption); anything else is rejected.

## Running it

```bash
cd ~/mba-research
git -C code fetch origin ccr-7642644c-2jb7jq && git -C code checkout -q ccr-7642644c-2jb7jq
export PYTHONPATH=$PWD/code/src
./venv/bin/python -m mba_bot.research phase1 plan            # prints the plan hash; it must read 6f29fad5f4cd6e33
nice -n 19 ionice -c3 ./venv/bin/python -m mba_bot.research phase1 run \
    --root data --out out/phase1 --baseline-json out/baseline.json --expect-data-fingerprint b60dd5b4e6c58013 --expect-plan-hash 6f29fad5f4cd6e33 2>&1 | tee phase1.log
```

Outputs in `out/phase1/`: `phase1_report.md`, `phase1_results.json`, `trials.jsonl` (the ledger), `tapes/` (cached signals, safe to delete).
Memory stays near 0.3 GB except while the Auto Select runs load four pairs (about 0.8 GB). Expect 30-60 minutes, most of it building signal tapes once.
