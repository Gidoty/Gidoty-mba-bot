"""Offline research tooling: only the public market-data downloader (data.py) and its command line (python -m mba_bot.research download).

Nothing in here is imported by the trading service. The downloader uses only Binance's PUBLIC market-data endpoints with plain GET requests, no
credentials, and never imports configuration, the database or the exchange client. It writes the candle files the two offline backtests read
(python -m mba_bot.rsi_backtest, python -m mba_bot.donchian_backtest). See docs/research/downloading_candles.md.

The rest of the research package (backtester engine, experiments, quality gate) stays on the parked research branch ccr-7642644c-2jb7jq.
"""
