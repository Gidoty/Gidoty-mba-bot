# Pre-entry protection check and repeat-failure limit

Status: on a branch, not merged, not deployed. Nothing here changes a Binance account, leverage or margin mode.

## Why

On 2026-10-27 an account at 100x leverage on XRP/USDT opened 23 live trades in under two hours. Each one
was closed again within the same cycle by `protection_failed_emergency_exit`. A 2x ATR stop sits about 2% from
entry, but at 100x an isolated position is liquidated about 1% away, so the stop would be beyond liquidation.
`protection_precheck` (venue.py) correctly refuses such a stop, but it can only run after the entry has filled,
because Binance reports a liquidation price only for an open position. The same account's BNB trades (5x) were
protected. The next scheduler round re-entered because the forming-candle cross persisted.

## 1. Pre-entry check (`risk.assess_stop_viability`)

Runs for live Futures entries only, after sizing and before any order. Inputs are values the entry path already
has: side, entry price, stop price, quantity, the account's read-only Binance leverage for the symbol, the
effective capital, and the margin mode from the same `fetch_leverage` read (no extra API call).

    stop_distance        = |entry - stop| / entry
    isolated (default)   liquidation_distance = 1/leverage - maintenance_margin_rate
    cross                liquidation_distance = max(isolated, effective_capital / notional - maintenance_margin_rate)
    allowed              = safety_fraction * liquidation_distance
    enter only if        liquidation_distance > 0  and  stop_distance <= allowed      (equality passes)

Defaults: `maintenance_margin_rate` 1% (above Binance's lowest tiers, so it errs towards refusing),
`safety_fraction` 0.75 (the stop must lie within 75% of the estimated distance to liquidation).

| Leverage | Largest stop distance allowed (isolated) |
|---|---|
| 5x | 14.25% |
| 10x | 6.75% |
| 20x | 3.0% |
| 25x | 2.25% |
| 30x | 1.75% |
| 50x | 0.75% |
| 100x | none (refused) |

Assumptions and limits, stated plainly:
* An estimate. It ignores fees, funding, unrealised P&L of other positions and the tiered maintenance rate at
  large notionals. The 1% rate and the 0.75 fraction are the margin for that.
* Unknown or unreported margin mode is treated as isolated (the smaller distance).
* Cross margin: collateral is taken as `effective_capital` (the lower of the allocated capital and the free
  balance), not the whole wallet, and other open positions on the account are ignored.
* Any missing, non-finite, non-positive or wrong-side input fails closed with `protection_check_inputs_invalid`.
  A missing leverage was already refused earlier (`leverage_unreadable`).
* The existing post-entry `protection_precheck` (real liquidation price from Binance) is unchanged and still runs.

When the check refuses an entry: no order is sent; a `decision_log` row is written (stage `entry`, outcome
`rejected`, reason `stop_beyond_safe_liquidation_distance` or `protection_check_inputs_invalid`, detail: leverage,
side, stop distance %, estimated liquidation distance %, maximum allowed stop %, margin mode assumed, assumed
rate and fraction); the dashboard message (`last_error`) is set; the customer gets one alert per
account, pair and reason per `PROTECTION_ALERT_REPEAT_HOURS`. Decision-log rows need `TRADE_TELEMETRY` on (the
existing switch); the refusal itself does not.

## 2. Repeat-failure limit (`protection_guard`)

Counts live trades for the same account and pair that were closed with `protection_failed_emergency_exit`
inside the window. No schema change: it reads the `trades` table. When the count reaches the limit, new
entries on that pair are refused before any exchange call, with a `protection_failure_limit` decision row and the
same de-duplicated alert. It releases by itself when the oldest failure leaves the window; nothing is latched.
Existing open trades are never touched. A failing lookup never blocks trading. With the defaults an account can
take at most 2 failed round trips per pair per 24 hours.

## 3. Journal

`protective stop attempt N/M failed (ProtectionUnavailable: <reason>)`. The reason is one of this codebase's own
fixed strings, never exchange text.

## Settings (all optional; environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PROTECTION_PRECHECK_ENABLED` | 1 (on) | Pre-entry check. `0` restores the previous behaviour. |
| `PRECHECK_MAINT_MARGIN_RATE` | 0.01 | Assumed maintenance-margin rate (0 to 0.2). |
| `PRECHECK_STOP_SAFETY_FRACTION` | 0.75 | Share of the liquidation distance the stop may use (0.1 to 1). |
| `PROTECTION_FAIL_LIMIT` | 2 | Failures per account and pair before blocking. `0` disables. |
| `PROTECTION_FAIL_WINDOW_HOURS` | 24 | Window for the count. |
| `PROTECTION_ALERT_REPEAT_HOURS` | 6 | Minimum gap between identical customer alerts (in memory; one extra alert after a restart). |

## Default behaviour change to be aware of

Unlike the earlier switches, the check and the limit are ON by default: they only refuse entries that the
existing post-entry check would have closed again anyway, or that repeat a failure twice already. An account whose
leverage is high enough that the estimate refuses a stop that Binance would in fact have accepted (for example
50x with a 1.5% stop) will stop trading that pair until its leverage is lowered or the two assumptions above are
relaxed. Deploying means setting nothing; to deploy dark, set `PROTECTION_PRECHECK_ENABLED=0` and
`PROTECTION_FAIL_LIMIT=0` first, then enable one at a time.

## Tests

`tests/test_pre_entry_protection.py` (44 tests): the calculation, boundaries, short and long symmetry,
cross margin, unknown margin mode, invalid inputs; refusal before any order with decision row and a single
alert; viable entry untouched; switch off; spot unaffected; the failure limit (below, at, outside window,
other pair, other reason, paper, disabled, end to end from a stop beyond liquidation, failing lookup); alert
repeat window; journal reason text.
