"""Read-only pilot status for the owner: one screen per LIVE account.

    python -m mba_bot.pilot_status        (from the app directory, with DATABASE_URL and the service's environment as the service has them)

It opens the database READ-ONLY (`PRAGMA query_only`, the way `restart_gate` does: no init_db, no create_all, nothing created or written), makes NO exchange call, places no order and changes no setting.
Everything it shows comes from the database and from the settings the environment defines, so the equity is the last sample the service stored (with its age), not a live read: use
`python -m mba_bot.equity_probe` for a live read.

Per live account: the market, pair and risk settings, the switches that are on, the day-start equity and the last equity sample, the daily pause and the total loss stop, the open trades, the last signal and
last error (both customer-safe texts already stored), and the last 10 decisions. It prints an account's first 8 characters only, and never a key, a secret, an e-mail, a balance per asset or an exchange reply.

Exit codes: 0 printed, 2 the database or settings could not be read, 3 no live account."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from . import account_loss, leverage as leverage_module, risk_limits
from .config import load_settings
from .equity_probe import _is_live
from .models import (AccountDayEquity, AccountLossPause, AccountTotalLossStop, DecisionLog, KillSwitch, StrategyPause, Trade, TradingAccount, NON_TERMINAL_TRADE_STATUSES)
from .restart_gate import DEFAULT_URL, _engine

EXIT_OK, EXIT_UNREADABLE, EXIT_NO_LIVE = 0, 2, 3

# (environment name, Settings attribute, the value that means "off / not in use"). Only these are ever printed, and only their plain values: nothing secret is in this list.
SWITCHES = (
    ("RISK_PCT_MAX", "risk_pct_max", 2.0), ("ACCOUNT_DAILY_LOSS_PCT", "account_daily_loss_pct", 0.0), ("ACCOUNT_DAY_TIMEZONE", "account_day_timezone", None),
    ("ACCOUNT_TOTAL_LOSS_STOP", "account_total_loss_stop", False), ("FUTURES_MAX_LEVERAGE", "futures_max_leverage", 0.0), ("FUTURES_LEVERAGE_MODE", "futures_leverage_mode", "strict"),
    ("PROTECTION_PRECHECK_MODE", "protection_precheck_mode", "off"), ("PROTECTION_FAIL_LIMIT_MODE", "protection_fail_limit_mode", "off"), ("STRATEGY_GUARDS_MODE", "strategy_guards_mode", "off"),
    ("NEW_STRATEGY_GUARDS_MODE", "new_strategy_guards_mode", "off"), ("EMA_CLOSED_CANDLE_ONLY", "ema_closed_candle_only", False), ("FOREIGN_ORDER_ALARM", "foreign_order_alarm", "off"),
    ("TRADE_TELEMETRY", "trade_telemetry", False), ("MAX_OPEN_POSITIONS", "max_open_positions", 1), ("MAX_TOTAL_RISK_PCT", "max_total_risk_pct", 2.0),
    ("RSI_MR_ENABLED", "rsi_mr_enabled", False), ("DONCHIAN_ENABLED", "donchian_enabled", False), ("FUTURES_PAIR_EXPANSION", "futures_pair_expansion", False),
)


def _fmt_time(value) -> str:
    if value is None:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _age(value, now: datetime) -> str:
    if value is None:
        return "no sample"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    minutes = int((now - value).total_seconds() // 60)
    return f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago"


def _num(value, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def switch_lines(settings) -> tuple[list[str], list[str]]:
    on, off = [], []
    for env, attr, off_value in SWITCHES:
        value = getattr(settings, attr, None)
        shown = f"{env}={value}"
        (off if off_value is not None and value == off_value else on).append(shown)
    return on, off


def global_lines(session: Session, settings) -> list[str]:
    ks = session.get(KillSwitch, 1)
    state = "engaged" if (ks is not None and ks.engaged) else "released" if ks is not None else "NO ROW (the service treats this as engaged)"
    on, off = switch_lines(settings)
    return [f"kill switch: {state}" + (f" (since {_fmt_time(ks.changed_at)})" if ks is not None and ks.engaged else ""),
            "switches on:  " + (", ".join(on) if on else "none"), "switches off: " + (", ".join(off) if off else "none")]


def account_screen(session: Session, account: TradingAccount, settings, now: datetime) -> list[str]:
    tz = account_loss.zone(settings.account_day_timezone)
    today = account_loss.day_key(now, tz) if tz is not None else None
    lines = [f"=== account {account.id[:8]} | LIVE | {account.market_type} | {account.pair} ({account.pair_selection_mode}) ===",
             f"settings: risk {account.risk_pct:g}% (limit {settings.risk_pct_max:g}%), leverage setting {leverage_module.account_cap(account.max_leverage, settings.futures_max_leverage)}x"
             f"{'' if account.market_type == 'futures' else ' (Spot: not used)'}, allocated capital {_num(account.allocated_capital)}",
             f"cycle: last scanned {_fmt_time(account.last_scanned_at)}, last signal {account.last_signal or 'none'} at {_fmt_time(account.last_signal_at)}"]
    # equity: the last stored sample, never a live read
    day = session.execute(select(AccountDayEquity).where(AccountDayEquity.account_id == account.id).order_by(desc(AccountDayEquity.day_key)).limit(1)).scalar_one_or_none()
    if day is None:
        lines.append("equity: no sample stored yet (the daily pause is off or no live cycle has run since it was switched on)")
    else:
        loss = None if not day.start_equity else (day.start_equity - (day.last_equity or day.start_equity)) / day.start_equity * 100.0
        lines.append(f"equity: last sample {_num(day.last_equity)} ({_age(day.last_equity_at, now)}); day {day.day_key}: start {_num(day.start_equity)}"
                     f"{' (approximate: first read of the day)' if day.approximate else ''}; change since day start {_num(None if loss is None else -loss)}%")
    # daily pause
    pause = session.execute(select(AccountLossPause).where(AccountLossPause.account_id == account.id).order_by(desc(AccountLossPause.started_at)).limit(1)).scalar_one_or_none()
    if settings.account_daily_loss_pct <= 0:
        lines.append("daily pause: off (ACCOUNT_DAILY_LOSS_PCT is 0)")
    elif pause is not None and today is not None and pause.day_key == today and (pause.ends_at.replace(tzinfo=timezone.utc) if pause.ends_at.tzinfo is None else pause.ends_at) > now:
        lines.append(f"daily pause: ACTIVE since {_fmt_time(pause.started_at)} (down {pause.loss_pct:.2f}%, limit {pause.limit_pct:g}%), new entries resume at {_fmt_time(pause.ends_at)}")
    else:
        lines.append(f"daily pause: not active (limit {settings.account_daily_loss_pct:g}%, day {today or 'unknown: invalid ACCOUNT_DAY_TIMEZONE'})")
    # total loss stop
    threshold = risk_limits.total_loss_threshold(account.risk_pct)
    stop = account_loss.active_total_stop(session, account.id)
    ref = account.loss_reference_equity
    if not settings.account_total_loss_stop:
        lines.append(f"total loss stop: off (ACCOUNT_TOTAL_LOSS_STOP is off); would be {threshold:g}% for risk {account.risk_pct:g}%")
    elif stop is not None:
        lines.append(f"total loss stop: ACTIVE since {_fmt_time(stop.triggered_at)} (down {stop.loss_pct:.2f}%, limit {stop.threshold_pct:g}%); only the customer's reset on the dashboard releases it")
    else:
        change = None if (ref is None or not ref or day is None or day.last_equity is None) else (day.last_equity - ref) / ref * 100.0
        lines.append(f"total loss stop: not active; reference {_num(ref)} (set {_fmt_time(account.loss_reference_at)}), limit {threshold:g}% below it; change since reference {_num(change)}%")
    # open trades
    open_trades = list(session.scalars(select(Trade).where(Trade.account_id == account.id, Trade.status.in_(NON_TERMINAL_TRADE_STATUSES), Trade.is_paper.is_(False)).order_by(Trade.opened_at)))
    if not open_trades:
        lines.append("open trade: none")
    for t in open_trades:
        lines.append(f"open trade: {t.pair or account.pair} {t.side} {t.status} (protection {t.protection_status or 'n/a'}) qty {t.quantity:g} entry {t.entry_price:g} stop {t.stop_loss_price:g}, "
                     f"strategy {t.strategy_id or 'ema_cross_atr_v1'}, opened {_fmt_time(t.opened_at)}")
    pauses = list(session.scalars(select(StrategyPause).where(StrategyPause.account_id == account.id, StrategyPause.released_at.is_(None), StrategyPause.is_paper.is_(False))))
    if pauses:
        lines.append("strategy pauses: " + ", ".join(f"{p.strategy_id}:{p.code}" for p in pauses))
    lines.append(f"last error: {account.last_error or 'none'}" + (f" ({_fmt_time(account.last_error_at)})" if account.last_error else ""))
    decisions = list(session.scalars(select(DecisionLog).where(DecisionLog.account_id == account.id).order_by(desc(DecisionLog.ts)).limit(10)))
    lines.append(f"last {len(decisions)} decisions (newest first):" if decisions else "last decisions: none (TRADE_TELEMETRY is off or nothing has happened)")
    for d in decisions:
        lines.append(f"  {_fmt_time(d.ts)}  {d.stage:<9} {d.outcome:<9} {d.reason_code:<40} {d.pair or '-':<9} {d.strategy_id}")
    return lines


def render(url: str | None = None, now: datetime | None = None) -> tuple[list[str], int]:
    url = url or os.environ.get("DATABASE_URL", DEFAULT_URL)
    if url.startswith("sqlite") and ":memory:" not in url:
        path = url.split("///", 1)[-1].split("?", 1)[0]
        if path and not os.path.exists(path):
            raise FileNotFoundError("database file not found")  # opening a missing file would create it
    settings = load_settings()
    now = now or datetime.now(timezone.utc)
    engine = _engine(url)
    out: list[str] = [f"pilot status, {_fmt_time(now)} (read-only; no exchange call; equity is the last stored sample)"]
    try:
        with Session(engine) as session:
            out += global_lines(session, settings)
            accounts = [a for a in session.scalars(select(TradingAccount).order_by(TradingAccount.id)) if _is_live(a, settings, now)]
            for account in accounts:
                out.append("")
                out += account_screen(session, account, settings, now)
    finally:
        engine.dispose()
    return out, (EXIT_NO_LIVE if not accounts else EXIT_OK)


def main(argv: list[str] | None = None) -> int:
    try:
        lines, code = render()
    except Exception as exc:  # noqa: BLE001 - fixed class name only, never the message
        print(f"pilot status: cannot start ({type(exc).__name__})", file=sys.stderr)
        return EXIT_UNREADABLE
    for line in lines:
        print(line)
    if code == EXIT_NO_LIVE:
        print("pilot status: no live account")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
