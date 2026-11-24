# Profit protection v2, Part C: the staged profit lock (one shared module, all three strategies)

Status: built, every stage OFF. Base branch: `feature/profit-every-round` (Part B), on `feature/profit-peak-tracking` (Part A), on `feature/ema-exit-rules` (PR #84).

## What it is
`profit_protect.staged_level` is pure and shared. EMA, Donchian and RSI trades, live and paper, all go through the same function and the same live step (`profit_live.running_step`). The strategy files `donchian.py` and `rsi_mr.py` (pinned by their pre-registered plans) are untouched.

R is the distance from the entry to the INITIAL stop. Every stage is judged on the best price since entry (`trades.peak_price`, Part A), not on where the price is now.

| Parameter | Setting | What it does |
|---|---|---|
| Stage 1 activation | `PROFIT_LOCK_STAGE1_R` (0 = off; try 1, 1.5, 2) | Once the best price is up this many R, the stop goes to entry plus the cost buffer. Close reason `break_even_stop`. |
| Stage 1 placement | `PROFIT_LOCK_STAGE1_ATR` (0 = at entry plus buffer) | If set, the stop is the better of entry plus buffer and (best price minus N ATR), so it sits beyond normal noise instead of exactly at the entry. |
| Stage 2 | `PROFIT_LOCK_STAGE2_R`, `PROFIT_LOCK_STAGE2_LOCK_R` (default 1) | Once up the activation R, lock entry plus lock R x R. Close reason `profit_lock`. Off unless the lock is below the activation. |
| Trail start | `PROFIT_TRAIL_START_R` (0 = off) | Once up this many R, trail the best price by an ATR multiple. Close reason `trailing_stop`. |
| Trail distance | `PROFIT_TRAIL_ATR` (default 2), `PROFIT_TRAIL_TIERS` (for example `0:3,2:2,4:1.5`) | Wide at low profit, tighter as profit grows: 3 ATR below +2R, 2 ATR from +2R, 1.5 ATR from +4R. Tiers are read from the peak profit in R. |
| Time condition | `PROFIT_TIGHTEN_AFTER_CANDLES`, `PROFIT_TIGHTEN_TRAIL_ATR` | After the trade has been held this many candles of its strategy's own timeframe, the trail is no wider than this. Both must be set. |

Each value is its own switch. Unset, empty, malformed, zero, negative or non-finite means OFF (a typo can never switch a stage on). Values that are on are clamped (R 0.25 to 50, ATR 0.5 to 10). A bad token anywhere in `PROFIT_TRAIL_TIERS` drops all tiers.

## The cost buffer
`entry x (2 x taker fee bps + assumed round-trip slippage bps) / 10,000`, plus funding paid so far per unit if known. Defaults: fee 5 bps per side (`FEE_GUARD_FEE_BPS`), slippage 11 bps round trip (`FEE_GUARD_SLIPPAGE_BPS`). On a 90 entry that is 0.189, so a stop-out at the stage 1 level is at or above zero net, not slightly below.
- The account's real fee tier is not read yet. Until Part F stores it, the buffer uses the assumed 5 bps and says so here. When the real rate is known (Part F) the buffer uses it.
- Funding paid is zero until Part F captures it. A funding amount received never shrinks the buffer.
- A stop at entry plus buffer is real money only if the exit fills at the stop. A gap or a thin book fills worse. This is not a guarantee.

## What a level can and cannot do
- A level that is not on the loss side of the best price is dropped. If R is smaller than the buffer, stage 1 simply waits until the peak gain exceeds the buffer, instead of forcing an instant exit.
- `profit_protect.decide` (Part B) never loosens a stop and never re-places the same level. If the price is already through the earned level the trade exits through `lifecycle.execute_exit` at the market, near the entry rather than near the original stop. If a better stop already rests at that level, the exchange-side stop owns the exit.
- Every stop move is `lifecycle.move_stop`: new stop placed and read back first, the trade records it, then the old one is cancelled. Same call limits as Part B (0.25 ATR improvement, 900 s between moves, first lock exempt from the interval).
- The staged lock runs every round from the peak whatever `PROFIT_EVERY_ROUND` says (it has no older candle-close behaviour to keep).
- Paper trades: the stop is ratcheted to the same level, never loosened, and set at the price when the price is already through it (a market exit, as live).

## Spot
On Spot the placed-first stop needs the base asset the old stop has locked. The fake exchange refuses it (`InsufficientFunds`), so the existing stop stays and the customer gets the existing "could not move the trailing stop" notice at most once per 6 hours. This is the same limit the live Donchian trail already has (UNVERIFIED against real Binance Spot). Staged exits through `execute_exit` still work on Spot. Part H measures Futures only.

## Not proven
No setting here has been shown to make money. Part H measures it on the same candles and reports whether a rule set lowers net profit.
