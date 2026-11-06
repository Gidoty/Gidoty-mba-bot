# Phase 3 registration: measured costs and the real trade ledger

Owner agreement: AGREED 2026-11-06

Status: REGISTERED on 2026-11-06, before any real ledger data has been exported or looked at. The agreement was recorded on the reviewer's recommendation, relayed in the owner's message of that date ("Owner agrees registration section 5 as written"). Sections 2 to 5 are unchanged from the DRAFT written before the agreement.
Rules hash (sections 2 to 5): 22ccc8e2aba3ff5ff6356cada08a63f8b54849be01c7a915223246a0f39ebccb
The hash is the SHA-256 of the text from the heading "2. Definitions" up to (not including) the heading "6. Procedure", trimmed. The report recomputes it; if the text differs, the agreement no longer applies and the verdict is withheld. Any later edit to sections 2 to 5 is a new registration with a new hash and a new agreement.

## 1. Question
After fees, slippage and funding that the exchange actually recorded, does the live EMA 20/50 + ATR strategy have a positive expectancy per trade? And do the research backtester's assumed costs match what real trades paid? A negative answer to either is an acceptable result.

## 2. Definitions (frozen)
* **Live normal trade**: a real-money trade not closed by a protection emergency exit.
* **Incident trade**: a live entry closed again by a protection emergency exit (`protection_failed_emergency_exit`, `protection_transient_emergency_exit`, `protection_unclassified_emergency_exit`). Reported separately as the cost of the incident, never as strategy performance.
* **Paper trade**: no real fees or slippage; never used for costs or edge.
* **Reconciled**: the exchange fills for the trade's symbol between 60 s before its recorded open and 120 s after its close net to flat (within 0.5%), match the recorded quantity (within 2%), the computed gross result agrees with the exchange's own realizedPnl (within max 0.02 USDT or 0.1%), and all commission was paid in USDT. Everything else is **unreconciled**: counted, listed, and excluded from every number.
* **Net R** = (gross from fills - commission + funding received) / planned risk, planned risk = quantity x |signal price - stop price|.
* Slippage is adverse-positive, measured against the signal price the bot captured (entry) or the stop price (stop exits).

## 3. Registered constants
```
N_MIN_VERDICT = 30
MIN_OBS_COST = 30
MIN_TRADES_HOLDOUT = 20
FIT_FRACTION = 0.6
```
* A statement about edge needs at least `N_MIN_VERDICT` reconciled live normal trades. Below it the report is descriptive only.
* A measured cost replaces an assumed one only with at least `MIN_OBS_COST` observations (per pair for pair-specific slippage). Otherwise the assumption stays and is labelled assumed.
* Costs are validated in time: fit on the earlier `FIT_FRACTION` of trades, compare with the later part; with fewer than `MIN_TRADES_HOLDOUT` in either part the report says "not validated".

## 4. Statistics and verdict labels
Mean net R with a 95% bootstrap interval (trades treated as independent; stated as optimistic). Wilson interval for the win rate. Labels: INSUFFICIENT SAMPLE (n below the minimum); POSITIVE (interval above 0); NEGATIVE (interval below 0); NO EDGE DETECTED (interval includes 0). No other label, no subgroup verdicts: pair and side tables are descriptive only.

## 5. What the owner does with each outcome (PROPOSED, awaiting the owner's agreement)
| Outcome | Action |
|---|---|
| POSITIVE | Keep live trading as it is. No production change follows from this report alone. Re-run the frozen Phase 2 arms with the measured costs as a separate registered step. |
| NO EDGE DETECTED or INSUFFICIENT SAMPLE | Keep the safety controls; add no features; do not increase risk per trade. Keep collecting and re-run when 30 more reconciled live normal trades exist. Owner decides whether new customers may start live trading in the meantime (recommended: tell them plainly that results are unproven). |
| NEGATIVE | Pause new live entries for the markets concerned (kill switch or per-account stop, owner's choice) until a registered change passes. Paper trading may continue. |
Whatever the outcome, the incident trades are reported as a separate cost, and the report never claims an edge it cannot support.

## 6. Procedure
1. The owner agrees section 5 (the line at the top reads `Owner agreement: AGREED` with the date). DONE 2026-11-06, before any real data was analysed. Editing sections 2 to 5 afterwards changes the rules hash and withholds the verdict.
2. The reviewer approves `ledger_export.py` and its checksum. The owner runs it as one supervised session; the bundle contains prices, quantities, fees, funding and times only. The owner checks the file's SHA-256 against the printed value and sends it.
3. `python -m mba_bot.research ledger --bundle <file>` writes `ledger_report.md` and `ledger_trades.csv`.
4. The report is read as written, including WITHHELD, INSUFFICIENT SAMPLE and NEGATIVE.
5. Only after a ledger result exists: re-run the frozen Phase 2 baseline and arms with the measured costs (a new registration that reuses the Phase 2 rules unchanged).

## 7. Limits
One market period. The live sample is small, and the incident trades reduce it further. Trades cluster in time. Fees paid in BNB are not converted. Slippage is against the bot's signal price, not tick-level mid. One account only.
