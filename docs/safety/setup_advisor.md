# Setup advisor

Ships behind `SETUP_ADVISOR` (default off). With it off the route does not exist (404), no account field is added and no screen changes.

## What it does

`GET` and `POST /api/account/advice` return a short ordered list of notes for the logged-in customer's own account: `id`, `severity` (`not_possible`, `warning`, `tip`), plain `text` with the real numbers,
an optional `action`, and the `tab` (trading or risk) it belongs on. `POST` takes the values the customer is about to save (the same field names as `PATCH /api/account`, none required), so the notes
update before Save. Bot Settings asks once, 500 ms after the customer stops editing. The dashboard shows only `not_possible` and `warning` notes, at most three.

It only informs. It never changes a setting, never blocks a save that is allowed today, never changes how the bot trades, makes no exchange call and returns no secret. There is no account parameter.
There is no owner-specific flag or default: every account goes through the same code.

## Same functions as enforcement

| Note | Comes from |
|---|---|
| `risk_refused` (not possible) | `risk_limits.risk_setting_problem`, the rule `PATCH /api/account` refuses with (range, then the acknowledgments) |
| `daily_refused` (not possible) | `daily_limit.validate_amount` |
| `slots_refused` (not possible) | `live_multi.slots_refusal`, the rule `PATCH /api/account` refuses with (switch off, Auto Select) |
| `pair_viability` (not possible / tip) | `small_capital.assess` on the cached exchange minimum |
| `size_cap_*` | `risk.compute_position_size`, `apply_exposure_ceiling`, `apply_margin_ceiling` at an assumed 1% stop |
| `daily_below_one_trade`, `daily_covers_n_trades`, `daily_levels` | `daily_limit.one_trade_can_exceed`, `total_stop_level_usd`, `pct_of` |
| `slots_pairs`, `slots_capital`, `open_risk` | `live_multi.live_pairs`, `free_capital`, `check_additional_entry` on modelled trades |
| `strategies*` | `multi_trade.account_strategies`, the strategy risk caps, the priority Donchian, EMA, RSI |
| `leverage`, `margin_not_isolated` | `leverage.account_cap`, `effective`, and the cached Binance read the dashboard already makes |
| `risk_band` | `risk_limits.band` |

`not_possible` is used only where enforcement itself refuses. Anything the bot judges at trade time with live prices is a `warning` or `tip` that states its assumption (a stop 1% away). The grid in
`tests/test_setup_advisor.py` runs each proposal through the advisor and through the real `PATCH /api/account` and requires the two to agree.

## Data it reads

The account row, the daily-limit and acknowledgment records, the cached exchange filters (`pair_catalog.cached_facts`, never fetched here) and the cached Binance leverage read
(`/api/account/binance-settings`, 60 s). If a value is not cached the note says it is unknown.

## Wording

All texts are in `src/mba_bot/setup_advice_texts.py`. They describe what a setting does and what the bot will or will not do. They never say what to buy or sell or when to trade, never predict and never
promise a loss limit. The full list for legal review is kept privately (lawyer question 7). Do not turn the switch on until the owner says so.

## Rollback

Set `SETUP_ADVISOR=0` (or remove it) and restart. No migration, nothing stored.
