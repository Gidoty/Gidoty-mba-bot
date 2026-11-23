"""Auto Select tie measurement (docs/safety/auto_select_strategies.md). A MEASUREMENT, not evidence that any ranking rule works.

When two or more pairs signal for the same strategy in the same round, the code enters the first one in scan order (BTC, ETH, BNB, XRP) and records the others as `not_best_ranked`. This module reads the opportunity ledger
(read-only) and reports how common such ties are, what each pair's signal strength was, and, if asked, what the price did on each pair over the next 24 candles in the signal's direction. It chooses nothing and the
trading code never imports it.

Strength (never used to choose): Donchian = how far the close is beyond the channel, in ATR units; RSI = how far the RSI is beyond its entry line, in RSI points (`opportunity.strength`).
A "round" here is one (account, strategy, signal candle): the ledger holds one row per pair per candle however many scheduler rounds saw it.
Candles (only with --candles): PUBLIC Binance candles, no key, one read per pair; fewer than 24 closed candles after the signal is said plainly and the figures are marked partial."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import exchange, opportunity, rsi_mr
from .models import DecisionLog

TIE_STRATEGIES = (opportunity.DONCHIAN_ID, opportunity.RSI_ID)
FORWARD_CANDLES = 24
MAX_TIE_ROUNDS_SHOWN = 20  # the newest ones; the count above covers all of them
NOTE = "This is a measurement of one fixed rule (the first pair in scan order is entered). It is not evidence that any ranking rule works; any rule must be judged on new forward data, because the last sealed window was already looked at once."


def _detail(raw) -> dict:
    try:
        d = json.loads(raw) if raw else {}
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def collect(session: Session, *, since: datetime, account_prefix: str | None = None) -> list[dict]:
    q = select(DecisionLog).where(DecisionLog.stage == opportunity.STAGE, DecisionLog.ts >= since, DecisionLog.strategy_id.in_(TIE_STRATEGIES),
                                  DecisionLog.outcome.in_((opportunity.OUT_SIGNAL, opportunity.OUT_NOT_TAKEN))).order_by(DecisionLog.ts)
    out = []
    for r in session.scalars(q).all():
        if account_prefix and not r.account_id.startswith(account_prefix):
            continue
        if r.outcome == opportunity.OUT_NOT_TAKEN and r.reason_code != opportunity.R_NOT_BEST:
            continue
        d = _detail(r.detail_json)
        out.append({"account_id": r.account_id, "strategy_id": r.strategy_id, "pair": r.pair, "candle_ts": r.candle_ts, "outcome": r.outcome, "side": r.signal, "price": r.price,
                    "market_type": r.market_type, "tf": d.get("tf") or "1h", "strength": d.get("strength"), "picked": d.get("picked"), "picked_strength": d.get("picked_strength"), "ts": r.ts})
    return out


def analyse(rows: list[dict]) -> dict:
    """{"rounds": n rounds with any signal, "ties": [ {account_id, strategy_id, candle_ts, picked, members: [ {pair, side, price, strength, tf, market_type, picked: bool} ] } ] }. Pure."""
    groups: dict[tuple, list[dict]] = {}
    picked_by: dict[tuple, str] = {}
    for r in rows:
        key = (r["account_id"], r["strategy_id"], r["candle_ts"])
        if r["outcome"] == opportunity.OUT_SIGNAL:
            groups.setdefault(key, []).append(r)
        elif r["picked"]:
            picked_by[key] = r["picked"]
    ties = []
    for key, members in groups.items():
        by_pair = {}
        for m in members:
            by_pair.setdefault(m["pair"], m)  # one row per pair per candle
        if len(by_pair) < 2 or key[2] is None:
            continue
        picked = picked_by.get(key)
        ties.append({"account_id": key[0], "strategy_id": key[1], "candle_ts": key[2], "picked": picked,
                     "members": [dict(m, picked=(m["pair"] == picked)) for m in sorted(by_pair.values(), key=lambda m: m["pair"])]})
    ties.sort(key=lambda t: t["candle_ts"], reverse=True)
    return {"rounds": len(groups), "ties": ties}


def forward_moves(side: str | None, ref: float | None, candles: list, *, tf_ms: int, now_ms: int, limit: int = FORWARD_CANDLES) -> dict:
    """What the price did over the next `limit` CLOSED candles, in the signal's direction, as fractions of the reference price (the signal candle's close).
    best = the best excursion, worst = the worst excursion, at_end = the move at the last candle read. `n` is how many closed candles existed. Pure."""
    closed = [c for c in candles if c[0] + tf_ms <= now_ms][:limit]
    if not closed or not ref or side not in ("long", "short"):
        return {"n": len(closed), "best": None, "worst": None, "at_end": None}
    highs, lows, last = max(c[2] for c in closed), min(c[3] for c in closed), closed[-1][4]
    if side == "long":
        return {"n": len(closed), "best": highs / ref - 1, "worst": lows / ref - 1, "at_end": last / ref - 1}
    return {"n": len(closed), "best": 1 - lows / ref, "worst": 1 - highs / ref, "at_end": 1 - last / ref}


def public_candles(market_type: str, pair: str, tf: str, since_ms: int, limit: int) -> list:
    """PUBLIC candles, no key: a ccxt client built with no credentials, one fetch_ohlcv, then closed. Raises on failure (the caller says so)."""
    cls = exchange._EXCHANGE_CLASSES[("binance", market_type)]

    async def go():
        client = cls({"enableRateLimit": True})  # no apiKey, no secret
        try:
            return await client.fetch_ohlcv(exchange.exchange_symbol(pair, market_type), timeframe=tf, since=since_ms, limit=limit + 1)
        finally:
            await client.close()

    return asyncio.run(go())


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def _num(x) -> str:
    return "n/a" if x is None else f"{x:g}"


def render(analysis: dict, *, fetch=None, now: datetime | None = None, hours: int = 24) -> list[str]:
    """The 'ties' section. `fetch(market_type, pair, tf, since_ms, limit) -> rows` is given only with --candles (tests pass a fake)."""
    now = now or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    ties, rounds = analysis["ties"], analysis["rounds"]
    pct = f" ({len(ties) / rounds * 100:.0f}%)" if rounds else ""
    out = [f"ties: {len(ties)} of {rounds} rounds with a Donchian or RSI signal had two or more pairs signalling the same strategy{pct} (last {hours} hours)", NOTE]
    if not ties:
        return out
    if fetch is None:
        out.append("price moves not read: add --candles to read public Binance candles (no key is used).")
    for t in ties[:MAX_TIE_ROUNDS_SHOWN]:
        when = datetime.fromtimestamp(t["candle_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        out.append("")
        out.append(f"account {t['account_id'][:8]}, {opportunity.LABEL.get(t['strategy_id'], t['strategy_id'])}, signal candle {when} UTC; picked {t['picked'] or 'unknown'}, skipped "
                   + (", ".join(m["pair"] for m in t["members"] if not m["picked"]) or "none"))
        for m in t["members"]:
            line = f"  {m['pair']}{' (picked)' if m['picked'] else ' (skipped)'}: {m['side'] or '?'}, strength {_num(m['strength'])}"
            if fetch is not None:
                tf_ms = rsi_mr.timeframe_ms(rsi_mr.normalize_timeframe(m["tf"])[0])
                try:
                    rows = fetch(m["market_type"], m["pair"], m["tf"], t["candle_ts"] + tf_ms, FORWARD_CANDLES)
                    mv = forward_moves(m["side"], m["price"], rows, tf_ms=tf_ms, now_ms=now_ms)
                except Exception as exc:  # noqa: BLE001
                    line += f"; candles could not be read ({type(exc).__name__})"
                else:
                    if mv["n"] == 0:
                        line += "; no candle has closed after the signal yet"
                    elif mv["n"] < FORWARD_CANDLES:
                        line += f"; ONLY {mv['n']} OF {FORWARD_CANDLES} candles exist yet, PARTIAL: best {_pct(mv['best'])}, worst {_pct(mv['worst'])}, move at candle {mv['n']} {_pct(mv['at_end'])}"
                    else:
                        line += f"; next {FORWARD_CANDLES} candles: best {_pct(mv['best'])}, worst {_pct(mv['worst'])}, move at {FORWARD_CANDLES} {_pct(mv['at_end'])}"
            out.append(line)
    if len(ties) > MAX_TIE_ROUNDS_SHOWN:
        out.append(f"... {len(ties) - MAX_TIE_ROUNDS_SHOWN} older tie rounds not listed (counted above)")
    return out
