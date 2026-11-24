"""Every customer-facing text of the setup advisor, in one place (docs/safety/setup_advisor.md). The full list is written to the private legal drafts for lawyer review (claude-code/legal-drafts/advisor-texts.md) by
`python -m mba_bot.setup_advice_texts`. Rules for the wording: say what a setting does and what the bot will or will not do; never say what to buy, sell or when to trade; never predict; never promise that a loss
stops at a limit. Placeholders in braces are filled with numbers from the bot's own rules. Three texts come straight from the rule the account API enforces (marked `ENFORCEMENT TEXT`); they are not repeated here."""
from __future__ import annotations

ASSUMPTION_STOP = "This assumes a stop-loss {stop_pct:g}% away from the entry price. Your real stop depends on the market at the time, and a stop can fill at a worse price."

TEXTS = {
    # 1. risk per trade versus the size cap
    "size_cap_binds": (
        "Your risk setting is {risk_pct:g}% of your trading capital, which is {risk_amount} {unit}. Your leverage limit of {leverage:g}x caps a position at {cap_value} {unit}. With a stop {stop_pct:g}% away, "
        "a position that size loses about {loss} {unit} ({loss_pct}% of your capital) if the stop is hit, not {risk_amount} {unit}. " + ASSUMPTION_STOP
    ),
    "size_cap_ok": (
        "At {risk_pct:g}% risk the bot sizes a trade so that a stop hit loses about {risk_amount} {unit}. Your leverage limit of {leverage:g}x does not reduce it at this stop distance. " + ASSUMPTION_STOP
    ),
    "size_cap_action": "Risk per trade and the leverage limit are on the Risk tab.",
    # 2. risk times capital versus the daily limit
    "daily_below_one_trade": (
        "One trade at {risk_pct:g}% of your {capital} {unit} capital risks about {risk_amount} {unit}, which is more than your daily loss limit of {limit} {unit}. One trade can lose more than this limit. "
        "A trade that is already open keeps its stop-loss; the limit only stops new trades."
    ),
    "daily_below_one_trade_action": "Change the daily loss limit or the risk per trade on the Risk tab.",
    "daily_covers_n_trades": "Your daily loss limit of {limit} {unit} is about {n:g} times the {risk_amount} {unit} one trade risks at the stop.",
    # 3. daily limit versus the total stop
    "daily_missing": "You have not set a daily loss limit. The service requires one before live trading can be turned on or the bot started.",
    "daily_missing_action": "Enter a daily loss limit in US dollars on the Risk tab.",
    "daily_levels": (
        "Your daily loss limit of {limit} {unit} is {limit_pct}% of your account value of {value} {unit}. The automatic total loss stop acts when the account is {stop_pct:g}% ({stop_value} {unit}) below its "
        "reference value, and is not a setting. The daily limit must stay below that level."
    ),
    # 4. slots
    "slots_pairs": "You set {slots} open trades, but only {pairs} pair(s) are set for your account ({pair_list}). The bot holds one trade per pair, so at most {pairs} trade(s) can be open.",
    "slots_pairs_action": "More pairs for your account can be set once the pair list is available on the Trading tab.",
    "slots_capital": (
        "A trade sized at {risk_pct:g}% risk uses about {margin} {unit} of your {capital} {unit} trading capital as margin. The bot shares the capital between trades, so after {k} trade(s) about {free} {unit} is left "
        "for the next one."
    ),
    "slots_capital_none": "With {k} trade(s) like that open, no trading capital is left, and the bot will not open trade number {next_n}.",
    # 5. open-risk cap
    "open_risk_refused": (
        "With {slots} slots at {risk_pct:g}% risk each, trade number {n} would be refused: {rule}. Open risk would be {total} {unit} against {limit_text}. The bot checks this with the stops as they are at the time. "
        "The first trade is not affected."
    ),
    "open_risk_rule_percent": "the open-risk cap of {cap_pct:g}% of your trading capital ({cap_value} {unit})",
    "open_risk_rule_daily": "your daily loss limit of {limit} {unit}",
    "open_risk_ok": "With {slots} slots at {risk_pct:g}% risk each, total open risk would be at most {total} {unit}, within the open-risk cap and your daily loss limit.",
    # 6. pair viability
    "pair_not_viable": (
        "{pair} needs about {need} {unit} of trading capital at your {leverage:g}x leverage limit to meet Binance's smallest order for it. Your trading capital is {capital} {unit}, so the bot cannot open a trade "
        "on {pair}."
    ),
    "pair_not_viable_action": "Choose another pair, raise your trading capital, or raise your leverage limit on the Trading and Risk tabs.",
    "pair_viable": "{pair} needs about {need} {unit} of trading capital at your {leverage:g}x leverage limit; you have {capital} {unit}.",
    "pair_unknown": "The smallest order Binance allows for {pair} is not known to the service yet. The bot checks it before each trade and tells you if the pair is too small for your capital.",
    # 7. strategies
    "strategies": (
        "Strategies running on your account, in the order the bot looks for a signal: {order}. Each uses the smaller of your risk setting and its own cap: {risks}."
    ),
    "strategies_one_slot": "With one open trade at a time, the first signal in that order takes the slot. The others wait until it closes.",
    "strategies_not_running": "{names} {is_are} switched on for your account but not running live on this service, so the bot takes no live {names} trades.",
    # 8. leverage
    "leverage_binance": "Your leverage limit is {cap:g}x. Binance's own leverage for {pair} is {binance:g}x. The bot caps a position at the smaller number ({effective:g}x of your trading capital).",
    "margin_not_isolated": "Binance shows a margin mode for {pair} that is not isolated, or it could not be read. This service trades isolated margin only and will not open a trade on {pair} until it is isolated. The service never changes it for you.",
    "leverage_cap": "Your leverage limit is {cap:g}x: the bot caps a position at {cap:g} times your trading capital. The service trades isolated margin only and never changes your leverage or margin mode on Binance.",
    # 9. risk bands and acknowledgments
    "risk_band_recommended": "{risk_pct:g}% is within the 2% or less that the service shows as its recommendation. The recommendation is a general statement, not advice for your circumstances.",
    "risk_band_high": "{risk_pct:g}% is above 2%. A short run of losing trades can reduce your capital faster at this setting. The service asks you to confirm you understand this, and records the confirmation.",
    "risk_band_very_high": "{risk_pct:g}% is 10% or more. A short run of losing trades can remove most of your trading capital at this setting. The service asks you to confirm you understand this, and records the confirmation.",
    "ack_needed_action": "Tick the confirmation under the risk field.",
    # 10. deposits and withdrawals
    "money_moves": (
        "Moving money in or out of your Binance account during the day changes the value the daily limit measures from, and the reference value of the automatic total stop. After you move money, reset the total "
        "stop if it is shown, and expect the daily limit to measure from a different start."
    ),
    # 11. the candle size of the EMA strategy (a preview: the candle size is set by the operator for everybody, nothing here saves it)
    "tf_unknown": "That candle size is not one of the three the preview knows: 1h, 4h or 1d.",
    "tf_stop_refused": (
        "With {tf} candles the EMA stop-loss sits about {stop_pct:.1f}% from the entry price (this preview assumes {base_pct:g}% on 1h and scales it with the square root of the candle length; real stops can be wider or narrower). "
        "At {lev_phrase} the bot's protection check allows a stop up to {max_pct:.1f}% from the entry (the same formula the bot uses, with its lowest maintenance rate; the live check reads Binance's own table and can be tighter). "
        "So the check would refuse these entries and the bot would not open the trade."
    ),
    "tf_stop_refused_action": "A lower leverage allows a wider stop: set it on Binance for this pair, and check the leverage limit on the Risk tab.",
    "tf_stop_ok": (
        "With {tf} candles the EMA stop-loss sits about {stop_pct:.1f}% from the entry price (this preview assumes {base_pct:g}% on 1h and scales it with the square root of the candle length). "
        "At {lev_phrase} the bot's protection check would allow a stop up to {max_pct:.1f}% (same formula, lowest maintenance rate; the live check reads Binance's own table)."
    ),
    "tf_stop_spot": "With {tf} candles the EMA stop-loss sits about {stop_pct:.1f}% from the entry price (assumed, see above). Spot has no liquidation price, so the protection check on leverage does not apply.",
    "tf_order_too_small": (
        "At {risk_pct:g}% risk and a stop {stop_pct:.1f}% away, a trade on your {capital} {unit} capital is about {order} {unit} of {pair}. The exchange's minimum order for {pair} is {minimum} {unit}, so the bot would not be able to open it. "
        "A wider stop means a smaller position for the same risk."
    ),
    "tf_order_too_small_action": "More trading capital or a higher risk per trade makes the order larger. Both are on the Trading and Risk tabs.",
    "tf_order_ok": "At {risk_pct:g}% risk and a stop {stop_pct:.1f}% away, a trade on your {capital} {unit} capital is about {order} {unit} of {pair}, above the exchange's minimum of {minimum} {unit}.",
    "tf_order_unknown": "At {risk_pct:g}% risk and a stop {stop_pct:.1f}% away, a trade on your {capital} {unit} capital is about {order} {unit}. The exchange's minimum order for {pair} could not be read, so it is not compared.",
    "tf_signals": (
        "The EMA crossover that opens a trade is rare on long candles. On a random price series it crosses about {per_hundred:g} times in every 100 candles, which is about {per_year:g} times a year on one pair with {tf} candles. "
        "The bot also skips flat markets, so expect fewer, and with one open trade at a time you can take fewer still. Real markets can differ by a factor of two or more. This is a count of signals, not a forecast of results."
    ),
    # common
    "no_capital": "Your trading capital is not set. The bot cannot open a live trade without it.",
    "no_capital_action": "Enter your trading capital on the Trading tab.",
}


def render(key: str, **kw) -> str:
    return TEXTS[key].format(**kw)


def as_markdown() -> str:
    lines = [
        "# Setup advisor: every customer-facing text (DRAFT)", "",
        "**FOR LAWYER REVIEW (lawyer question 7: advice). Not legal advice. The advisor ships OFF (`SETUP_ADVISOR`) and must not be turned on until the owner says so.**", "",
        "Rules for the wording: it says what a setting does and what the bot will or will not do. It never says what to buy or sell or when to trade, never predicts, never promises that a loss stops at a limit.",
        "Placeholders in braces are numbers filled in from the bot's own rules. Three notes (risk outside the allowed range, a missing risk confirmation, a slot setting the service refuses) reuse the exact text the account API shows when it refuses a save; those texts are in `src/mba_bot/risk_limits.py` and `src/mba_bot/live_multi.py`.", "",
        "| id | text |", "|---|---|",
    ]
    for k, v in TEXTS.items():
        lines.append(f"| `{k}` | {v.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(as_markdown())
