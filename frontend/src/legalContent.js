// The three legal documents, as shown to customers. Plain and short on purpose. Every statement describes what the service does; where the code
// can prove it, docs/commercial_readiness.md names the place. Each section is { heading, body }, body being a list of paragraph strings or { list: [...] }.
// There is deliberately no fourth document: refunds, cancellation and data requests are inside the Terms and the Privacy Policy.
//
// Versions: the version of each document is its effective date written YYYY-MM-DD. They MUST equal TERMS_VERSION, PRIVACY_VERSION and
// RISK_DISCLOSURE_VERSION in src/mba_bot/legal.py (a test checks it). Change the text, change the date here and there, and every customer is asked
// to accept again.

export const TERMS_VERSION = "2026-11-21";
export const PRIVACY_VERSION = "2026-11-21";
export const RISK_DISCLOSURE_VERSION = "2026-11-21";

export const TERMS_OF_SERVICE = {
  title: "Terms of Service",
  effectiveDate: "21 November 2026",
  version: TERMS_VERSION,
  intro: [
    'These Terms govern your use of the Metabridge Academy automated cryptocurrency trading bot, its website and dashboard, and its notifications (together, the "Service").',
    "By creating an account, paying for access, connecting an exchange account, enabling live trading or otherwise using the Service, you agree to these Terms. The Trading Risk Disclosure and the Privacy Policy are part of them.",
  ],
  sections: [
    {
      heading: "1. Who runs the Service",
      body: [
        "The Service is operated by Metabridge Academy (website: metabridgeacademy.com, e-mail: info@metabridgeacademy.com). In these Terms, \"we\" and \"us\" mean Metabridge Academy.",
      ],
    },
    // REGULATORY-STATUS-SLOT: the operator may add one sentence here about the regulatory status of the Service. Nothing in this document says whether
    // the Service is or is not licensed, registered, approved, exempt or regulated, and none of the code assumes it.
    {
      heading: "2. What the Service does",
      body: [
        "The Service places buy and sell orders on your own Binance account (Spot or Futures), automatically, using the settings you chose and an API key you supply. It does not hold, receive or move your money. Your assets stay on your exchange account.",
        "The Service is software. It does not give personal investment advice, it does not manage your money for you, and it does not tell you what you should buy or sell. A note on screen, such as \"2% or less\", is general information and is not advice for your situation.",
        "We do not promise profit. Results can be a total loss of the money you trade with.",
      ],
    },
    {
      heading: "3. Eligibility",
      body: [
        "You must be able to enter into a contract and must follow the laws that apply to you. You are responsible for checking that cryptocurrency trading, automated trading and the use of Binance are lawful for you where you live.",
        "You must not use the Service for anything unlawful, including fraud, market manipulation, money laundering or evading sanctions.",
      ],
    },
    {
      heading: "4. Your exchange account and API key",
      body: [
        "You connect the Service with a Binance API key. The key needs permission to trade only. Do not enable withdrawal permission.",
        "The Service checks the key before it opens a new trade. If it finds that the key can withdraw, or cannot confirm that it cannot, it stops opening new trades on your account until you replace the key with a trade-only key. Trades that are already open keep being managed.",
        "We store your key encrypted and do not show the secret again after you save it. You are responsible for your exchange account, its security and its settings, and for revoking the key at Binance at once if you think it has been exposed.",
        "The Service reads your leverage and margin settings on Binance and never changes them. You keep control of them on your exchange account.",
      ],
    },
    {
      heading: "5. Practice and live trading",
      body: [
        "A new account starts in practice mode. Practice trades are simulated: no real order is placed and no real money is used. Practice results can differ from live results in prices, speed and fees.",
        "Live trading places real orders with real money on your Binance account. It starts only when all of these are true: your access is paid (while paid access is switched on), the practice period has ended or you have chosen to skip it, you have saved a Binance key marked as a live key, you have set a daily loss limit in US dollars and a trading capital, you have accepted the Trading Risk Disclosure, and you press Start Bot. Paying does not turn live trading on by itself.",
        "Once the bot is started it trades automatically without asking you before each order. You can see your trades on the dashboard. You are responsible for watching your account and for turning off automated trading, or revoking the key at Binance, when you no longer want the bot to trade.",
      ],
    },
    {
      heading: "6. Settings you choose",
      body: [
        "You choose these settings, within the limits the Service sets:",
        {
          list: [
            "Risk per trade, from 0.1% of your trading capital up to a maximum shown in your settings, and never more than 40%. The default is 1%. You are asked to confirm above 2% and again from 10%, and we record each confirmation with its date and the version of the text you saw.",
            "Leverage cap for Futures, from 1x to 5x (3x if you never set one).",
            "Trading capital: the most of your Binance balance the bot may use as its sizing base. It is a ceiling, not a transfer.",
            "A daily loss limit in US dollars. It is required before live trading can start.",
          ],
        },
        "The settings are your decision. We may change the limits within which you can choose.",
      ],
    },
    {
      heading: "7. Safety limits",
      body: [
        "The Service has limits that can stop it from opening NEW trades: your daily loss limit; an automatic total loss stop when your account value is 50% or more below its reference value; for Futures, a check that the stop-loss sits safely away from the estimated liquidation level; and a control we can use to stop new trades for all accounts.",
        "These limits stop new trades only. They never close a trade that is already open, and an open trade keeps its stop-loss and exit rules. They are measured from your exchange balance when the Service checks it, so a loss can pass a limit before the Service acts. They do not guarantee that a loss stops at any amount.",
        "If you deposit or withdraw money, the limits measure from the wrong value until the next day starts or, for the total loss stop, until you reset it. You are responsible for resetting it after you move money.",
        "We may stop opening new trades at any time. We are not required to close an open trade because of a limit.",
      ],
    },
    {
      heading: "8. Trading risk and no guaranteed returns",
      body: [
        "Cryptocurrency trading is risky. Prices can move fast, markets can have little liquidity, exchanges can go down, orders can fill at a different price from the one expected, and software can fail. Only trade with money you can afford to lose.",
        "Nothing in the Service, including its risk setting, safety limits, checks, reports and notifications, guarantees a profit, limits a loss to a particular amount or protects your capital. Past, simulated or tested results do not show future results.",
        "The Service may run more than one trading strategy on an account. Strategies other than the default are newer, have a shorter record, and can perform differently from their tests. The Service applies your daily loss limit and the total loss stop to the whole account across all strategies and trades.",
      ],
    },
    {
      heading: "9. Futures and leverage",
      body: [
        "Futures and leverage can lose money much faster than Spot trading. You can lose the money set aside for a position and the exchange may close it when that money is used up.",
        "You set a leverage cap. The Service sizes a position to at most that multiple of your trading capital, or to Binance's own limit for the pair if that is lower. The Service trades isolated margin only. Leverage and margin mode can differ between pairs: check them for each pair you trade.",
      ],
    },
    {
      heading: "10. Your responsibilities",
      body: [
        "You are responsible for:",
        {
          list: [
            "your login details, your e-mail account and your Telegram account;",
            "your exchange account, its security and its API key permissions;",
            "the settings you choose and for checking them;",
            "keeping enough balance in your exchange account;",
            "following the laws and the exchange's rules that apply to you.",
          ],
        },
      ],
    },
    {
      heading: "11. Fees and paid access",
      body: [
        "Binance charges its own trading commissions and, on Futures, funding payments to your exchange account. We do not set or receive them. Figures we show are \"before fees\" unless we say they are net, which we do only when the real exchange fees of every trade in the figure are known. Funding payments are not included in any figure. Practice figures are simulated.",
        "Paid access is a monthly subscription. The price and the currency are shown on the Activate screen before you pay: by card in naira through Paystack, or in USDT through NOWPayments. A card subscription renews by itself every month until you cancel it. A USDT payment buys one month and does not renew by itself: you pay again each month. If you bought one-time access before monthly subscriptions existed, that access has no end date.",
        "Where access is sold as a subscription, cancelling stops the next charge, and your access continues until the end of the period you have already paid for.",
        "To cancel a card subscription, use the manage-subscription link in the e-mails Paystack sends you, or e-mail us and we will cancel it for you. A USDT subscription ends by itself if you do not pay again.",
        "If a payment is late, your access continues for a grace period (currently 5 days) after the paid period ends. We send reminders on the dashboard and, if you linked Telegram, on Telegram.",
        "When paid access ends, the Service stops opening new live trades on your account and the account goes back to practice mode. Trades that are already open stay open: the bot keeps managing them with their stop-loss and exit rules until they close. We do not close, cancel or change anything on your exchange account because a payment is missing. If you want a position closed sooner, close it yourself on Binance.",
        "Payments are not refunded automatically. If you think you were charged by mistake, or you paid and did not receive access, e-mail us with the payment details. We decide each request and, if we approve a refund, we make it through the same payment provider and method. Refund times, and any provider or network fees, depend on Paystack and NOWPayments and on your bank or wallet. We do not refund trading losses.",
        "Payment for access is separate from the money in your exchange account. We never ask you to send trading capital to us.",
      ],
    },
    {
      heading: "12. Security",
      body: [
        "We use reasonable technical measures to protect the information we hold, including encryption of exchange keys. No internet service is completely secure. You must protect your own accounts and devices.",
      ],
    },
    {
      heading: "13. Third parties",
      body: [
        "The Service depends on others, such as Binance, Paystack, NOWPayments, Telegram and our hosting providers. We do not control them. If one of them fails, the Service may stop working, delay an order or miss one.",
      ],
    },
    {
      heading: "14. Suspension and ending",
      body: [
        "We may suspend or limit access where needed to protect the Service, other customers or third parties, including for suspected abuse, a security threat, unlawful use or a serious breach of these Terms.",
        "You can stop using the Service at any time. Turn off the bot and revoke the API key at Binance. You can ask us to delete your data (see the Privacy Policy).",
      ],
    },
    {
      heading: "15. Changes to the Service and to these Terms",
      body: [
        "Features, pairs, strategies and limits may change. If we change these Terms, the Privacy Policy or the Trading Risk Disclosure, we change its version and ask you to accept the new version before you continue to use your account's settings. The Trading Risk Disclosure is also shown again to customers who have live trading enabled. Trades that are open keep being managed while you have not yet accepted: accepting is needed to use the dashboard, not for the bot to manage a position.",
      ],
    },
    {
      heading: "16. Liability",
      body: [
        "To the extent the law allows, we are not responsible for losses that come from ordinary market movements, your own settings or decisions, failures of Binance or other third parties, or events outside our reasonable control, including a limit or stop that acts late, at a different price or not at all.",
        "Nothing in these Terms limits liability that the law does not allow to be limited.",
      ],
    },
    {
      heading: "17. Intellectual property",
      body: [
        "The software, design, name and content of the Service belong to their owners. You may not copy, reverse-engineer or resell them except as the law allows.",
      ],
    },
    {
      heading: "18. Governing law",
      body: [
        "These Terms are governed by the laws of the Federal Republic of Nigeria. Nothing here takes away rights you have under mandatory consumer law where you live.",
      ],
    },
    {
      heading: "19. Contact",
      body: [
        "Questions about these Terms, payments or refunds:",
        "Metabridge Academy",
        "Website: metabridgeacademy.com",
        "Email: info@metabridgeacademy.com",
      ],
    },
  ],
};

