# Leverage help and refusal messages (draft for review)

Status: DRAFT. These texts are NOT yet wired into the dashboard info icons. The owner and the reviewer approve the wording first; the screens then show exactly this text. The leverage setting is changed on the dashboard only (there is no Telegram command for it), and its help lives behind the dashboard info icon only. Plain language for a first-time crypto user, no financial advice. Anything about Binance's own screens is generic on purpose; any exact click path is marked UNVERIFIED.

## 1. Leverage setting: help text (dashboard info icon)

**What the number means.** Your leverage setting is the most a trade can be worth compared with the trading capital you gave the bot. At 3x, a trade is never worth more than 3 times your trading capital.

**Where the number applies.** It applies to Futures only. Spot trades ignore it. It applies to new trades only. A trade that is already open is not touched when you change it.

**Binance has its own number.** Binance keeps its own leverage number and its own margin mode (isolated or cross) for each pair, separately. The bot reads those two settings for the pair it is about to trade. The bot never changes anything on Binance. Only you can change them there.

**What the bot does when the two numbers differ.** The bot uses the smaller one.
- If Binance's number for the pair is lower than yours, the trade is sized to Binance's number, so the trade does not need more money set aside than your trading capital can cover at that number.
- If Binance's number is higher than yours, the bot does not refuse. It sizes the trade to your number, so the trade is never worth more than your setting allows.

**Isolated margin is required.** In isolated margin, the loss on a position is limited to the money set aside for that one position, and Binance closes the position when that money is used up. In cross margin, a position can draw on your whole Futures balance. This service trades isolated margin only. If the pair is on cross, or the bot cannot read the mode, it does not open a new trade on that pair, and it tells you how to fix it. Trades that are already open are not touched.

**Example.** You give the bot 21.60 USDT of trading capital and your setting is 3x. A trade is never worth more than 21.60 x 3 = 64.80 USDT. If Binance's number for the pair is 2x, the trade is limited to 21.60 x 2 = 43.20 USDT. If Binance's number is 10x, the trade is still limited to 64.80 USDT, your number. These are upper limits. A trade can be smaller, because the bot also limits each trade by your risk-per-trade setting.

**Please read this warning.** A higher number on Binance brings the liquidation price closer to the entry price. If the price reaches the liquidation price, Binance closes the position and you can lose all the money set aside for it. Before every trade, the bot makes an estimate of where Binance would liquidate the position. It builds the estimate from the leverage Binance has for the pair and the maintenance-margin tiers Binance publishes. If the bot's protective stop would sit too close to that estimated level, or beyond it, the bot does not open the trade. This is an estimate, not a price Binance gives the bot. Binance's real liquidation price can differ, for example because of fees, funding charges, or the price moving between the check and the fill. So the check lowers the risk. It does not remove it. A stop order can also fill at a worse price than planned, especially in fast markets. Only use money you can afford to lose.

## 2. Margin mode: help text (dashboard info icon next to the margin-mode status)

Isolated means each trade has its own money set aside, and a loss on it is limited to that amount. Cross means every Futures trade shares your whole Futures balance, so a loss on one can use money that backs another. This service trades isolated only. Binance keeps the choice for each pair, so check it for every pair you trade. To change it, open the Futures page for the pair on Binance and find the Margin Mode control. UNVERIFIED: the exact label and the steps on Binance's current screens; the reviewer will confirm them before this text goes live. You cannot change margin mode on a pair while you hold a position on it (UNVERIFIED, Binance's rule; confirm before publishing).

## 3. Customer-safe messages

These are the exact strings the code sends. A test keeps this file and the code in step.

**Margin mode is not isolated, or could not be read (size_cap mode):**
> your Binance Futures margin mode on {pair} is not isolated (or could not be read), and this service trades isolated margin only. On Binance, open the Futures page for {pair}, find the Margin Mode control and choose Isolated, then try again. MBA Bot never changes it for you.

**Binance leverage is above the service's global limit (strict mode):**
> your Binance Futures leverage on {pair} ({leverage}x) is above the limit of {limit}x set for this service. Lower it for this pair on Binance. MBA Bot never changes it for you.

**Margin mode is not isolated (strict mode):**
> your Binance Futures margin mode on {pair} is not isolated (or could not be read), and this service trades isolated margin only. Switch the pair to isolated margin on Binance. MBA Bot never changes it for you.

**Leverage could not be read:**
> Could not verify this account's Binance Futures leverage/margin just now, so no new live trade was opened. This will retry automatically on the next check.

**Stop too close to liquidation (existing pre-entry check, unchanged):**
> on {pair} your Binance leverage ({leverage}x) puts liquidation too close for a protective stop to work (the stop would sit about {stop}% from entry, at most about {safe}% is safe). Changing the leverage for this pair on Binance to a lower number allows trading it again.

## 4. For the reviewer
Statements about how Binance behaves, which the bot's code does not prove and which need confirming on Binance before this text goes live (UNVERIFIED):
- Binance stores one leverage number and one margin mode for each pair (the bot reads them per pair from Binance's symbol-config data through ccxt, which is not the same as seeing it on Binance's screens).
- The wording of the isolated-margin sentence (the loss is limited to the money set aside for that position, and Binance closes the position when it is used up).
- The exact names of the Leverage and Margin Mode controls on the Futures page, and whether margin mode can be changed while a position is open.
- The liquidation paragraph describes the bot's own estimate (`assess_stop_viability`, built from `maintenance_tiers`); the tiers come from Binance and the formula has not been checked against an official Binance page.
The leverage setting is changed on the dashboard only. The info icon and the margin-mode status are built after this wording is approved.
