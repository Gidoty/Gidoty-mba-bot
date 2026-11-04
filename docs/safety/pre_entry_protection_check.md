# Pre-entry protection check and repeat-failure limit

Status: on a branch, not merged, not deployed. Nothing here changes a Binance account, leverage or margin mode;
every exchange call the guards make is a read.

## Why

On 2026-10-27 an account at 100x leverage on XRP/USDT opened 23 live trades in under two hours. Each was closed again
within the same cycle by `protection_failed_emergency_exit`. A 2x ATR stop sits about 2% from entry, but at 100x an
isolated position is liquidated about 1% away, so the stop would be beyond liquidation. The existing post-entry
`protection_precheck` (venue.py) correctly refuses such a stop, but it can only run after the entry has filled, because
Binance reports a liquidation price only for an open position. The same account's BNB trades (5x) were protected. The
next scheduler round re-entered because the forming-candle cross persisted.

## The three modes (each guard has its own switch)

| Guard | Variable | Values | Default |
|---|---|---|---|
| Pre-entry check | `PROTECTION_PRECHECK_MODE` | `off`, `observe`, `enforce` | `observe` |
| Repeat-failure limit | `PROTECTION_FAIL_LIMIT_MODE` | `off`, `observe`, `enforce` | `observe` |

* `off`: nothing is evaluated, read or logged.
* `observe`: evaluates exactly as `enforce` would, and records what it WOULD have refused: a `decision_log` row with
  outcome `observed` and `would_block: true` (needs `TRADE_TELEMETRY` on), plus a journal line. It never blocks the
  entry, never alerts the customer, never sets the dashboard error. Journal output is deliberately low-volume and does
  not depend on telemetry:
  * a would-be refusal is written at WARNING as `[observe] ...`, at most once per `PROTECTION_ALERT_REPEAT_HOURS`
    (6 h) for the same account, pair and reason, however many 5-minute rounds the signal persists (the decision-log
    rows, one per candle, are the complete record);
  * a PASS is written at INFO as `[observe] protection pre-check passes ...` with leverage, stop distance, allowed
    distance, bracket, effective maintenance rate and margin mode. A pass leads to an entry, so this is about one line
    per trade (at most one per hour for the same account and pair). The accepted decision row carries the same numbers
    when telemetry is on. Enforce mode writes no pass line.
* `enforce`: refuses the entry before any order, writes a `rejected` decision row, sets the dashboard message, and
  alerts the customer once per account, pair and reason per `PROTECTION_ALERT_REPEAT_HOURS`.

`1`, `true`, `yes`, `on` mean `enforce`; `0`, `false`, `no` mean `off`. Unset or empty is `observe`. An unrecognised value
is treated as `observe` and logged at startup (never silently off, never enforce).

Observe does not stop a loop: an account that is already looping (high leverage, stop beyond liquidation) keeps
opening and closing trades while its guard only observes. Keep such an account inactive, or run the limit in `enforce`.

## 1. Pre-entry check (`risk.assess_stop_viability`)

Runs for live Futures entries only, after sizing and before any order. It reads, fresh on every entry attempt (nothing
is cached by this code, so data is never staler than that call):

* leverage and margin mode: the existing `fetch_leverage` read (symbol config);
* the symbol's leverage brackets: `fetch_leverage_tiers` (notional floor and cap, maintenance-margin rate, maintenance
  amount `cum`), asked for the one symbol;
* only when the margin mode is cross: the account-wide position list (`fetch_positions` with no symbol).

Calculation (single position, one-way mode, USDT-margined), with `N = quantity * entry`, the bracket whose range
contains `N` (rate `r`, amount `cum`) and the effective maintenance rate `t = max(r - cum / N, PRECHECK_MIN_MAINT_RATE)`:

    margin ratio m   isolated:  1 / leverage
                     cross:     max(1 / leverage, PRECHECK_CROSS_COLLATERAL_FACTOR * effective_capital / N)
    distance         long:  m - t          short:  (m - t) / (1 + r)
    enter only if    distance > 0  and  |entry - stop| / entry  <=  PRECHECK_STOP_SAFETY_FRACTION * distance   (equality passes)

Largest stop distance allowed with the defaults (flat 0.4% bracket, so `t` is the 0.5% floor), isolated, long:

| Leverage | Largest stop |
|---|---|
| 5x | 14.6% |
| 10x | 7.1% |
| 20x | 3.4% |
| 25x | 2.6% |
| 30x | 2.1% |
| 50x | 1.1% |
| 100x | 0.38% |
| 125x | 0.23% |