export const RISK_DISCLOSURE = {
  title: "Trading Risk Disclosure",
  effectiveDate: "21 November 2026",
  version: RISK_DISCLOSURE_VERSION,
  intro: [
    "This page explains the main risks of trading with the Metabridge Academy bot. Read it before you turn on live trading. You are asked to accept it before live trading starts, and again whenever we change it.",
  ],
  sections: [
    {
      heading: "You can lose all the money you trade with",
      body: [
        "Cryptocurrency prices can fall or rise a lot in minutes. The bot can lose part or all of your trading capital. We do not promise profit, and nothing the bot shows, including results and reports, guarantees a result. Trade only with money you can afford to lose.",
      ],
    },
    {
      heading: "Past results do not show future results",
      body: [
        "Practice results are simulated: no real orders, no real money, and prices, speed and fees can differ from live trading. Tests on past prices also do not show how a strategy will do next. A strategy that did well in a test can lose money live. Newer strategies have a shorter record than the default one.",
        "The absence of earlier losses does not mean losses cannot happen.",
      ],
    },
    {
      heading: "Risk per trade",
      body: [
        "You choose how much of your trading capital the bot may risk on one trade, from 0.1% up to a maximum shown in your settings (never more than 40%). The default is 1%. We recommend 2% or less. The higher you set it, the faster a short run of losing trades can reduce your capital. At 10% or more, a short run of losing trades can remove most of your capital.",
        "Above 2%, and again from 10%, you are asked to confirm that you understand this, and we record your confirmation with the date. The setting is your own decision.",
        "The setting is a target for the loss if a stop-loss order is hit. The real loss can be larger: an order can fill at a worse price, markets can move fast, and an order can fail to be placed or filled.",
      ],
    },
    {
      heading: "Safety limits are not a guarantee",
      body: [
        "The bot has limits that can stop it from opening new trades: the daily loss limit you set in US dollars, an automatic total loss stop when your account is 50% or more below its reference value, for Futures a check that the stop-loss is safely away from the estimated liquidation level, and a control we can use to stop new trades for all accounts.",
        "These limits stop NEW trades only. They never close a trade that is already open. They are measured from your exchange balance when the bot checks it, so a loss can pass a limit before the bot acts. The liquidation level the bot uses is an estimate and can differ from Binance's. If you deposit or withdraw money, the limits measure from the wrong value until the next day starts, or until you reset the total loss stop.",
        "The daily loss limit is an amount you choose. With a high risk setting, one losing trade can be larger than your daily limit. An open trade keeps its stop-loss.",
      ],
    },
    {
      heading: "Stop-losses can fail or fill badly",
      body: [
        "The bot places a protective stop-loss on Binance after it opens a trade, and closes the position if it cannot place one. A stop-loss is not a guarantee: in a fast or thin market it can fill at a much worse price, or not fill at all. If Binance is down, or your key stops working, the bot may not be able to act.",
      ],
    },
    {
      heading: "Futures and leverage",
      body: [
        "Leverage can make gains and losses much bigger. You set a leverage cap from 1x to 5x. The bot sizes a position to at most that multiple of your trading capital, or to Binance's own limit for the pair if that is lower. The bot reads your Binance settings and never changes them. It trades isolated margin only: a position can lose the money set aside for it, and Binance may close it when that money is used up. Check leverage and margin mode for each pair on your Binance account.",
      ],
    },
    {
      heading: "Several strategies",
      body: [
        "The bot may run more than one strategy on an account and, if you set it up that way, hold more than one open trade at a time (one per pair). Several open trades can lose at the same time. Your daily loss limit and the total loss stop apply to the whole account; they do not close trades that are already open.",
      ],
    },
    {
      heading: "Fees and what figures mean",
      body: [
        "Binance charges commissions on every trade and, on Futures, funding payments. Profit figures are \"before fees\" unless the bot says they are net; it says net only when the real exchange fees of every trade in that figure are known. Funding payments are never included. A trade whose fee could not be valued (for example a fee paid in another asset) is shown before fees.",
      ],
    },
    {
      heading: "Technology and exchange risk",
      body: [
        "Software can fail. Binance can have outages, delays, restrictions or security problems. Your internet, our servers and other services we rely on can fail. Orders can be delayed, missed or placed at a different price from the one expected. Watch your Binance account yourself, and know how to turn off the bot and revoke the API key at Binance.",
      ],
    },
    {
      heading: "API key",
      body: [
        "Give the bot trade permission only. Never enable withdrawal permission. If the bot finds that your key can withdraw, it stops opening new trades until you replace the key. If you think the key has been exposed, revoke it at Binance at once.",
      ],
    },
    {
      heading: "Your decision and your responsibility",
      body: [
        "Turning on live trading is your decision. When you do, real orders may be placed on your Binance account with real money, and you may lose money. Payment for access does not turn on live trading by itself.",
        "You are responsible for the risk setting, leverage cap, daily loss limit and trading capital you choose; for checking the settings of each pair on your Binance account; for resetting the total loss stop after you move money in or out; and for trading only money you can afford to lose.",
        "The bot does not give personal investment advice.",
      ],
    },
  ],
};

