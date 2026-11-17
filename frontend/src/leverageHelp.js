// The approved customer help for the leverage setting and the margin mode (docs/customer/leverage_help.md, sections 1 and 2). Shown behind the info icons in Bot Settings.
// Plain words for a first-time user. A node test (leverageHelp.test.js) re-reads the doc and fails if this file drifts from it. Nothing marked UNVERIFIED in the doc is here.
export const LEVERAGE_HELP = {
  title: "About the leverage setting",
  blocks: [
    {
        "title": "What the number means",
        "text": "Your leverage setting is the most a trade can be worth compared with the trading capital you gave the bot. At 3x, a trade is never worth more than 3 times your trading capital."
    },
    {
        "title": "Where the number applies",
        "text": "It applies to Futures only. Spot trades ignore it. It applies to new trades only. A trade that is already open is not touched when you change it."
    },
    {
        "title": "Binance has its own number",
        "text": "Binance keeps its own leverage number and its own margin mode (isolated or cross) for each pair, separately. The bot reads those two settings for the pair it is about to trade. The bot never changes anything on Binance. Only you can change them there."
    },
    {
        "title": "What the bot does when the two numbers differ",
        "text": "The bot uses the smaller one.",
        "bullets": [
            "If Binance's number for the pair is lower than yours, the trade is sized to Binance's number, so the trade does not need more money set aside than your trading capital can cover at that number.",
            "If Binance's number is higher than yours, the bot does not refuse. It sizes the trade to your number, so the trade is never worth more than your setting allows."
        ]
    },
    {
        "title": "Isolated margin is required",
        "text": "In isolated margin, the loss on a position is limited to the money set aside for that one position, and Binance closes the position when that money is used up. In cross margin, a position can draw on your whole Futures balance. This service trades isolated margin only. If the pair is on cross, or the bot cannot read the mode, it does not open a new trade on that pair, and it tells you how to fix it. Trades that are already open are not touched."
    },
    {
        "title": "Example",
        "text": "You give the bot 21.60 USDT of trading capital and your setting is 3x. A trade is never worth more than 21.60 x 3 = 64.80 USDT. If Binance's number for the pair is 2x, the trade is limited to 21.60 x 2 = 43.20 USDT. If Binance's number is 10x, the trade is still limited to 64.80 USDT, your number. These are upper limits. A trade can be smaller, because the bot also limits each trade by your risk-per-trade setting."
    },
    {
        "title": "Please read this warning",
        "text": "A higher number on Binance brings the liquidation price closer to the entry price. If the price reaches the liquidation price, Binance closes the position and you can lose all the money set aside for it. Before every trade, the bot makes an estimate of where Binance would liquidate the position. It builds the estimate from the leverage Binance has for the pair and the maintenance-margin tiers Binance publishes. If the bot's protective stop would sit too close to that estimated level, or beyond it, the bot does not open the trade. This is an estimate, not a price Binance gives the bot. Binance's real liquidation price can differ, for example because of fees, funding charges, or the price moving between the check and the fill. So the check lowers the risk. It does not remove it. A stop order can also fill at a worse price than planned, especially in fast markets. Only use money you can afford to lose."
    }
],
};

export const MARGIN_MODE_HELP = {
  title: "About margin mode",
  blocks: [
    {
        "title": null,
        "text": "Isolated means each position has its own money set aside, and a loss on it is limited to that amount. Cross means every Futures position shares your whole Futures balance, so a loss on one can use money that backs another. This service trades isolated margin only. Binance keeps the choice for each pair, so check it for every pair you trade. To change it, open the Futures page for the pair on Binance and look for the Leverage and Margin Mode settings. The bot never changes them for you."
    }
],
};
