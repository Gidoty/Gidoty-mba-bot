-- Read-only export of PAPER trades, for comparing them with mainnet candles (docs/ops/paper_source_and_replay.md).
-- Run against a COPY of the database, or read-only:
--   sqlite3 -readonly /path/to/mba_bot.db < docs/ops/paper_trades_export.sql > paper_trades.csv
-- It selects only: a short trade reference, a 4-character account prefix (not the account id), the market, pair, side, strategy, times, prices, quantity, pnl, close reason and the risk amount.
-- No customer e-mail, no account id, no key, no Telegram id.
.headers on
.mode csv
SELECT
  substr(t.id, 1, 8)                                   AS trade_ref,
  substr(t.account_id, 1, 4)                           AS account_ref,
  a.market_type                                        AS market_type,
  COALESCE(t.pair, a.pair)                             AS pair,
  t.side                                               AS side,
  COALESCE(NULLIF(t.strategy_id, ''), 'ema_cross_atr_v1') AS strategy_id,
  t.status                                             AS status,
  t.opened_at                                          AS opened_at,
  t.closed_at                                          AS closed_at,
  t.entry_price                                        AS entry_price,
  t.stop_loss_price                                    AS stop_loss_price,
  t.exit_price                                         AS exit_price,
  t.quantity                                           AS quantity,
  t.pnl                                                AS pnl,
  t.close_reason                                       AS close_reason,
  ABS(t.entry_price - t.stop_loss_price) * t.quantity  AS risk_amount
FROM trades t
JOIN trading_accounts a ON a.id = t.account_id
WHERE t.is_paper = 1
ORDER BY t.opened_at;