Brackets that matter use the maintenance amount: at 100,000 notional in a 0.5% bracket with `cum` 50, `t` is 0.45%, not 0.5%.
A notional exactly on a bracket boundary takes the worse of the two brackets.

### What fails closed (entry refused, specific reason code)

| Reason code | When |
|---|---|
| `stop_beyond_safe_liquidation_distance` | the stop is not inside `PRECHECK_STOP_SAFETY_FRACTION` of the estimated distance, or the distance is not positive |
| `protection_check_inputs_invalid` | a price, quantity or capital is missing, non-finite or non-positive; the stop is on the wrong side of entry; leverage is outside 1 to `PRECHECK_MAX_LEVERAGE` (125), or is a boolean, string, NaN or infinite; a setting is out of range; the check itself raised |
| `maintenance_tiers_unavailable` | the bracket read failed, a bracket lacks `cum`, the table is empty, has a gap or overlap, does not start at 0, has a rate outside 0 to 50% or a rate that falls with size, or no bracket contains `N` |
| `margin_mode_unknown` | Binance did not report isolated or cross (it is not assumed) |
| `cross_margin_unverifiable` | cross margin and the account holds any other open position, or the position list could not be read reliably |

Unreadable leverage was already refused earlier (`leverage_unreadable`): the leverage READ rejects only values that are
not a leverage at all (booleans, non-numbers, non-finite, zero or negative). It does NOT apply the 1 to 125 range, so
with both guards `off` the read behaves exactly as it did before the guards existed. The plausible range is applied by
the pre-check alone, so it follows the guard's mode: `enforce` refuses an implausible leverage
(`protection_check_inputs_invalid`), `observe` records it and trades, `off` ignores it. The maximum is the setting
`PRECHECK_MAX_LEVERAGE` (default 125, clamped to 1 to 1000), so an exchange that raises its limit needs a setting
change, not a code change. 125 is the current Binance maximum as far as the authors know; that is not verified here.

### Assumptions that remain (not verified against an official Binance source)

* The liquidation formula is the standard single-position isolated formula derived from the maintenance-margin model
  (maintenance margin = notional x rate - maintenance amount). It was not checked against an official Binance page: the
  development environment could not reach binance.com. Treat it as an estimate.
* For a long the exact denominator would be `(1 - r)`; it is dropped, which makes the distance smaller (safer). For a
  short `(1 + r)` is applied.
* Fees, funding, the liquidation clearance fee and unrealised P&L are ignored. `PRECHECK_MIN_MAINT_RATE` (0.5%) and
  `PRECHECK_STOP_SAFETY_FRACTION` (0.75) are the margin for that; both are judgement, not exchange rules.
* The bracket field names (`notionalFloor`, `notionalCap`, `maintMarginRatio`, `cum`) and the symbol-config
  `marginType` come from the pinned ccxt 4.5.88 sources and its documented samples. Re-run `tests/test_ccxt_contract.py`
  and this file's tests before any ccxt upgrade.
* Cross margin: collateral is `effective_capital` (the lower of allocated capital and free balance) at
  `PRECHECK_CROSS_COLLATERAL_FACTOR` (0.9). Refused whenever another position exists. Open orders that reserve margin
  are not counted.
* One-way position mode only (hedge mode, multi-assets and portfolio margin are refused earlier by the account-mode
  checks).
* The existing post-entry `protection_precheck` (the real liquidation price from Binance) is unchanged and still runs.

## 2. Repeat-failure limits (`protection_guard`)

A protective stop that cannot be established ends in an emergency exit. The cause is classified
(`lifecycle.classify_protection_failure`) and recorded in the trade's `close_reason`. The classification is reset at the
start of every protection attempt, so the category used for the exit can only come from the attempt that failed last,
never from an earlier attempt, trade or call on the same context.

| Category | Examples | `close_reason` | Limit | Scope | Window |
|---|---|---|---|---|---|
| structural | `ProtectionUnavailable` (unavailable, beyond liquidation), Binance `InvalidOrder`, `BadRequest`, `NotSupported` | `protection_failed_emergency_exit` (unchanged) | 2 | account and pair | 24 h |
| transient | network error, timeout, exchange unavailable, rate limit | `protection_transient_emergency_exit` | 3 | whole account (any pair) | 6 h |
| unclassified | anything else, including Binance rejections that ccxt does not map to a specific error | `protection_unclassified_emergency_exit` | 3 | account and pair | 24 h |