export const PRIVACY_POLICY = {
  title: "Privacy Policy",
  effectiveDate: "21 November 2026",
  version: PRIVACY_VERSION,
  intro: [
    "Metabridge Academy runs the trading bot service. This page says what personal information the service holds, why, who can see it, how long it is kept, and how to ask for a copy or for deletion.",
  ],
  sections: [
    {
      heading: "Who is responsible",
      body: [
        "Metabridge Academy (website: metabridgeacademy.com, e-mail: info@metabridgeacademy.com) decides how your information is used and is responsible for it.",
      ],
    },
    {
      heading: "What we hold",
      body: [
        {
          list: [
            "Your e-mail address, and a password stored only as a one-way hash (never the password itself).",
            "Your account settings: pair, Spot or Futures, risk per trade, leverage cap, trading capital, daily loss limit, notification channel, and the dates you started and enabled things.",
            "Your Binance API key and secret, stored encrypted. The secret is not shown again after you save it.",
            "Your Telegram chat identifier, if you link Telegram.",
            "Your trades and their results, and readings of your account value that the safety limits use.",
            "Your acceptances of the Terms, this Policy and the Risk Disclosure, each with its version and time, and your confirmations of higher risk settings.",
            "Payment records: amount, currency, provider, status and dates, and the provider's reference. We do not receive or store card numbers.",
            "Your requests to export or delete data.",
            "Technical logs and error records needed to run and protect the service.",
          ],
        },
        "We keep a sign-in cookie in your browser so you stay signed in.",
      ],
    },
    {
      heading: "Why we use it",
      body: [
        "To create and run your account, sign you in, place orders on your Binance account as you set it up, send notifications, take and check payments, show your results, find and fix errors, protect the service against abuse, give support, and meet legal duties.",
      ],
    },
    {
      heading: "Who sees it",
      body: [
        "The operator can see your information through an administration screen. It shows customers with their e-mail partly hidden (first letter and domain). To see a full e-mail address the operator must type a written reason, and each reveal and each administration request is written to an audit log; reveals are limited to 20 a day. The screen never shows your API key or secret, password, Telegram identifier or payment references.",
        "Others who process information for us or at your request: Binance (orders and balance reads for your account), Paystack and NOWPayments (payments), Telegram (notifications you chose) and our hosting providers. Each handles information under its own terms and privacy policy. We do not sell your information.",
      ],
    },
    {
      heading: "Keeping it safe",
      body: [
        "Exchange keys are encrypted. Access to the servers and to the administration screen is restricted. No internet service is completely secure.",
      ],
    },
    {
      heading: "How long we keep it",
      body: [
        "We keep your information while your account exists. The service has no automatic deletion: nothing is removed on a timer. Information stays until you ask us to delete it and the operator does so, or until the operator removes it.",
        "When we delete your data at your request we keep these records, because we need them: payment and subscription records (to reconcile payments, handle refunds and disputes and meet accounting duties); your acceptances of the Terms, Privacy Policy and Risk Disclosure with version and time (as proof of what you agreed to and when); and the record of your request and its outcome (as proof that it was made and handled).",
      ],
    },
    {
      heading: "Ask for a copy or for deletion",
      body: [
        "Sign in and use \"Your data\" on the Account page to ask for a copy of your data or for deletion. The request is recorded for the operator; it does not delete anything by itself. The operator reviews it, prepares the copy or deletes the data by hand, and replies to the e-mail on your account. You can also e-mail info@metabridgeacademy.com. Before deletion, close your open trades and revoke the API key at Binance yourself; deleting your data does not close anything on Binance.",
        "You can also ask us to correct information that is wrong.",
      ],
    },
    {
      heading: "Changes to this Policy",
      body: [
        "If we change this Policy we change its version and ask you to accept the new version.",
      ],
    },
    {
      heading: "Contact",
      body: [
        "Privacy questions:",
        "Metabridge Academy",
        "Website: metabridgeacademy.com",
        "Email: info@metabridgeacademy.com",
      ],
    },
  ],
};
