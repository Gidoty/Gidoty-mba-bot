# Phase 2 pre-registration

**Plan hash `a3a0ff0672445ea7`**
**Code fingerprint `c39cd20e12619f0b`** (SHA-256 prefix over `engine.py`, `tape.py`, `data.py`, `metrics.py`, `analysis.py`, `confirm_stats.py`, `phase1.py`, `phase2.py`, `validation.py`, `reporting.py`)
**Code commit `6e13d5b`** on branch `ccr-7642644c-2jb7jq`. The Phase 1 registration (`6f29fad5f4cd6e33`) is unchanged.

This document was committed before the older history was downloaded or examined. `phase2 download` and `phase2 run` refuse to start if the
plan hash, the code fingerprint or the SHA-256 of `phase1_results.json` differs from the value the operator passes in. Any change to an arm,
a statistic, a threshold or a fingerprinted file produces a different hash, and a different hash is a different experiment with a new entry
here. Nothing in this file is edited after a result is seen.

## Question

Do the Phase 1 near-misses (V67, SM10, XC), and their combination, improve the after-cost result of the current strategy on data that
played no part in choosing them? Does halving the risk on shorts improve the account-level risk profile? Does Auto Select's ranking
beat a random choice among the pairs that signal together? A negative answer to any of these is an acceptable result.

## What is frozen

| Arm | Role | Rule (research policy) | Phase 1 counterpart |
|---|---|---|---|
| BASE | baseline | current production behaviour | |
| V67 | primary, P2-H1 | skip entries above the pair's own 67th volatility percentile (trailing 90 days) | H2-V67 |
| SM10 | primary, P2-H2 | shorts need an EMA gap of at least 0.10 ATR; longs unchanged | H3-SM10 |
| XC | primary, P2-H3 | act on the opposite crossover only when the hourly candle closes; stops stay exchange-native | H4-XC |
| COMBO | primary, P2-H4 | V67 + SM10 + XC together | none (never run) |
| SHORT_HALF | sizing, P2-H5 | shorts are sized at 0.5 of the normal risk; entries and exits unchanged | none |
| V80 | sensitivity only | skip entries above the 80th volatility percentile | H2-V80 |
| NOSHORT | diagnostic only | no shorts (measures what shorts contribute) | D-NOSHORT |
| AUTO | P2-H6 | production Auto ranking against random ranking, 20 seeds (1 to 20), four development pairs, one position | H5-RND |

No arm is added, removed, retuned or substituted after the older-history results are seen. The cost-ranking Auto arm was dropped before
registration and has no replacement. V80 and NOSHORT are reported and never judged. `SHORT_HALF` exists only in the research engine
(`Policy.short_risk_scale`); production is not changed by it.

Every fixed-pair arm runs in the same single-pair account setup the Phase 1 runner used: one position, 1000 allocated capital, 1% risk, 2% cap, 3x
notional cap, 5x leverage, replay of the production 5-minute polling. Costs are the Phase 0/1 costs: 5 bp taker fee, 3 bp market slippage,
8 bp stop slippage (BTC 1/3 bp, ETH 1.5/4 bp), funding applied. Funding is not scaled by the cost multiple.

## Data

| Segment | Pairs | Period | Role |
|---|---|---|---|
| A | BTC, ETH, BNB, XRP | older history, from the first available day up to the first moment of the Phase 1 store | PRIMARY. Time out-of-sample. |
| B | SOL, DOGE, SUI, LINK, ADA, LTC | Phase 1 store, development period only (the lockbox is excluded) | Asset out-of-sample. Not a clean holdout (see Deviation). |
| C | the same six | older history (pairs with under 30 days of history there are skipped) | Time and asset out-of-sample. |

Datasets consumed by the run: (1) the older-history store, 1-hour and 5-minute klines plus funding for the ten pairs; (2) the Phase 1 store
for segment B; (3) `phase1_results.json`, pinned by SHA-256, from which the runner reads the Phase 1 improvements, the development range and
the lockbox range. Public market data only. The Phase 1 lockbox stays sealed: segment B stops at the end of the development range, and the
runner refuses if that range overlaps the lockbox. Segments A and C stop where the Phase 1 store begins.

The older history is one-shot evidence. The runner writes `PHASE2_CONSUMED.json` into the older store with this plan hash and fingerprint. The
same frozen plan may be re-run (a crashed run is the same test and gives the same answer). A different plan or different code is refused:
it would be a second look.

## Statistics

* **Paired block bootstrap.** For each arm against BASE, trades of both arms are placed in 14-day calendar blocks. Blocks are resampled with
  replacement (5000 resamples, seed 7) and both arms' mean net R are recomputed on the same blocks. This keeps the overlap between a filter's
  trades and the baseline's, the clustering of pairs in time, and the heavy tail. Reported: the difference in mean R, an 80% interval (10th to
  90th percentile), and the one-sided p-value (share of resamples at or below zero). The Phase 1 test (two independent samples, judged by a probability-of-improvement threshold) was
  underpowered for this kind of data and ignored the overlap; this replaces it. It is not a looser standard: the arm must also clear K2.
* **Holm adjustment** over the four primary arms (V67, SM10, XC, COMBO).
* **Design effect and effective sample size** of the baseline, to show how much clustering in time reduces the information in the trade count.
* **Heavy-tail views:** trimmed mean, winsorised mean, mean without the top 5% of trades, count of 3R-plus trades, top-decile mean.
* **Cost break-even:** expectancy at 0.5x, 1.0x, 2.0x, 3.0x costs for BASE, V67, SM10, XC, COMBO, a straight-line fit, and the cost multiple at
  which the edge disappears.
