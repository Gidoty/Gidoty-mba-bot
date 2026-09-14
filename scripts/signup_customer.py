#!/usr/bin/env python
"""Create a customer + trading account from the command line.

There's no signup web UI in this repo yet (see README) - this script is
the stand-in for "what the signup form submits", so the DB schema and
onboarding rules (trial dates, the live-trading bypass disclosure) can be
exercised end to end. A real signup form should call the same
`mba_bot.onboarding` functions this script calls, not duplicate the logic.

Usage:
    python scripts/signup_customer.py \\
        --email customer@example.com \\
        --pair BTC/USDT --market-type spot \\
        --notification-channel telegram --notification-target 123456789 \\
        --api-key <mainnet-key> --api-secret <mainnet-secret>

    # to skip the paper trial and go straight to live trading:
    python scripts/signup_customer.py ... --skip-trial --accept-live-risk-disclosure
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mba_bot.config import SUPPORTED_MARKET_TYPES, SUPPORTED_NOTIFICATION_CHANNELS, SUPPORTED_PAIRS, load_settings  # noqa: E402
from mba_bot.crypto_utils import encrypt  # noqa: E402
from mba_bot.db import init_db, session_scope  # noqa: E402
from mba_bot.models import ApiCredential, Customer, TradingAccount  # noqa: E402
from mba_bot.onboarding import LIVE_TRADING_DISCLOSURE, accept_live_bypass, start_trial  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--pair", required=True, choices=SUPPORTED_PAIRS)
    parser.add_argument("--market-type", required=True, choices=SUPPORTED_MARKET_TYPES)
    parser.add_argument("--notification-channel", required=True, choices=SUPPORTED_NOTIFICATION_CHANNELS)
    parser.add_argument("--notification-target", required=True, help="Telegram chat id / WhatsApp number")
    parser.add_argument("--risk-pct", type=float, default=None, help="defaults to RISK_PCT_DEFAULT, capped at RISK_PCT_MAX")
    parser.add_argument("--api-key", required=True, help="customer's mainnet Binance API key (trade-only, no withdrawal)")
    parser.add_argument("--api-secret", required=True)
    parser.add_argument(
        "--skip-trial",
        action="store_true",
        help="go straight to live trading instead of the 7-day paper trial",
    )
    parser.add_argument(
        "--accept-live-risk-disclosure",
        action="store_true",
        help="required alongside --skip-trial: confirms the customer checked the disclosure box",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.skip_trial and not args.accept_live_risk_disclosure:
        raise SystemExit("--skip-trial requires --accept-live-risk-disclosure (the logged, timestamped consent)")

    settings = load_settings()
    if not settings.encryption_key:
        raise SystemExit("ENCRYPTION_KEY is not set - generate one with `python -m mba_bot.crypto_utils`")
    init_db(settings.database_url)

    risk_pct = args.risk_pct if args.risk_pct is not None else settings.risk_pct_default

    with session_scope() as session:
        customer = Customer(email=args.email)
        session.add(customer)
        session.flush()

        account = TradingAccount(
            customer_id=customer.id,
            pair=args.pair,
            market_type=args.market_type,
            notification_channel=args.notification_channel,
            notification_target=args.notification_target,
            risk_pct=risk_pct,
            trial_ends_at=start_trial(trial_days=settings.trial_days),
            paper_balance=settings.paper_starting_balance,
        )
        if args.skip_trial:
            account.live_bypass_accepted_at = accept_live_bypass()
            account.live_bypass_disclosure_text = LIVE_TRADING_DISCLOSURE
        session.add(account)
        session.flush()

        credential = ApiCredential(
            account_id=account.id,
            exchange="binance",
            encrypted_api_key=encrypt(args.api_key, key=settings.encryption_key),
            encrypted_api_secret=encrypt(args.api_secret, key=settings.encryption_key),
        )
        session.add(credential)

        print(f"created customer {customer.id} ({customer.email}), account {account.id}")
        print(f"mode: {'LIVE (trial bypassed)' if args.skip_trial else f'PAPER until {account.trial_ends_at.isoformat()}'}")


if __name__ == "__main__":
    main()
