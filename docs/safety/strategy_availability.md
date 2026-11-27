# Strategy availability controls, and the setup advisor's candle-size preview

Status: built, everything OFF or EMA-only by default. **No default changes**: with nothing set, every account trades exactly as before, no customer can choose a strategy, and a new account is created exactly as before. **Depends on the operator-settings and admin-actions PR** (branch `feature/settings-table-admin-actions`): the new settings sit in its table and the admin override uses its step-up and change log. **No migration.**

## What is controlled
| Setting (environment name, also editable in the admin settings table) | Default | Meaning |
|---|---|---|
| `CUSTOMER_STRATEGY_CHOICE` | off | Customers may choose among the available strategies (`PUT /api/account/strategies`). |
| `STRATEGY_DONCHIAN_AVAILABLE`, `STRATEGY_RSI_AVAILABLE` | off | That strategy may be chosen by customers. EMA is always available. |
| `NEW_ACCOUNT_STRATEGIES` | `ema_only` | What a NEW account starts with: `ema_only`, `ema_donchian`, `ema_rsi`, `ema_donchian_rsi`. Only strategies that are available are applied; a typo is `ema_only`. |
All four are on the operator-settings allowlist (apply on the next request, no restart); the first three are shown to customers, the fourth is not.

**What availability does NOT do.** It never switches a strategy on for trading. A strategy trades for an account only when the account is opted in (`extra_strategies`, as before) AND the operator's live switch is on (`LIVE_RSI_MR`, `LIVE_DONCHIAN`, unchanged). Changing availability never touches an account that is already set up; an account's open trades are managed by their own strategy whatever the settings say (a test checks that `trader.py` and `scheduler.py` do not even import the module). The customer list shows `trades_live_now` per strategy, so a customer can see a strategy that is available but not running live on this service.

## Routes
* Customer: `GET /api/account/strategies` (available, selected, whether choosing is on, whether each strategy trades live now) and `PUT /api/account/strategies {"strategies": [...]}`. The PUT is 403 while choosing is off, refuses unknown, repeated or unavailable ids (422) and refuses while any trade of the account is open (409). It writes only `trading_accounts.extra_strategies`.
* Admin override: `POST /api/admin/customers/{ref}/strategies {strategies, reason, code}` (step-up, reason, change-log row as every admin action): any known strategy whatever is available to customers, same open-trade refusal. EMA is implied.
* New accounts: `extra_strategies` is set to the configured default (only available strategies), else left empty.

## The advisor's candle-size preview
The setup advisor (`SETUP_ADVISOR`) accepts an optional `ema_timeframe` (`1h`, `4h`, `1d`) in `POST /api/account/advice`; the Bot Settings screen has a "What if the EMA strategy used longer candles?" box. **It is a preview only: the candle size (`TIMEFRAME`) is one setting for the whole service, set by the operator; no per-customer candle size exists or is saved by this.** Without the field, nothing changes. For the chosen size it says, with every assumption stated in the note:
1. **Would the protection check refuse the stop?** It runs the bot's own `risk.assess_stop_viability` on a stop at the assumed distance (1% on 1h, scaled with the square root of the candle length: 2% on 4h, 4.9% on 1d), with the leverage set on Binance for the pair when that is known (cached read), else the customer's leverage limit, and the lowest maintenance rate the bot allows (the live check reads Binance's own bracket table and can be tighter). It states the widest stop the check would allow. Spot has no liquidation, so the note says the leverage check does not apply.
2. **Minimum order size.** The order the bot would size at the customer's risk, capital and that stop, against the cached exchange minimum for the pair; "the bot would not be able to open it" when it is under. A wider stop means a smaller position for the same risk, which is why small capital hits the minimum first on long candles.
3. **How few trades a year.** About 2 crossings per 100 candles, which is the frequency of the EMA 20/50 crossover on a RANDOM price series (a property of the indicator, recomputed by a test on a fresh random series, not market data), so about 175, 44 and 7 a year on one pair for 1h, 4h and 1d. The note says the flat-market filter and the single open trade make the real number lower and that real markets can differ by a factor of two or more. It is a count of signals, not a forecast of results.
The wording follows the advisor's rules (no "should", no promises) and lives in `setup_advice_texts.py`.

## Not built, and open
* Saving a per-customer candle size. The trading code reads one global `TIMEFRAME` in many places; making it per account is a larger change to how candles are fetched and how every candle-counted setting is read, and it was not part of this request's defaults. Until then the preview answers "what would it look like", the operator decides for everyone.
* A customer-facing screen to choose strategies (the API exists; no UI was added for it).
* The assumed stop distances are assumptions, not measurements of any market. The advisor's note says so.

## Owner steps (nothing here was run by Claude)
1. Deploy when no live trade is open. No migration.
2. Nothing changes until you set something. To let customers choose: `CUSTOMER_STRATEGY_CHOICE=true` and at least one `STRATEGY_*_AVAILABLE=true` (environment, or the admin settings table once the operator-settings PR is in). Keep `LIVE_RSI_MR` and `LIVE_DONCHIAN` as they are: they decide whether a chosen strategy trades.
3. Rollback: unset the switches (or reset the rows) and restart; accounts keep whatever `extra_strategies` they were given.