All three share the one switch `PROTECTION_FAIL_LIMIT_MODE` (`off`, `observe`, `enforce`); each has its own count and
window, and a limit of 0 turns that one off. Each count uses exactly its own `close_reason` string, live trades only,
`closed_at` inside the window (UTC). When a limit is reached, new entries in its scope are refused (enforce) or recorded
(observe) before any exchange call, with the reason codes `protection_failure_limit`,
`transient_protection_failure_limit` and `unclassified_protection_failure_limit`. If several are reached, the structural
one is reported first. Each releases by itself when the oldest counted exit leaves its window; nothing is latched, and
open trades are never touched. A failing lookup of one category never blocks trading and never hides the others.

Why these numbers (judgement, not exchange facts):

* **Structural, 2 per account and pair per 24 h.** The cause repeats identically (a stop beyond liquidation, a
  rejected order type), so the third attempt is predicted to fail. Two failures cost two unprotected round trips of a
  few seconds each; this is the limit that stops the incident loop (23 trades) after 2.
* **Transient, 3 per account per 6 h.** An exit already means three consecutive failed stop attempts inside one cycle,
  so one is not an ordinary blip, but a single bad minute can still produce it. A third within 6 hours means the
  connection or the exchange is not reliable enough to open positions that must be protected. It is scoped to the
  whole account because the cause is the connection, not the pair. The window is short because such trouble usually
  passes: the worst false-positive lockout is a pause of at most 6 hours after the oldest counted exit, during which
  open positions are still managed and only NEW entries wait. During a real outage that pause is the desired outcome.
* **Unclassified, 3 per account and pair per 24 h.** The cause is unknown, so the bar is slightly higher than for
  structural. It is scoped to the pair because an unmapped Binance rejection is normally tied to one symbol, and
  an account-wide scope would lock unrelated pairs. Residual risk: an unknown account-wide fault is counted pair by
  pair, so on Auto Select it can cost up to 3 exits on each of the four pairs per day before all are blocked.

Known gaps: historic rows with the structural reason can include earlier transient failures (the categories did not
exist), but only rows inside the 24 h window count, so any such effect ends within 24 h of deployment. The classification
of a Binance rejection depends on ccxt's error mapping; a rejection ccxt does not map is "unclassified".

