import { useId } from "react";

import { oneTradeCanExceed, pctOfValue, pendingNotice, suggestedText } from "../lib/lossLimit.js";
import { usd } from "../lib/format.js";
import { inputClass } from "./form.jsx";

// The customer's daily loss limit, in US dollars. The customer types the amount (nothing is filled in for them); the screen shows it as a percent of the account value, suggests
// about 5%, says when a raise starts, and warns when one trade can lose more than the limit. The server checks the number again and refuses live trading without one.
export default function DailyLossLimitField({ value, onChange, account, riskPct, capital, problem }) {
  const id = useId();
  const hintId = `${id}-hint`;
  const ref = account.account_value_reference;
  const pct = pctOfValue(value, ref);
  const suggestion = suggestedText(ref);
  const pending = pendingNotice(account);
  const unchangedDefault = account.daily_loss_limit_is_default && String(account.daily_loss_limit_usd ?? "") === String(value);
  const exceeds = oneTradeCanExceed(riskPct, capital, value);
  return (
    <div>
      <label htmlFor={id} className="text-sm font-medium text-fg">
        Daily loss limit (US dollars)
      </label>
      <div className="relative">
        <span aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted">$</span>
        <input id={id} type="number" min="0.01" step="0.01" inputMode="decimal" value={value} onChange={(e) => onChange(e.target.value)} className={`${inputClass} pl-7`} aria-describedby={hintId} aria-invalid={problem ? "true" : undefined} />
      </div>
      <span id={hintId} className="mt-1 block text-xs text-muted">
        If your account falls by this amount in one day, the bot opens no new trades until midnight Lagos time. Open trades keep their stop-loss.
        {suggestion ? ` We suggest about 5% of your account value: ${suggestion}.` : ""}
      </span>
      {pct !== null && <p className="mt-1 text-sm text-fg">That is {pct}% of your account value of {usd(ref)}.</p>}
      {problem && <p role="alert" className="mt-2 text-sm font-medium text-neg">{problem}</p>}
      {account.daily_loss_limit_required && !problem && <p className="mt-2 text-sm font-medium text-fg">Required before live trading.</p>}
      {unchangedDefault && (
        <p role="status" className="mt-2 rounded-ctl border border-line border-l-4 border-l-warn bg-warn-soft px-3 py-2 text-sm font-medium text-fg">
          This is a starting limit we set for you ({usd(account.daily_loss_limit_usd)}). Check it and save your own amount.
        </p>
      )}
      {pending && <p role="status" className="mt-2 text-sm font-medium text-fg">{pending}</p>}
      {exceeds && !problem && (
        <p role="status" className="mt-2 rounded-ctl border border-line border-l-4 border-l-warn bg-warn-soft px-3 py-2 text-sm font-medium text-fg">
          With this risk setting, one trade can lose more than this limit. Open trades keep their stop-loss.
        </p>
      )}
      {account.daily_loss_limit_enforced === false && <p className="mt-2 text-xs text-muted">The service is not enforcing the daily limit yet. Your amount is saved for when it does.</p>}
    </div>
  );
}
