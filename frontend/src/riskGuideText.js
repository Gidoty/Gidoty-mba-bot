// What the app tells a customer about risk per trade, leverage and the safety limits. One source for the How-to guide, the Help page and the Landing page, so they cannot disagree.
// Every number comes from the constants in lib/riskBands.js, which a Python test compares with the code. Plain words, no promise of profit, no advice.
import { LEVERAGE_DEFAULT, LEVERAGE_MAX, LEVERAGE_MIN, RECOMMENDED_MAX, RISK_CEILING, RISK_MIN, TOTAL_STOP_PCT, VERY_HIGH_FROM } from "./lib/riskBands.js";

// The definition of "risk per trade" and how it differs from leverage. One wording, used by the info icon next to the field, the How-to guide and the Help page.
export const RISK_DEFINITION = "Risk per trade is the share of your trading capital the bot is willing to lose on one trade if its stop-loss is hit.";
export const RISK_NOT = "It is not the amount put into the trade and it is not the size of the trade.";
export const RISK_EXAMPLE = "Example: with 100 USDT of trading capital and a risk of 2%, the bot sizes the trade so that a stop-loss hit loses about 2 USDT. The trade itself can be worth more than 2 USDT.";
export const RISK_VS_LEVERAGE = "The leverage setting is different: risk sets how much a stop-loss hit may lose, and the leverage setting caps how large the trade can be compared with your trading capital.";
export const RISK_WORSE_FILL = "If a stop fills at a worse price than planned, the loss can be larger.";
export const RISK_NEW_TRADES_ONLY = "A change to your risk per trade, leverage or trading capital applies to new trades only.";

// The dialog behind the info icon next to the Risk per trade field (same block shape as the leverage help).
export const RISK_PER_TRADE_HELP = {
  title: "About risk per trade",
  blocks: [
    { title: null, text: `${RISK_DEFINITION} ${RISK_NOT}` },
    { title: null, text: RISK_EXAMPLE },
    { title: null, text: RISK_VS_LEVERAGE },
    { title: null, text: RISK_WORSE_FILL },
  ],
};

const RISK_CHOICE = `You can choose from ${RISK_MIN}% up to ${RISK_CEILING}% at most; Bot Settings shows the highest value that applies now. We recommend ${RECOMMENDED_MAX}% or less. Above ${RECOMMENDED_MAX}% the screen warns you and asks you to tick a box. From ${VERY_HIGH_FROM}% it turns red and asks for a second, stronger tick.`;
const LEVERAGE_TEXT = `Your leverage setting (${LEVERAGE_MIN}x to ${LEVERAGE_MAX}x, ${LEVERAGE_DEFAULT}x until you change it) is the most a position can be worth compared with your trading capital. You change it on the dashboard only. Binance keeps its own number for each pair. The bot uses the smaller of the two and never changes anything on Binance. It trades isolated margin only.`;

export const RISK_GUIDE_STEPS = [
  ["Risk per trade", `${RISK_DEFINITION} ${RISK_NOT} ${RISK_EXAMPLE}`],
  ["Choosing your risk", `${RISK_CHOICE} ${RISK_WORSE_FILL}`],
  ["Leverage on Futures", LEVERAGE_TEXT],
  ["Risk and leverage compared", `${RISK_VS_LEVERAGE} ${RISK_NEW_TRADES_ONLY}`],
  [
    "The liquidation check",
    "Before a Futures trade the bot estimates where Binance would liquidate the position. If its stop-loss would sit too close to that level, or beyond it, it does not open the trade and tells you. This is an estimate, so it lowers the risk and does not remove it. A stop-loss can also fill at a worse price in a fast market.",
  ],
  [
    "Daily loss limit",
    "You set your own daily loss limit in US dollars (USDT) on Bot Settings, and live trading cannot start without it. If your account falls by that amount within one day, the bot opens no new trades until the next day starts at midnight Lagos time. Lowering it applies at once; raising it starts at the next midnight Lagos time. It does not change with your risk setting, and an open trade keeps its stop-loss.",
  ],
  [
    "Total loss stop",
    `If your account falls ${TOTAL_STOP_PCT}% or more below its reference value, the bot opens no new trades until you reset it on Bot Settings. This stop is automatic and the same for every account. It is not a setting, and it never resets on its own. The reference value is your balance when the bot first checked, or when you last reset.`,
  ],
  [
    "Open trades and your money",
    "The loss limits apply to live trading; practice trades are simulated. They only stop new trades. A trade that is already open keeps its stop-loss and exit rules. Do not deposit or withdraw money in the middle of the day: the daily limit and the total loss stop are measured from your balance, so moving money makes them wrong. After you move money, reset the total loss stop on Bot Settings.",
  ],
];

// The Help page questions, taken from the same text.
export const RISK_HELP_TOPICS = [
  { title: "How much can I risk on one trade?", body: `${RISK_DEFINITION} ${RISK_NOT} ${RISK_EXAMPLE} ${RISK_CHOICE} ${RISK_WORSE_FILL}` },
  { title: "What does the leverage setting do?", body: `${LEVERAGE_TEXT} ${RISK_VS_LEVERAGE}` },
  { title: "What can stop the bot from opening new trades?", body: `${RISK_GUIDE_STEPS[4][1]} ${RISK_GUIDE_STEPS[5][1]} ${RISK_GUIDE_STEPS[6][1]}` },
  { title: "Should I deposit or withdraw while the bot runs?", body: RISK_GUIDE_STEPS[7][1] },
];
