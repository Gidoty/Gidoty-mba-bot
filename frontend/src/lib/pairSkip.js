// The notice about a pair the bot skipped because the customer's trading capital cannot meet its smallest order (the server writes the text; it is set only while SMALL_CAPITAL_PAIRS is on).
// Run with: node --test frontend/src/lib/pairSkip.test.js (also run by tests/test_frontend_lib.py)
export function pairSkipNotice(account) {
  const text = account?.pair_skip_message;
  return typeof text === "string" && text.length > 0 ? text : null;
}
