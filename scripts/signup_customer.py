#!/usr/bin/env python
"""Create a customer + trading account from the command line.

There's no signup web UI in this repo yet (see README) - this script is
the stand-in for "what the signup form submits", so the DB schema and
onboarding rules (trial dates, the live-trading bypass disclosure) can be
exercised end to end. A real signup form should call the same
`mba_bot.onboarding` functions this script calls, not duplicate the logic.

Usage:
    python scripts/signup_customer.py \\
        --email customer@example.com --password "a-strong-password" \\
        --pair BTC/USDT --market-type spot \\
        --notification-channel telegram --notification-target 123456789 \\
        --api-key <key> --api-secret <secret> --environment testnet

    # accounts are LOCKED by default (see has_paid in models.py) - add
    # --mark-paid to unlock immediately, or run mark_customer_paid.py later
    python scripts/signup_customer.py ... --mark-paid

    # to skip the 7-day trial wait (eligibility only - does not by
    # itself enable live trading, see below):
    python scripts/signup_customer.py ... --skip-trial --accept-live-risk-disclosure

    # to actually enable live trading, ALL of these are required: paid,
    # eligible (trial done or skipped), a mainnet-tagged credential, and
    # this flag with its own disclosure acceptance:
    python scripts/signup_customer.py ... \\
        --environment mainnet --mark-paid --skip-trial --accept-live-risk-disclosure \\
        --enable-live-trading --accept-live-activation-disclosure
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mba_bot.api.security import hash_password  # noqa: E402
from mba_bot.botcode import assign_unique_bot_code  # noqa: E402
from mba_bot.config import SUPPORTED_MARKET_TYPES, SUPPORTED_NOTIFICATION_CHANNELS, SUPPORTED_PAIRS, load_settings  # noqa: E402
from mba_bot.crypto_utils import encrypt  # noqa: E402
from mba_bot.db import init_db, session_scope  # noqa: E402
from mba_bot.api.schemas import CREDENTIAL_ENVIRONMENTS  # noqa: E402
from mba_bot.models import ApiCredential, Customer, TradingAccount  # noqa: E402
from mba_bot.onboarding import (  # noqa: E402
    LIVE_TRADING_ACTIVATION_DISCLOSURE,
    LIVE_TRADING_DISCLOSURE,
    accept_live_bypass,
    enable_live_trading,
    resolve_trading_mode,
    start_trial,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True, help="lets the customer log into the web dashboard later")
    parser.add_argument("--pair", required=True, choices=SUPPORTED_PAIRS)
    parser.add_argument("--market-type", required=True, choices=SUPPORTED_MARKET_TYPES)
    parser.add_argument("--notification-channel", required=True, choices=SUPPORTED_NOTIFICATION_CHANNELS)
    parser.add_argument("--notification-target", required=True, help="Telegram chat id / WhatsApp number")
    parser.add_argument("--risk-pct", type=float, default=None, help="defaults to RISK_PCT_DEFAULT, capped at RISK_PCT_MAX")
    parser.add_argument("--api-key", required=True, help="trade-only, no withdrawal - testnet or mainnet, see --environment")
    parser.add_argument("--api-secret", required=True)
    parser.add_argument(
        "--environment",
        choices=CREDENTIAL_ENVIRONMENTS,
        default="testnet",
        help="which Binance system --api-key/--api-secret are registered against (default: testnet, the safe default)",
    )
    parser.add_argument(
        "--skip-trial",
        action="store_true",
        help="become eligible for live trading sooner than the 7-day trial - eligibility only, does NOT by itself "
        "enable live trading, see --enable-live-trading",
    )
    parser.add_argument(
        "--accept-live-risk-disclosure",
        action="store_true",
        help="required alongside --skip-trial: confirms the customer checked the trial-bypass disclosure box",
    )
    parser.add_argument(
        "--enable-live-trading",
        action="store_true",
        help="the one thing that actually turns on live trading - requires --environment mainnet, --mark-paid, "
        "eligibility (--skip-trial or an already-expired trial), and --accept-live-activation-disclosure",
    )
    parser.add_argument(
        "--accept-live-activation-disclosure",
        action="store_true",
        help="required alongside --enable-live-trading: confirms the customer checked the live-activation disclosure box",
    )
    parser.add_argument(
        "--mark-paid",
        action="store_true",
        help="also unlock lifetime access immediately (accounts are locked by default - see has_paid in models.py)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.skip_trial and not args.accept_live_risk_disclosure:
        raise SystemExit("--skip-trial requires --accept-live-risk-disclosure (the logged, timestamped consent)")
    if args.enable_live_trading:
        if not args.accept_live_activation_disclosure:
            raise SystemExit("--enable-live-trading requires --accept-live-activation-disclosure")
        if args.environment != "mainnet":
            raise SystemExit("--enable-live-trading requires --environment mainnet - a testnet credential can never be used for it")
        if not args.mark_paid:
            raise SystemExit("--enable-live-trading requires --mark-paid")
        if not (args.skip_trial or args.accept_live_risk_disclosure):
            raise SystemExit("--enable-live-trading requires eligibility: pass --skip-trial --accept-live-risk-disclosure too")

    settings = load_settings()
    if not settings.encryption_key:
        raise SystemExit("ENCRYPTION_KEY is not set - generate one with `python -m mba_bot.crypto_utils`")
    init_db(settings.database_url)

    risk_pct = args.risk_pct if args.risk_pct is not None else settings.risk_pct_default

    with session_scope() as session:
        customer = Customer(email=args.email, password_hash=hash_password(args.password), has_paid=args.mark_paid)
        session.add(customer)
        session.flush()

        account = TradingAccount(
            customer_id=customer.id,
            bot_code=assign_unique_bot_code(session),
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
        if args.enable_live_trading:
            account.live_trading_enabled_at = enable_live_trading()
            account.live_trading_disclosure_text = LIVE_TRADING_ACTIVATION_DISCLOSURE
        session.add(account)
        session.flush()

        credential = ApiCredential(
            account_id=account.id,
            exchange="binance",
            environment=args.environment,
            encrypted_api_key=encrypt(args.api_key, key=settings.encryption_key),
            encrypted_api_secret=encrypt(args.api_secret, key=settings.encryption_key),
        )
        session.add(credential)

        state = resolve_trading_mode(
            trial_ends_at=account.trial_ends_at,
            live_bypass_accepted_at=account.live_bypass_accepted_at,
            has_paid=customer.has_paid,
            live_trading_enabled_at=account.live_trading_enabled_at,
            has_mainnet_credential=account.has_mainnet_credential,
        )

        print(f"created customer {customer.id} ({customer.email}), account {account.id}")
        print(f"bot code: {account.bot_code}")
        print(f"credential environment: {args.environment}")
        print(f"mode: {state.mode.value.upper()}")
        print(f"access: {'UNLOCKED' if args.mark_paid else 'LOCKED - run scripts/mark_customer_paid.py to unlock'}")


if __name__ == "__main__":
    main()
