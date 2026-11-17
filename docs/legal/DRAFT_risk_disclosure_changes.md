# DRAFT: proposed changes to the Trading Risk Disclosure and the Terms of Service

**Status: DRAFT, for review by the owner and his lawyer. This is not legal advice.** It has not been published and is not wired into any screen. The live pages (`frontend/src/legalContent.js`, Terms and Risk Disclosure, both effective 18 October 2026) are unchanged. The wording below is a proposal written by the engineering side to describe what the product now does; the lawyer decides what the documents must say, in which words, and what law applies. Where this draft makes a statement about the law it is written as a question.

## 1. What the product does now (facts, with where they live in the code)

| Topic | What happens | Source |
|---|---|---|
| Risk per trade | A customer chooses from 0.1% up to a limit the service sets (`RISK_PCT_MAX`, never above 40%). The default for new accounts is 1%. 2% or less is recommended. | `risk_limits.py`, `config.py` |
| High-risk warnings | Above 2% the screen shows "Higher risk" and the customer must tick an acknowledgment; from 10% it shows red "Very high risk" and a second, stronger acknowledgment. The server refuses the setting without them and records them (document types `risk_ack_high`, `risk_ack_very_high`). | `risk_limits.py`, `api/routers/account.py`, `frontend/src/lib/riskBands.js` |
| The stored risk is a ceiling | The bot sizes each trade so that a stop-loss hit loses about the chosen share of the trading capital, then applies size ceilings; the loss actually realised can be smaller. It can also be larger when a stop fills at a worse price. | `risk.py`, `trader.py` |
| Daily loss pause | If the account's equity (balance plus open positions at current value) falls by the service's daily limit from its value at the start of the day (Lagos calendar day), no new trades open until the next day. The limit is the same for every account. Open trades keep their stops. | `account_loss.py` |
| Total loss stop | If equity falls below a reference value by max(10%, 3 x the risk setting), never more than 80%, no new trades open until the customer resets it on the dashboard. It never resets by itself. | `account_loss.py`, `risk_limits.py` |
| Deposits and withdrawals | Neither is adjusted for automatically. Moving money distorts the two limits above; the customer can reset the total loss limit afterwards. | `docs/safety/risk_per_trade.md` |
| Leverage (Futures), the "size cap" | The customer sets a number from 1x to 5x (3x by default). The bot sizes a position to at most that multiple of the trading capital, or Binance's own number for the pair if that is smaller (the size cap: a high Binance number does not stop a trade, it only limits its size). Isolated margin only. The bot reads Binance's settings and never changes them. | `leverage.py`, `trader.py` |
| Liquidation check | Before a Futures trade the bot estimates where Binance would liquidate the position (from Binance's published maintenance-margin tiers). If its stop-loss would sit too close to that estimate or beyond it, it does not open the trade. This is an estimate and can differ from Binance's real liquidation price. | `risk.py` (`assess_stop_viability`) |
| Practice mode | New accounts trade in a simulated environment: prices are read from Binance's practice server, orders are simulated, no real money. | `trader.py`, `exchange.py` |
| Live mode | Only after payment, a trade-only Binance key (withdrawals off), an explicit "enable live trading" acceptance and "Start Bot". | `onboarding.py` |
| What the limits do not do | They only stop NEW trades. An open trade is never closed by them. They do not guarantee that a loss stops at the limit. | all of the above |

## 2. Proposed changes to the Trading Risk Disclosure

### 2.1 New section: "Risk Per Trade Setting"
> The bot lets you choose how much of your trading capital it may risk on a single trade, from 0.1% up to a maximum shown in your settings (never more than 40%). We recommend 2% or less. The higher you set it, the faster a short run of losing trades can reduce your capital. At 10% or more, a short run of losing trades can remove most of your capital. When you choose more than 2%, and again from 10%, you are asked to confirm that you understand this, and we record your confirmation. The setting is your own decision. The service does not advise you on what to choose.
>
> The risk setting is a target for the loss if a stop-loss order is hit. The actual loss can be larger, for example when an order fills at a worse price than planned, in a fast market, or if an order cannot be placed or filled.

### 2.2 New section: "Safety Limits and Their Limits"
> The bot includes limits that can stop it from opening new trades: a daily loss pause, a total loss stop, and a check before each Futures trade that the stop-loss sits safely away from the estimated liquidation level. These limits only stop new trades. They never close a trade that is already open, and an open trade keeps its stop-loss and exit rules.
>
> These limits are not a guarantee. They are measured from your exchange balance at the times the bot checks it, so a loss can go past a limit before the bot acts. The liquidation level the bot uses is an estimate and can differ from the level Binance applies. If you deposit or withdraw money, the limits measure from the wrong value until you reset them or the next day starts. The daily limit is the same for every account whatever risk setting you choose. At a high risk setting, one losing trade can reach or pass it.

### 2.3 Amend "Futures and Leverage" (add)
> You set a leverage number on your dashboard. The bot sizes a position to at most that multiple of your trading capital, or to Binance's own number for the pair if that is lower. The bot reads your settings on Binance and never changes them: you keep control of leverage and margin mode on your Binance account. The service trades isolated margin only. In isolated margin a position can lose the money set aside for it, and Binance may close it when that money is used up. Binance's leverage and margin mode can differ between pairs, so check each pair you trade.

### 2.4 Amend "Testnet Results" (rename and add)
> Heading: "Practice Mode (Simulated Trading)". Practice mode uses prices from Binance's practice server and simulated orders; no real money is used. Results in practice mode differ from live results, for example in prices, liquidity, speed and fees. Practice results do not show that live trading will be profitable.

### 2.5 Amend "No Guaranteed Profit" (add)
> Nothing in the service, including its risk setting, safety limits, checks and notifications, guarantees a profit, limits a loss to a particular amount, or protects your capital.

### 2.6 Amend "User Responsibility" (add)
> You are responsible for the risk setting and the leverage number you choose, for checking the settings of each pair on your Binance account, and for not moving money in or out of your account while the bot is trading unless you then reset the limits as the dashboard describes.

## 3. Proposed changes to the Terms of Service (for the lawyer to place)

1. The customer chooses their own risk setting within the limits the service sets, and the service may change those limits (for example the maximum risk setting) and the safety limits, with notice as the lawyer decides.
2. The service may stop opening new trades at any time, including automatically when a limit applies, and is not required to close open trades because of a limit.
3. The customer's confirmations of high-risk settings are recorded with the date and version and may be relied on by the service.
4. Nothing in the service is investment, financial, legal or tax advice, and the recommendation of 2% or less is a general statement, not advice for the customer's circumstances.
5. Practice mode is simulated; results are not a representation of live results.

## 4. Questions for the lawyer (not answered here)

1. Does offering automated trading with a customer-selectable risk of up to 40% of capital per trade change how the service must be classified, licensed or described under Nigerian securities or other law? Is a lower maximum advisable for retail customers?
2. Are the recorded acknowledgments (above 2% and from 10%) enough as informed consent, or is something more required (a longer notice, a cooling-off delay, a knowledge check, an age or eligibility check)?
3. How should the documents describe the total loss stop and daily pause so that customers do not read them as a capital guarantee?
4. Should existing customers be asked to re-accept the updated Risk Disclosure, and before or after the maximum risk setting is raised above 2%?
5. Is the wording about leverage, margin and liquidation, and about the bot never changing Binance settings, accurate enough given that the exchange's rules are not the service's own?
6. Does the Privacy Policy need a change because the service now stores the customer's acknowledgments and equity figures used for the limits?

## 5. What a version bump would affect (engineering facts, for deciding how to publish)

| If you change | What happens in the code today |
|---|---|
| `TERMS_VERSION` or `PRIVACY_VERSION` (`legal.py`) | Every existing customer stops having a current acceptance. `/api/auth/me` reports it, every `/api/account/*` route returns 403 (`require_legal_acceptance`), and the dashboard shows the consent gate until the customer accepts both again. The bot itself keeps running: the trading cycle does not check legal acceptance, so open trades and the limits continue, but the customer cannot change settings or reset the total loss limit until they re-accept. |
| `RISK_DISCLOSURE_VERSION` (`legal.py`) | It only changes the version recorded the next time a customer enables live trading. No gate reads the Risk Disclosure acceptance today, so existing customers are NOT asked to re-accept and nothing blocks them. If the owner wants existing live customers to re-accept the new Risk Disclosure, a new gate is needed (a code change, not just a version bump). |
| `ACK_VERSION` (`risk_limits.py`) | Existing risk acknowledgments stop counting. A customer saving a risk above 2% (or from 10%) is refused until they tick again; the bot keeps trading at the stored risk. |
| The effective date and text in `legalContent.js` | Display only; the code does not compare them with the versions above, so the dates and the constants must be changed together by hand. |

None of these has been changed in this PR.
