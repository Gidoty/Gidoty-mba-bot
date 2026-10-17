#!/usr/bin/env python
"""Manually unlock lifetime access for a customer who paid outside Paystack
(bank transfer, cash, a referral you're comping, ...).

Every account is locked by default - the scheduler won't trade it until
`Customer.has_paid` is set, either by the Paystack webhook on a real
payment or by this script. Run this on the server (SSH), not from
anywhere a customer could reach - it has no auth of its own beyond
whoever can run commands on your VPS.

Usage:
    python scripts/mark_customer_paid.py --email customer@example.com
    python scripts/mark_customer_paid.py --email customer@example.com --reference "UBA transfer 12 Oct"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mba_bot.config import load_settings  # noqa: E402
from mba_bot.db import init_db, session_scope  # noqa: E402
from mba_bot.models import Customer  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--reference", default=None, help="optional note for your own records, e.g. a bank transfer reference")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()
    init_db(settings.database_url)

    with session_scope() as session:
        customer = session.query(Customer).filter(Customer.email == args.email.lower()).first()
        if customer is None:
            raise SystemExit(f"no customer found with email {args.email}")
        if customer.has_paid:
            print(f"{customer.email} already has lifetime access - nothing to do")
            return

        customer.has_paid = True
        if args.reference:
            customer.paystack_reference = args.reference
        if customer.account is not None:
            customer.account.is_active = True

        bot_code = customer.account.bot_code if customer.account else None
        print(f"unlocked lifetime access for {customer.email}" + (f" (bot code: {bot_code})" if bot_code else ""))


if __name__ == "__main__":
    main()
