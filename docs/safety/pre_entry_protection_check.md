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
  outcome `observed` and `would_block: true`, plus a journal line `[observe] ...`. It never blocks the entry, never
  alerts the customer, never sets the dashboard error. The decision-log row needs `TRADE_TELEMETRY` on; the journal line
  does not. Entries that pass also carry the check's numbers (`precheck`) on their `accepted` row, so predicted and
  actual protection outcomes can be compared.
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
| `protection_check_inputs_invalid` | a price, quantity or capital is missing, non-finite or non-positive; the stop is on the wrong side of entry; leverage is outside 1 to 125 (or a boolean, string, NaN, infinite); a setting is out of range; the check itself raised |
| `maintenance_tiers_unavailable` | the bracket read failed, a bracket lacks `cum`, the table is empty, has a gap or overlap, does not start at 0, has a rate outside 0 to 50% or a rate that falls with size, or no bracket contains `N` |
| `margin_mode_unknown` | Binance did not report isolated or cross (it is not assumed) |
| `cross_margin_unverifiable` | cross margin and the account holds any other open position, or the position list could not be read reliably |

Unreadable leverage was already refused earlier (`leverage_unreadable`).

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

## 2. Repeat-failure limit (`protection_guard`)

A protective stop that cannot be placed ends in an emergency exit. The cause is classified
(`lifecycle.classify_protection_failure`) and recorded in the trade's `close_reason`:

| Category | Examples | `close_reason` | Counted by the limit |
|---|---|---|---|
| structural | `ProtectionUnavailable` (unavailable, beyond liquidation), Binance `InvalidOrder`, `BadRequest`, `NotSupported` | `protection_failed_emergency_exit` (unchanged) | yes |
| transient | network error, timeout, exchange unavailable, rate limit | `protection_transient_emergency_exit` | no |
| other | anything else | `protection_unclassified_emergency_exit` | no |

The limit counts structural exits for the same account and pair, live only, inside `PROTECTION_FAIL_WINDOW_HOURS`
(`closed_at`, UTC). Historic rows keep the structural reason, including any that were in fact transient. When the count
reaches `PROTECTION_FAIL_LIMIT`, new entries on that pair are refused (enforce) or recorded (observe) before any exchange
call. It releases by itself when the oldest failure leaves the window. Open trades are never touched. A failing lookup
never blocks trading (it is a secondary guard). Per-account cycles are serialised by the scheduler lock and the
single-instance lock, so the count cannot change between its read and the entry.

The customer message on an emergency exit now names the cause (structural: "Binance cannot hold a protective stop ...
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
| `PROTECTION_FAIL_LIMIT_MODE` | `observe` | `off`, `observe`, `enforce` |
| `PRECHECK_MIN_MAINT_RATE` | 0.005 | Floor for the effective maintenance rate (0 to 0.2). |
| `PRECHECK_STOP_SAFETY_FRACTION` | 0.75 | Share of the liquidation distance the stop may use (0.1 to 1). |
| `PRECHECK_CROSS_COLLATERAL_FACTOR` | 0.9 | Share of capital counted as cross collateral (0.1 to 1). |
| `PROTECTION_FAIL_LIMIT` | 2 | Structural failures per account and pair before blocking. `0` disables. |
| `PROTECTION_FAIL_WINDOW_HOURS` | 24 | Window for the count. |
| `PROTECTION_ALERT_REPEAT_HOURS` | 6 | Minimum gap between identical customer alerts (in memory; one extra alert after a restart). |

No migration is needed (the limit reads existing `trades` columns; decision rows reuse `decision_log`). The previous
branch-only `PROTECTION_PRECHECK_ENABLED` and `PRECHECK_MAINT_MARGIN_RATE` no longer exist; neither was ever deployed.

## Suggested rollout (owner decides each step)

1. Deploy with the defaults (both `observe`). Trading behaviour is unchanged. Optionally set
   `PROTECTION_PRECHECK_MODE=observe` and `PROTECTION_FAIL_LIMIT_MODE=observe` explicitly.
2. Watch `decision_log` for `outcome = 'observed'` and the `precheck` numbers on accepted entries, and the journal for
   `[observe]` lines. Compare against real protection outcomes. Look for false refusals (an observed refusal on a trade
   that was then protected) and for read failures (`maintenance_tiers_unavailable`, `margin_mode_unknown`).
3. Move the limit to `enforce` first (it only acts after structural failures have actually happened), then the pre-check.
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
fail-closed path, failure classification, and the repeat limit. The shared fixtures (`tests/helpers_live.py` and three
test files) now run both guards ENFORCED, so every live-path test exercises them; the synthetic candles put the stop
about 52% from entry, which only passes at 1x leverage, so the fixtures use 1x.
