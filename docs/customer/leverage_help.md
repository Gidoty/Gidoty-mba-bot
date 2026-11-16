# Leverage help and refusal messages (draft for review)

Status: DRAFT. These texts are NOT yet wired into the dashboard info icons or Telegram `/help`. The owner and the reviewer approve the wording first; the screens then show exactly this text. Plain language for a first-time crypto user, no financial advice. Anything about Binance's own screens is generic on purpose; any exact click path is marked UNVERIFIED.

## 1. Leverage setting: help text (dashboard info icon and Telegram `/help leverage`)

**What the number means.** Your leverage setting is the most a trade can be worth compared with the trading capital you gave the bot. At 3x, a trade is never worth more than 3 times your trading capital.

**Where the number applies.** It applies to Futures only. Spot trades ignore it. It applies to new trades only. A trade that is already open is not touched when you change it.

**Binance has its own number.** Binance keeps its own leverage number and its own margin mode (isolated or cross) for each pair, separately. The bot reads those two settings for the pair it is about to trade. The bot never changes anything on Binance. Only you can change them there.

**What the bot does when the two numbers differ.** The bot uses the smaller one.
- If Binance's number for the pair is lower than yours, the trade is sized to Binance's number, as the exchange would limit it anyway.
- If Binance's number is higher than yours, the bot does not refuse. It sizes the trade to your number, so the trade is never worth more than your setting allows.

**Isolated margin is required.** In isolated margin, a trade can only lose the money set aside for that one trade. In cross margin, a trade can draw on your whole Futures balance. This service trades isolated margin only. If the pair is on cross, or the bot cannot read the mode, it does not open a new trade on that pair, and it tells you how to fix it. Trades that are already open are not touched.

**Example.** You give the bot 21.60 USDT of trading capital and your setting is 3x. A trade is never worth more than 21.60 x 3 = 64.80 USDT. If Binance's number for the pair is 2x, the trade is limited to 21.60 x 2 = 43.20 USDT. If Binance's number is 10x, the trade is still limited to 64.80 USDT, your number.

**Please read this warning.** A higher number on Binance brings the liquidation price closer to the entry price. If the price reaches the liquidation price, Binance closes the position and you can lose the whole margin set aside for it. The bot checks, before every trade, that its protective stop sits inside the liquidation price that Binance reports for the pair, and it does not open the trade if it does not. That check reduces the risk. It does not remove it. A stop order can fill at a worse price than planned, especially in fast markets. Only use money you can afford to lose.

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
> on {pair} your Binance leverage ({leverage}x) puts liquidation too close for a protective stop to work (the stop would sit about {stop}% from entry, at most about {safe}% is safe). Lowering the leverage for this pair on Binance (for example to 10x) allows trading it again.

Reviewer note: this existing text suggests "for example to 10x", which is above most customers' own setting. Consider wording it as "to a lower number". Not changed in this build.

## 4. Telegram replies for `/leverage`

- Status: "Your leverage setting is {n}x. It is the most a position can be worth compared with your trading capital, and you can choose a whole number from 1 to {top}. To change it, send /leverage followed by the number, for example /leverage 2. A change applies to new trades only; a trade that is already open is not touched. MBA Bot never changes your leverage or margin mode on Binance."
- Changed: "Done. Your leverage setting is now {n}x for new trades. A trade that is already open is not touched."
- Not a valid number: "Send a whole number from 1 to {top}, for example /leverage 2. Nothing was changed."
- Spot account: "The leverage setting applies to Futures only. Your account trades Spot, so it has no effect on your trades."
- Chat not linked: "This chat is not linked to an MBA Bot account. Open your dashboard and link Telegram first."

## 5. For the reviewer
- The refusal in section 3 for the stop check is the existing text; confirm it still reads well beside the new ones.
- Telegram `/help` and the dashboard info icons are the next step after this wording is approved. This build only adds the leverage control itself.
