"""Enrol an admin's TOTP authenticator. Run by the owner ON THE SERVER, never over the web (docs/security/admin_dashboard.md).

    cd /opt/mba-bot && PYTHONPATH=src .venv/bin/python -m mba_bot.admin_enroll --email you@example.com

The email must be in ADMIN_EMAILS and belong to an existing customer. The tool prints the setup key and the recovery codes ONCE, to the terminal, and stores only the encrypted key and hashed recovery codes.
Run again with --replace to start over (the old authenticator and codes stop working). Nothing is printed to a log; nothing is sent anywhere."""
from __future__ import annotations

import argparse
import sys

from . import admin_auth
from .config import load_settings
from .db import init_db, session_scope
from .models import Customer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enrol an admin's authenticator app (run on the server by the owner).")
    parser.add_argument("--email", required=True)
    parser.add_argument("--replace", action="store_true", help="replace an existing enrolment (the old authenticator and recovery codes stop working)")
    args = parser.parse_args(argv)
    settings = load_settings()
    email = args.email.strip().lower()
    if not admin_auth.is_admin_email(email, settings.admin_emails):
        print("That email is not in ADMIN_EMAILS. Add it to the environment first, then restart.", file=sys.stderr)
        return 2
    if not settings.encryption_key:
        print("ENCRYPTION_KEY is not set.", file=sys.stderr)
        return 2
    init_db(settings.database_url, wal=settings.sqlite_wal, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
    with session_scope() as db:
        customer = db.query(Customer).filter(Customer.email == email).first()
        if customer is None:
            print("No customer has that email. Sign up through the website first.", file=sys.stderr)
            return 2
        made = admin_auth.enrol(db, customer.id, encryption_key=settings.encryption_key, replace=args.replace)
    if made is None:
        print("This admin is already enrolled. Use --replace to start over.", file=sys.stderr)
        return 3
    secret, codes = made
    print("Setup key (type it into your authenticator app; shown once):")
    print(f"  {secret}")
    print("Or open this link on the phone that has the app (it contains the same key):")
    print(f"  {admin_auth.provisioning_uri(secret, email)}")
    print("Recovery codes (each works once; keep them offline; shown once):")
    for c in codes:
        print(f"  {c[:4]}-{c[4:8]}-{c[8:12]}-{c[12:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
