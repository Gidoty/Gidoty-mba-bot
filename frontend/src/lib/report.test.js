import assert from "node:assert/strict";
import test from "node:test";

import { basisLabel, feesText, headline, resultText, strategyName, winRateText } from "./report.js";

const live = { mode: "live", closed_trades: 4, wins: 3, losses: 1, gross_pnl: 10, fees: 1.5, trades_with_fees: 4, fees_complete: true, net_pnl: 8.5, simulated: false };

test("net is shown only when every trade's real fees are known", () => {
  assert.equal(basisLabel(live), "net, after fees");
  assert.equal(headline(live), 8.5);
  const partial = { ...live, fees_complete: false, net_pnl: null, trades_with_fees: 2 };
  assert.equal(basisLabel(partial), "before fees");
  assert.equal(headline(partial), 10); // gross, never an invented net
  assert.equal(feesText(partial), "1.50 (known for 2 of 4 trades)");
  assert.equal(feesText({ ...live, fees: null, trades_with_fees: 0, fees_complete: false, net_pnl: null }), "not captured");
});

test("a practice group is always simulated and never net", () => {
  const paper = { mode: "paper", closed_trades: 5, wins: 2, losses: 3, gross_pnl: -3, fees: null, trades_with_fees: 0, fees_complete: false, net_pnl: null, simulated: true };
  assert.equal(basisLabel(paper), "simulated, no fees or slippage");
  assert.equal(headline(paper), -3);
  assert.equal(feesText(paper), "none (simulated)");
  const sneaky = { ...paper, fees_complete: true, net_pnl: -1 };
  assert.equal(headline(sneaky), -3); // even if a net arrived for a simulated group it is not shown as the result
});

test("result, win rate and strategy names", () => {
  assert.equal(resultText(live, "USDT"), "+8.50 USDT");
  assert.equal(winRateText(live), "75%");
  assert.equal(winRateText({ closed_trades: 0 }), "-");
  assert.equal(strategyName("rsi_mr_v1"), "RSI mean reversion");
  assert.equal(strategyName(null), "EMA crossover");
  assert.equal(headline(null), null);
});

test("a net figure without complete fees is not shown as net, whatever else arrives", () => {
  const odd = { ...live, fees_complete: false, net_pnl: 8.5 };
  assert.equal(basisLabel(odd), "before fees");
  assert.equal(headline(odd), 10);
});
