// "How to use this bot": a walk through the whole app in the order a new customer meets it.
// Plain words, short steps, no promise of profit. Every statement here describes a screen or a
// rule the app already has; if a screen changes, change the matching step.
export function howToSections(serverIp) {
  return [
    {
      title: "Getting set up",
      steps: [
        ["Sign up", "Create an account with your email and a password, and accept the Terms of Service and the Privacy Policy. You do not need a card or a Binance account yet."],
        ["Link Telegram", "On the Notifications page, choose \"Open Telegram and link\", then press Start in Telegram. The bot does not scan or trade until Telegram is linked."],
        ["Try practice mode", "Every account starts in practice mode. The bot reads prices from Binance's practice server and simulates every trade. No order is sent and no real money is used. Watch it on the Dashboard."],
        ["Activate when you are ready", "Choose Activate on the Dashboard. It is a one-time payment. It does not turn on live trading."],
        ["Create a Binance API key", "In Binance, open API Management and create a standard trading key. Turn on Reading and Trading. Leave withdrawals and transfers switched off, so the bot can never move your funds out."],
        ["Add the server address", `On the key, choose \"Restrict access to trusted IPs only\" and add Metabridge's server address: ${serverIp}. The key then works only from that address.`],
        ["Connect Binance", "On the Dashboard, follow \"Connect Your Binance Account\": paste the key, then the secret, and choose Verify Connection. Keep the secret private and never send it to anyone."],
      ],
    },
    {
      title: "Reading your screens",
      steps: [
        ["Dashboard", "Exchange balance is the free balance Binance reports. Today, 7D, 30D and All-time P&L add up your closed trades. Win rate is the share of closed trades that ended with a gain. Profit figures are before exchange fees and funding, so your Binance balance is the exact figure. In practice mode every figure is simulated."],
        ["Positions", "Shows a trade that is open now: its entry, its stop-loss and how far the price is from it. A stop-loss closes the trade if the price moves against it. For live trades it rests at Binance."],
        ["Trade History", "Lists every closed trade with its result, how it closed (for example stop-loss or signal exit) and its date."],
        ["Performance", "Choose 7D, 30D, 90D or All. You see the cumulative result, wins and losses, and results by month. With fewer than 30 closed trades the figures are greyed out, because that is too few to read much from."],
      ],
    },
    {
      title: "Your settings and account",
      steps: [
        ["Bot Settings", "Trading capital is the most of your Binance balance, in dollars, that the bot may use. Risk per trade is set between 0.1% and 2%. You also choose the pair and the market, spot or futures. Pair, market and pair selection mode cannot be changed while a trade is open. Risk per trade and trading capital can be changed at any time. Appearance sets Auto, Light or Dark."],
        ["Notifications", "Telegram messages tell you when a trade opens or closes, when a position needs attention, and a daily summary."],
        ["Account", "Shows your email, your account status and your bot code. Keep the bot code handy: support will ask for it."],
      ],
    },
    {
      title: "Going live",
      steps: [
        ["Enable live trading", "When Binance is connected, choose Enable Live Trading, read the notice, tick the box and confirm. This allows live orders. It does not start the bot. It cannot be done while a practice trade is open."],
        ["Set your capital and start the bot", "Enter your trading capital, then choose Start Bot. From then on the bot runs on our servers. You do not need to keep the page open. Start Bot cannot be pressed while a practice trade is open."],
        ["Stopping", "There is no Pause button. To step back from live trading, replace your Binance key with a practice (testnet) key on the Exchange Connection page. That turns live trading off for the account. It cannot be done while a trade is open: wait for the trade to close first. If you need help, contact support with your bot code."],
      ],
    },
  ];
}
