"""Offline research tooling (Phase 0): historical data, an honest backtester,
metrics and validation scaffolding.

Nothing in here is imported by the trading service. The data downloader uses
only Binance's PUBLIC market-data endpoints with plain GET requests, no
credentials, and never imports configuration, the database or the exchange
client. See docs/research/README.md.
"""
