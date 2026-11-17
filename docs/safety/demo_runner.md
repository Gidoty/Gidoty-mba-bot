# Demo runner (B4)

Status: on a branch (stacked on the small-capital PR for the migration chain), not merged, not deployed. Ships OFF (`DEMO_RUNNER`, default off). CLAUDE-RUN against a FAKE exchange only.

## What the bot does with the switch OFF
Exactly what it does today. No account can resolve to DEMO, the three demo routes answer 409, no demo endpoint is reachable, and the customer's paper accounts keep using the platform's paper keys.

## What DEMO is
A third mode next to paper and live. DEMO runs the **full live path** (entry, protective stop placed and verified, reconciliation, exits) against Binance **Futures Demo Trading** (Futures accounts) or the **Spot Testnet** (Spot accounts), with the customer's OWN demo keys. It is not paper: paper is the existing simulated mode with the platform's keys and no lifecycle.

## How a real key can never be used by mistake
1. Demo keys live in their own table (`demo_credentials`, encrypted like every credential). The live credential (`api_credentials`) is never read in DEMO and the demo keys are never read in live.
2. A DEMO client is always built with `sandbox=True`; `exchange.build_exchange_client` proves from the effective URL table that it routes only to Demo Trading / Spot Testnet hosts (`exchange.assert_routing`, already used for paper). A live client is always `sandbox=False` and proven production-only.
3. Saving demo keys and switching DEMO on each make one authenticated read against the demo endpoint. Binance does not accept a real key there, so a real key (or a key for the other demo system) is refused with a fixed text and nothing is stored or switched on. A demo key equal to the stored live key is refused with no call.
4. DEMO needs: the switch, demo keys on file, the customer's explicit confirmed action, a paid account, trading capital set, no open trade, and NO live authorisation (`live_trading_enabled_at` and `bot_started_at` both empty). Live trading cannot be enabled while DEMO is on; DEMO cannot be switched on while live is enabled. An ambiguous row (both) resolves to the ordinary live/paper chain, never DEMO.

## Records never mix
`trades.is_demo` marks demo trades (`is_paper` is False for them, because the path is the live one). The live screens (`is_paper=false`) exclude them; `is_demo=true` returns only them; paper never includes them. Telegram messages say `[DEMO]`, never `[LIVE]` (`lifecycle._mode_tag`). Strategy-guard history ignores demo trades. The dashboard, Trade History, Performance and Positions show a DEMO badge or subtitle and the app shows a DEMO banner on every page.

## What DEMO does not apply (decision for the owner, see the PR)
* The daily dollar limit, the 50% total stop and their equity samples are the live account's: they are measured from the live balance. Running them on demo money would mix two balances, so they are skipped in DEMO. The kill switch, the pre-entry protection check, risk per trade, the leverage cap and the protective stop all apply.
* The withdrawal-permission read (`assert_trade_only`, `has_withdrawal_permission`) is skipped in DEMO: it is a production-only endpoint and demo keys cannot withdraw real funds. Live is unchanged.

## Migration `a6e2c4f8b1d3` (parent `f4c7d9b2a6e1`)
`trading_accounts.demo_enabled_at`, `trades.is_demo` (default false), table `demo_credentials`. Additive, idempotent, reversible. Not run.

## Owner steps
See the runbook in the handoff repo (`claude-code/runbooks/demo-runner.md`): create the demo keys on Binance yourself, restart gate, backup, migrate, deploy, set `DEMO_RUNNER=true`, enter the keys on the Exchange Connection page, switch demo mode on.
Rollback: remove the line and restart; `alembic downgrade f4c7d9b2a6e1` drops the table and columns (demo keys lost).

## NOT verified
* That Binance Futures Demo Trading and the Spot Testnet accept every call the live path makes (conditional stop orders, `fetch_my_trades`, margin and leverage reads, `fetch_balance`). UNVERIFIED: the fake exchange stands in. The first demo run is the test.
* That the demo endpoint rejects a real key with an error this code treats as a refusal (ccxt behaviour assumed; any failure refuses, so the failure mode is "demo does not start").
* The screens in a browser.
