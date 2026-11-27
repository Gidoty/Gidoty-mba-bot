// The practice-mode notice on the Dashboard: a short visible label with an info button that opens the full text (components/InfoHelp.jsx). The wording is the paragraph the Dashboard showed before, unchanged.
export const PRACTICE_NOTICE_LABEL = "Practice mode";

export const PRACTICE_NOTICE_TEXT =
  "Everything on this page is simulated. The bot reads prices from Binance's practice server, then records each practice trade itself. No order is sent to Binance, no real money is involved, and nothing here can be withdrawn. Real trades can fill at different prices and pay fees.";

export const PRACTICE_NOTICE_HELP = { title: PRACTICE_NOTICE_LABEL, blocks: [{ text: PRACTICE_NOTICE_TEXT }] };
