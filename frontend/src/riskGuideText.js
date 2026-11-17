// What the app tells a customer about risk per trade, leverage and the safety limits. One source for the How-to guide, the Help page and the Landing page, so they cannot disagree.
// Every number comes from the constants in lib/riskBands.js, which a Python test compares with the code. Plain words, no promise of profit, no advice.
import { LEVERAGE_DEFAULT, LEVERAGE_MAX, LEVERAGE_MIN, RECOMMENDED_MAX, RISK_CEILING, RISK_MIN, TOTAL_STOP_CAP_PCT, TOTAL_STOP_MIN_PCT, TOTAL_STOP_MULTIPLE, VERY_HIGH_FROM } from "./lib/riskBands.js";

export const RISK_GUIDE_STEPS = [
  [
    "Risk per trade",
    `This is the share of your trading capital the bot is willing to lose on one trade if its stop-loss is hit. You can choose from ${RISK_MIN}% up to ${RISK_CEILING}% at most; Bot Settings shows the highest value that applies now. We recommend ${RECOMMENDED_MAX}% or less. Above ${RECOMMENDED_MAX}% the screen warns you and asks you to tick a box. From ${VERY_HIGH_FROM}% it turns red and asks for a second, stronger tick.`,
  ],
  [
    "Leverage on Futures",
    `Your leverage setting (${LEVERAGE_MIN}x to ${LEVERAGE_MAX}x, ${LEVERAGE_DEFAULT}x until you change it) is the most a position can be worth compared with your trading capital. You change it on the dashboard only. Binance keeps its own number for each pair. The bot uses the smaller of the two and never changes anything on Binance. It trades isolated margin only.`,
  ],
  [
    "The liquidation check",
    "Before a Futures trade the bot estimates where Binance would liquidate the position. If its stop-loss would sit too close to that level, or beyond it, it does not open the trade and tells you. This is an estimate, so it lowers the risk and does not remove it. A stop-loss can also fill at a worse price in a fast market.",
  ],
  [
    "Daily loss pause",
    "If your account falls by the daily loss limit within one day, the bot opens no new trades until the next day starts. Your balance and open positions are counted at their current value. The day runs from midnight to midnight in Lagos time. The limit is the same for every account and does not change with your risk setting, so at a high risk setting one lost trade can already reach it.",
  ],
  [
    "Total loss stop",
    `If your account falls ${TOTAL_STOP_MIN_PCT}% below its reference value, or ${TOTAL_STOP_MULTIPLE} times your risk per trade if that is more (never more than ${TOTAL_STOP_CAP_PCT}%), the bot opens no new trades until you reset it on Bot Settings. It never resets on its own. The reference value is your balance when the bot first checked, or when you last reset.`,
  ],
  [
    "Open trades and your money",
    "The loss limits apply to live trading; practice trades are simulated. They only stop new trades. A trade that is already open keeps its stop-loss and exit rules. Do not deposit or withdraw money in the middle of the day: the daily pause and the total loss stop are measured from your balance, so moving money makes them wrong. After you move money, reset the total loss limit on Bot Settings.",
  ],
];

// The Help page questions, taken from the same text.
export const RISK_HELP_TOPICS = [
  { title: "How much can I risk on one trade?", body: RISK_GUIDE_STEPS[0][1] },
  { title: "What does the leverage setting do?", body: RISK_GUIDE_STEPS[1][1] },
  { title: "What can stop the bot from opening new trades?", body: `${RISK_GUIDE_STEPS[2][1]} ${RISK_GUIDE_STEPS[3][1]} ${RISK_GUIDE_STEPS[4][1]}` },
  { title: "Should I deposit or withdraw while the bot runs?", body: RISK_GUIDE_STEPS[5][1] },
];