The customer message on an emergency exit names the cause (structural: "Binance cannot hold a protective stop ...
(<reason>)"; transient: "a temporary connection or exchange problem, not a problem with your settings"; other: "could not
be confirmed"). The journal line reads
`protective stop attempt N/M failed (ProtectionUnavailable: <reason>) [structural]`; the reason is one of this codebase's
own fixed strings, never exchange text.

## Units

Every distance in the decision log's `detail` is a FRACTION of the entry price (`0.02` = 2%), with keys ending
`_frac`. `trade_telemetry.stop_distance_pct` is the same fraction under an older name (renaming a production column
needs a migration, which is not worth it; the name is documented here instead). Customer messages show percent.

## Settings (all optional; environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PROTECTION_PRECHECK_MODE` | `observe` | `off`, `observe`, `enforce` |
| `PROTECTION_FAIL_LIMIT_MODE` | `observe` | `off`, `observe`, `enforce` (all three limits) |
| `PRECHECK_MIN_MAINT_RATE` | 0.005 | Floor for the effective maintenance rate (0 to 0.2). |
| `PRECHECK_STOP_SAFETY_FRACTION` | 0.75 | Share of the liquidation distance the stop may use (0.1 to 1). |
| `PRECHECK_CROSS_COLLATERAL_FACTOR` | 0.9 | Share of capital counted as cross collateral (0.1 to 1). |
| `PRECHECK_MAX_LEVERAGE` | 125 | Highest leverage the pre-check treats as plausible (1 to 1000). |
| `PROTECTION_FAIL_LIMIT` | 2 | Structural failures per account and pair before blocking. `0` disables. |
| `PROTECTION_FAIL_WINDOW_HOURS` | 24 | Window for the structural count. |
| `PROTECTION_TRANSIENT_LIMIT` | 3 | Transient exits per account (any pair) before blocking. `0` disables. |
| `PROTECTION_TRANSIENT_WINDOW_HOURS` | 6 | Window for the transient count. |
| `PROTECTION_UNCLASSIFIED_LIMIT` | 3 | Unclassified exits per account and pair before blocking. `0` disables. |
| `PROTECTION_UNCLASSIFIED_WINDOW_HOURS` | 24 | Window for the unclassified count. |
| `PROTECTION_ALERT_REPEAT_HOURS` | 6 | Minimum gap between identical customer alerts (in memory; one extra alert after a restart). |

No migration is needed (the limit reads existing `trades` columns; decision rows reuse `decision_log`). The previous
branch-only `PROTECTION_PRECHECK_ENABLED` and `PRECHECK_MAINT_MARGIN_RATE` no longer exist; neither was ever deployed.

## Frontend (rebuild required)

`frontend/src/constants.js` (`closeReasonLabel`) maps `close_reason` to the customer's Trade History wording and falls
back to "Signal exit". The two new reasons now have explicit labels (`protection_transient_emergency_exit` shows "Safety
exit (connection issue)", `protection_unclassified_emergency_exit` shows "Safety exit"), and a test fails if any
emergency-close reason lacks one. `frontend/dist/` is not tracked in git, so the change reaches customers only after the
frontend is rebuilt on the server (`cd frontend && npm run build`) as part of deployment; the Python service restart alone
does not update it. `quantity_unverifiable`, an older emergency-close reason that used to fall through to "Signal exit", is now labelled "Safety exit" too.

## Verification gates that remain OPEN

These are not resolved by anything on this branch and must be closed before the corresponding mode is enforced:

1. **The Binance liquidation formula.** The calculation is the standard single-position isolated formula derived from the
   maintenance-margin model; no official Binance source could be reached to confirm it, including the fee and funding
   terms and the exact bracket boundary rule. The 0.5% floor and the 0.75 fraction are judgement.
2. **Live response shapes.** The bracket call is made with `{"symbol": <id>}` and the code requires `cum` in every
   bracket; the cross-margin check uses the account-wide position list. Both are written against ccxt 4.5.88's
   documented samples, never against a live response. If the real shapes differ, enforce mode would refuse every futures
   entry (`maintenance_tiers_unavailable` or `cross_margin_unverifiable`): safe, but a trading outage.
3. **Margin mode and leverage fields** (`marginType`, `leverage`) of the symbol-config read, same caveat.

Observe mode is how gates 2 and 3 are closed in practice: real entry attempts will show real tiers, `cum` and margin
modes in the `precheck` numbers (decision log or `[observe]` journal lines) before anything is enforced.

## Suggested rollout (owner decides each step)

1. Deploy with the defaults (both modes `observe`) and rebuild the frontend. Trading behaviour is unchanged. Keep
   `TRADE_TELEMETRY` on so the decision log is complete. An account that is already looping stays exposed in observe;
   keep it inactive, or enforce the limits first (below).
2. Watch `decision_log` (`outcome = 'observed'`) and the journal (`[observe]` lines, including the pass lines) over
   several real entries. Look for read failures (`maintenance_tiers_unavailable`, `margin_mode_unknown`,
   `cross_margin_unverifiable`), implausible `cum` or rates, and observed refusals on trades that were then protected.
3. Move `PROTECTION_FAIL_LIMIT_MODE` to `enforce` first (it only acts after exits have really happened), then
   `PROTECTION_PRECHECK_MODE` once gates 1 to 3 are closed.
4. To go back at any point, set the mode to `observe` or `off` and restart the service.

Retrospective check with no deployment (read-only; isolated margin, flat 0.4% bracket, so approximate; units are
fractions):

    sqlite3 -header -column 'file:/opt/mba-bot/mba_bot.db?mode=ro' "
    SELECT pair, account_leverage AS lev, ROUND(stop_distance_pct*100,2) AS stop_pct, COUNT(*) AS n,
      CASE WHEN account_leverage IS NULL OR account_leverage<1 OR account_leverage>125 OR stop_distance_pct IS NULL THEN 'REFUSE(inputs)'
           WHEN 1.0/account_leverage-0.005>0 AND stop_distance_pct <= 0.75*(1.0/account_leverage-0.005)+1e-12 THEN 'allow'
           ELSE 'REFUSE' END AS verdict
    FROM trade_telemetry WHERE mode='live'
    GROUP BY pair, account_leverage, ROUND(stop_distance_pct,3);"

## Tests

`tests/test_pre_entry_protection.py` covers the calculation (tiers, `cum`, boundaries, short, cross, invalid inputs),
Binance-shaped payloads parsed by the real ccxt code, configuration, the three modes, pre-order refusal for every
fail-closed path, failure classification and its per-attempt reset, all three repeat limits (thresholds, scopes,
windows, modes, independence, release), the leverage-range behaviour per mode, observe-mode journal volume, and the
frontend labels. The margin-ceiling test in `tests/test_capital_lifecycle_and_auto_select_safety.py` now uses candles
whose risk-sized position exceeds the ceiling at 1x and 2x, so it fails if the ceiling stops binding. The shared
fixtures run both guards ENFORCED, so every live-path test exercises them; their synthetic candles put the stop about
52% from entry, which only passes at 1x leverage, so the fixtures use 1x.