* **Regimes:** BTC trailing 90-day return above +25% is bull, below -25% is bear, otherwise range; computed causally at each entry.
* **Event days:** the top 1% of BTC absolute daily moves in the segment. Trades overlapping those days are removed from both arms.
* **Kept versus removed:** for the pure entry filters (V67, SM10), baseline trades are matched by (pair, side, entry time) and split into kept
  and removed, with their means and a paired bootstrap of the gap.
* **Account level (SHORT_HALF):** total return, maximum drawdown, ulcer index and return/drawdown, per pair, averaged over pairs.
* **Auto Select:** share of entry decisions with two or more signalling pairs and which pairs; an oracle bound (at each such decision, the R each
  signalling pair's own single-pair run earned at that moment: chosen, average of the candidates, best of the candidates); production ranking
  against 20 random seeds at account level.

## Pass and fail rules

Primary arms (segment A unless stated). Every applicable criterion must hold for PASS.

| ID | Criterion |
|---|---|
| K0 | the arm's own net expectancy is above 0 |
| K1 | mean R improves on BASE (difference above 0) |
| K2 | Holm-adjusted one-sided p-value is 0.10 or lower |
| K3 | the improvement is at least 50% of the Phase 1 improvement for the same arm (not applicable to COMBO, which has no Phase 1 value) |
| K4 | at least 85% of the 3R-plus trades and at least 85% of the top-decile mean R are kept |
| K5 | the worst pair's maximum drawdown is at most 110% of the baseline's |
| K6 | the arm beats BASE on at least 3 of the 4 pairs |
| K7 | the difference is above 0 in segment B pooled, and in segment C pooled when C has at least 100 baseline trades |
| K8 | among regimes with at least 30 baseline trades (at least two needed), at least two show a positive difference and none is below -0.10 R |
| K9 | still better than BASE at 2.0x costs |
| K10 | still better than BASE after removing event-day trades |

Verdict: **PASS** if no applicable criterion fails. **FAIL** if K1, K3 or K7 fails (the direction or the size of the Phase 1 effect did not
replicate). **INCONCLUSIVE** otherwise. A criterion that cannot be measured is "not applicable" and is shown as such in the report.

Sizing arm SHORT_HALF (segments A and B): S1 return/drawdown not lower than BASE; S2 ulcer index lower than BASE; S3 gives up no more than 25%
of BASE's mean return. PASS if all hold, FAIL if any fails.

Auto Select: production ranking "adds value" if its mean R is above the 90th percentile of the 20 random seeds. This is a description of the
ranking, not a candidate for adoption; no replacement ranking is proposed.

Sensitivities that are reported and never judged: V80, NOSHORT, 3.0x costs, event-day exclusion for the baseline, per-regime tables.

## Interpretation fixed in advance

* PASS means the arm survived a harder test than Phase 1. It is not permission to change production. **No candidate reaches production from
  historical confirmation alone.** A forward shadow period (decisions recorded, nothing traded) is still required, then a separate review.
* INCONCLUSIVE does not become PASS by running more of the same older history, adding arms or loosening a threshold.
* If BASE itself is unprofitable on the older history, K0 fails for every arm and that is itself the finding about the strategy.
* With the baseline break-even near 1.4x costs, a cost model that is too kind could make every arm look good. K9 and the 3x sensitivity are
  there to expose that.

## Limitations that apply to every number

* **Survivor bias.** Only contracts that still trade today are in the data. Delisted or failed contracts are absent, which flatters any
  long-biased result and hides tail events.
* **Market structure.** Liquidity, participation, spreads, market-maker behaviour and regime mix in the older years differ from today's.
* **Fees, funding and contract specifications changed over time.** The runner applies today's fee tier, today's tick/step/min-notional rules
  and recorded historical funding to every period. Historical rule changes (leverage brackets, minimum notional, tick sizes) are not modelled.
* **Extreme-event slippage cannot be recovered from candles.** Fixed 8 bp stop slippage understates the cost of a gap-through. The 3x cost
  multiple and event-day exclusion are sensitivities, not a model of those events.
* **Exchange availability.** Pairs listed later have shorter or no older history; segment C is thinner than A.
* **The strategy was not designed on this data, but the hypotheses were suggested by a full-sample baseline that included months and pairs
  outside the Phase 1 development set.** The only fully clean evidence is data that does not exist yet.

## Deviation (disclosed)

The six added pairs were set aside in Phase 1 as holdouts. Their baseline and descriptive results (side, volatility, ADX and exit tables) were
already observed in Phase 0 and Phase 1; no Phase 1 candidate was run on them. They are used here as asset out-of-sample confirmation for the
frozen arms, approved by the project owner. After this test they are no longer clean holdouts for these hypotheses. The look ledger
(`look_ledger.jsonl`) records this, the retrospective Phase 0/1 looks, and the sealed lockbox.

## Procedure

1. `python -m mba_bot.research phase2 plan` prints the plan hash and code fingerprint. They must equal the values at the top of this file.
2. `phase2 download --expect-plan-hash a3a0ff0672445ea7 --expect-phase1-sha256 <sha>` fetches the older range into a separate store. Public
   endpoints only. Nothing is analysed by the download.
3. `phase2 run --expect-plan-hash a3a0ff0672445ea7 --expect-code-fingerprint c39cd20e12619f0b --expect-phase1-sha256 <sha>` runs the data-quality
   gate, claims the older store, runs the arms, and writes `phase2_results.json`, `phase2_report.md`, `phase2_trades.csv` and `look_ledger.jsonl`.
4. Results are reported as they are, including FAIL and INCONCLUSIVE, with the limitations above.

Neither command is run until the project owner has reviewed this registration and authorised it.
