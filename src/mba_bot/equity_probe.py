"""Read-only equity probe for the owner to run on the server: does the daily-loss equity reading work for each LIVE account?

    python -m mba_bot.equity_probe        (from the app directory, with DATABASE_URL and ENCRYPTION_KEY as the service has them)

For each live account it calls the SAME function the service uses (`trader._live_equity`: one balance read, plus one ticker read per open Spot position) and prints
one line: the account id, the market type and either `equity=<number with 2 decimals>` or `FAILED <class>`. It prints no key, no secret, no per-asset balance, no
customer data and no exchange reply, and a failure is only ever one of the fixed class names below, never exception text. It opens the database read-only
(`PRAGMA query_only`, the way `restart_gate` does: no init_db, no create_all, nothing created), places no order, cancels nothing and changes no setting. The
exchange calls it makes are Binance GETs through ccxt: the account endpoint (and, for Spot positions, the ticker), plus ccxt's own public market-data load.

Exit codes: 0 every live account was read, 2 at least one failed or the database/settings could not be read, 3 there is no live account.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import exchange
from .billing.entitlement import is_entitled
from .config import load_settings
from .models import TradingAccount
from .onboarding import TradingMode, resolve_trading_mode
from .restart_gate import DEFAULT_URL, _engine

EXIT_OK, EXIT_FAILED, EXIT_NO_LIVE = 0, 2, 3
F_NO_CREDENTIALS = "no_credentials"
F_CLIENT = "client_build_failed"
F_UNEXPECTED = "unexpected_error"


def _is_live(account: TradingAccount, settings, now: datetime) -> bool:
    state = resolve_trading_mode(
        trial_ends_at=account.trial_ends_at, live_bypass_accepted_at=account.live_bypass_accepted_at,
        has_paid=is_entitled(account.customer, settings=settings, now=now), live_trading_enabled_at=account.live_trading_enabled_at,
        has_mainnet_credential=account.has_mainnet_credential, bot_started_at=account.bot_started_at, now=now,
    )
    return state.mode != TradingMode.PAPER


async def probe_account(session: Session, account: TradingAccount, settings) -> str:
    """One output line for one live account. Never raises, never prints anything but fixed class names and a rounded number."""
    from . import trader  # imported late: the trader module pulls in the whole service

    head = f"{account.id} {account.market_type}"
    try:
        key, secret = trader._resolve_credentials(account, settings, is_paper=False)
    except Exception:  # noqa: BLE001
        return f"{head} FAILED {F_NO_CREDENTIALS}"
    if key is None:
        return f"{head} FAILED {F_NO_CREDENTIALS}"
    try:
        client = exchange.build_exchange_client(exchange="binance", market_type=account.market_type, api_key=key, api_secret=secret, sandbox=False)
    except Exception:  # noqa: BLE001
        return f"{head} FAILED {F_CLIENT}"
    try:
        equity, reason = await trader._live_equity(session, account, client)
    except Exception:  # noqa: BLE001
        return f"{head} FAILED {F_UNEXPECTED}"
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
    if equity is None:
        return f"{head} FAILED {reason}"
    return f"{head} equity={equity:.2f}"


async def run(url: str | None = None) -> tuple[list[str], int]:
    url = url or os.environ.get("DATABASE_URL", DEFAULT_URL)
    if url.startswith("sqlite") and ":memory:" not in url:
        path = url.split("///", 1)[-1].split("?", 1)[0]
        if path and not os.path.exists(path):
            raise FileNotFoundError("database file not found")  # opening a missing file would create it
    settings = load_settings()
    engine = _engine(url)
    lines: list[str] = []
    try:
        now = datetime.now(timezone.utc)
        with Session(engine) as session:
            accounts = list(session.scalars(select(TradingAccount).order_by(TradingAccount.id)))
            live = [a for a in accounts if _is_live(a, settings, now)]
            for account in live:
                lines.append(await probe_account(session, account, settings))
    finally:
        engine.dispose()
    failed = sum(1 for line in lines if " FAILED " in line)
    return lines, (EXIT_NO_LIVE if not lines else EXIT_FAILED if failed else EXIT_OK)


def main(argv: list[str] | None = None) -> int:
    try:
        lines, code = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - fixed class name only, never the message
        print(f"equity probe: cannot start ({type(exc).__name__})", file=sys.stderr)
        return EXIT_FAILED
    for line in lines:
        print(line)
    print(f"equity probe: {len(lines)} live account(s), {sum(1 for l in lines if ' FAILED ' in l)} failed")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
