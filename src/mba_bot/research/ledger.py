"""Trade ledger: what each real trade actually cost and returned, from the exchange's own fills and income rows.

Pure functions over a SANITISED bundle (see `ledger_export.py`, which the owner runs). Nothing here reads a credential,
a database or the network. The bundle holds prices, quantities, fees and timestamps, with account ids and order ids
replaced by salted hashes.

Matching rule. The bot holds one position per account at a time, so the fills of a trade are the fills for its symbol
between a little before it opened and a little after it closed. Order references are used only as a cross-check. A trade
is `reconciled` only if the matched fills net to flat and their size agrees with the recorded quantity; anything else
(a manual trade on the same exchange account, a partial record) is reported as `unreconciled` and is excluded from every
estimate. Excluded trades are counted, never hidden.

Classes
  paper     is_paper trades: no real fees or slippage, kept apart.
  incident  a live entry that was closed again by a protection emergency exit (the open-and-close loops). Real costs, no
            strategy decision: reported as the cost of the incident, not as strategy performance.
  normal    every other live trade.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict, dataclass, field

SCHEMA = "mba-ledger-bundle/1"
DAY_MS = 86_400_000
MATCH_PAD_BEFORE_MS = 60_000     # fills may be stamped a little before the recorded open
MATCH_PAD_AFTER_MS = 120_000     # and a little after the recorded close (a stop that fired just before the bot noticed)
QTY_TOLERANCE = 0.02             # matched entry size may differ from the recorded quantity by 2% (rounding, dust)
FLAT_TOLERANCE = 0.005           # entry and exit quantity must agree within 0.5%
PNL_TOLERANCE_ABS = 0.02         # USDT: computed gross versus the exchange's realizedPnl sum

# Closed by a protection emergency exit (see protection_guard.EMERGENCY_REASON_*; cross-checked by a test).
EMERGENCY_REASONS = frozenset(
    {"protection_failed_emergency_exit", "protection_transient_emergency_exit", "protection_unclassified_emergency_exit"}
)

CLASS_PAPER, CLASS_INCIDENT, CLASS_NORMAL = "paper", "incident", "normal"


class BundleError(ValueError):
    """The bundle is malformed or contains something it must not. The message is safe to show."""


# Allow-list of field names. Anything else (an extra column, a nested object, a stray identifier) is refused, so a bundle
# can only ever carry the fields the analysis needs.
TOP_KEYS = frozenset({"schema", "exported_at_ms", "window", "salt_id", "trades", "fills", "income"})
WINDOW_KEYS = frozenset({"start_ms", "end_ms", "days"})
TRADE_KEYS = frozenset({"trade_ref", "pair", "side", "is_paper", "status", "close_reason", "opened_ms", "closed_ms", "signal_price", "entry_price",
                        "exit_price", "stop_price", "quantity", "pnl", "entry_order_ref", "exit_order_ref", "stop_order_ref"})
FILL_KEYS = frozenset({"time_ms", "symbol", "side", "price", "qty", "quote_qty", "commission", "commission_asset", "maker", "realized_pnl", "order_ref"})
INCOME_KEYS = frozenset({"time_ms", "symbol", "kind", "amount", "asset"})


def _check_keys(obj, allowed, what: str) -> None:
    if not isinstance(obj, dict):
        raise BundleError(f"{what} must be an object")
    extra = set(obj) - allowed
    if extra:
        raise BundleError(f"{what} carries a field that is not in the bundle format ({len(extra)} unexpected)")


def _validate_shape(raw: dict) -> None:
    _check_keys(raw, TOP_KEYS, "the bundle")
    if "window" in raw and raw["window"] is not None:
        _check_keys(raw["window"], WINDOW_KEYS, "window")
    for name, allowed in (("trades", TRADE_KEYS), ("fills", FILL_KEYS), ("income", INCOME_KEYS)):
        rows = raw.get(name, [])
        if not isinstance(rows, list):
            raise BundleError(f"{name} must be a list")
        for r in rows:
            _check_keys(r, allowed, f"a {name[:-1] if name != 'income' else 'income'} row")


def ref_hash(salt: str, value) -> str | None:
    """Stable, non-reversible reference for an id inside one export."""
    if value is None or value == "":
        return None
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Fill:
    time_ms: int
    symbol: str
    side: str          # BUY | SELL
    price: float
    qty: float
    quote_qty: float
    commission: float
    commission_asset: str
    maker: bool
    realized_pnl: float
    order_ref: str | None


@dataclass(frozen=True)
class Income:
    time_ms: int
    symbol: str
    kind: str          # COMMISSION | FUNDING_FEE
    amount: float      # exchange sign: negative = paid
    asset: str


@dataclass(frozen=True)
class LedgerTrade:
    trade_ref: str
    pair: str          # BTC/USDT
    side: str          # long | short
    is_paper: bool
    status: str
    close_reason: str | None
    opened_ms: int | None
    closed_ms: int | None
    signal_price: float | None
    entry_price: float | None
    exit_price: float | None
    stop_price: float | None
    quantity: float | None
    pnl_recorded: float | None
    entry_order_ref: str | None = None
    exit_order_ref: str | None = None
    stop_order_ref: str | None = None

    @property
    def symbol(self) -> str:
        return self.pair.replace("/", "")


@dataclass
class Economics:
    trade_ref: str
    klass: str
    pair: str
    side: str
    close_reason: str | None
    opened_ms: int | None
    closed_ms: int | None
    reconciled: bool
    problems: list[str] = field(default_factory=list)
    quantity: float | None = None
    entry_avg: float | None = None
    exit_avg: float | None = None
    gross_pnl: float | None = None        # (exit - entry) * qty * side, from the fills
    fees_usdt: float | None = None        # commission paid in USDT
    fees_other_assets: bool = False       # some commission was paid in another asset (BNB): not converted
    funding_usdt: float | None = None     # received positive
    net_pnl: float | None = None
    planned_risk: float | None = None
    r_net: float | None = None
    entry_slip_bps: float | None = None   # adverse positive, against the signal price
    stop_slip_bps: float | None = None    # adverse positive, only for stop exits
    entry_fee_bps: float | None = None
    exit_fee_bps: float | None = None
    entry_taker: bool | None = None
    exit_taker: bool | None = None
    hold_minutes: float | None = None
    notional: float | None = None
    recorded_vs_fills_pnl_gap: float | None = None  # trades.pnl minus gross-from-fills, USDT


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_bundle(text: str) -> tuple[list[LedgerTrade], list[Fill], list[Income], dict]:
    """Parses and validates a bundle. Raises BundleError with a safe message."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise BundleError("the bundle is not valid JSON") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise BundleError(f"unsupported bundle schema (expected {SCHEMA})")
    _validate_shape(raw)
    trades, fills, income = [], [], []
    for r in raw.get("trades", []):
        try:
            trades.append(LedgerTrade(
                trade_ref=str(r["trade_ref"]), pair=str(r["pair"]), side=str(r["side"]), is_paper=bool(r["is_paper"]),
                status=str(r.get("status", "")), close_reason=r.get("close_reason"), opened_ms=r.get("opened_ms"),
                closed_ms=r.get("closed_ms"), signal_price=_f(r.get("signal_price")), entry_price=_f(r.get("entry_price")),
                exit_price=_f(r.get("exit_price")), stop_price=_f(r.get("stop_price")), quantity=_f(r.get("quantity")),
                pnl_recorded=_f(r.get("pnl")), entry_order_ref=r.get("entry_order_ref"), exit_order_ref=r.get("exit_order_ref"),
                stop_order_ref=r.get("stop_order_ref"),
            ))
        except (KeyError, TypeError) as exc:
            raise BundleError("a trade row is missing a required field") from exc
    for r in raw.get("fills", []):
        try:
            fills.append(Fill(
                time_ms=int(r["time_ms"]), symbol=str(r["symbol"]), side=str(r["side"]).upper(), price=float(r["price"]),
                qty=float(r["qty"]), quote_qty=float(r["quote_qty"]), commission=float(r["commission"]),
                commission_asset=str(r.get("commission_asset", "USDT")), maker=bool(r.get("maker", False)),
                realized_pnl=float(r.get("realized_pnl", 0.0)), order_ref=r.get("order_ref"),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise BundleError("a fill row is malformed") from exc
    for r in raw.get("income", []):
        try:
            income.append(Income(int(r["time_ms"]), str(r["symbol"]), str(r["kind"]), float(r["amount"]), str(r.get("asset", "USDT"))))
        except (KeyError, TypeError, ValueError) as exc:
            raise BundleError("an income row is malformed") from exc
    meta = {k: raw.get(k) for k in ("exported_at_ms", "window", "salt_id")}
    return trades, fills, income, meta


def classify(t: LedgerTrade) -> str:
    if t.is_paper:
        return CLASS_PAPER
    if t.close_reason in EMERGENCY_REASONS:
        return CLASS_INCIDENT
    return CLASS_NORMAL


def _avg(fills: list[Fill]) -> float | None:
    q = sum(f.qty for f in fills)
    return sum(f.quote_qty for f in fills) / q if q else None


def economics_for(t: LedgerTrade, fills: list[Fill], income: list[Income]) -> Economics:
    e = Economics(t.trade_ref, classify(t), t.pair, t.side, t.close_reason, t.opened_ms, t.closed_ms, reconciled=False)
    if t.is_paper:
        e.problems.append("paper trade: no exchange record")
        return e
    if t.opened_ms is None or t.closed_ms is None or not t.quantity or t.quantity <= 0:
        e.problems.append("the trade has no open or close time or quantity")
        return e
    lo, hi = t.opened_ms - MATCH_PAD_BEFORE_MS, t.closed_ms + MATCH_PAD_AFTER_MS
    mine = [f for f in fills if f.symbol == t.symbol and lo <= f.time_ms <= hi]
    entry_side, exit_side = ("BUY", "SELL") if t.side == "long" else ("SELL", "BUY")
    ent = [f for f in mine if f.side == entry_side]
    ext = [f for f in mine if f.side == exit_side]
    if not ent or not ext:
        e.problems.append("no matching entry and exit fills")
        return e
    q_in, q_out = sum(f.qty for f in ent), sum(f.qty for f in ext)
    if abs(q_in - q_out) > FLAT_TOLERANCE * max(q_in, q_out):
        e.problems.append("matched fills do not net to flat (another trade on the account, or an incomplete record)")
    if abs(q_in - t.quantity) > QTY_TOLERANCE * t.quantity:
        e.problems.append("matched size differs from the recorded quantity")
    sign = 1.0 if t.side == "long" else -1.0
    e.quantity, e.entry_avg, e.exit_avg = q_in, _avg(ent), _avg(ext)
    e.gross_pnl = (e.exit_avg - e.entry_avg) * min(q_in, q_out) * sign
    usdt = [f for f in mine if f.commission_asset == "USDT"]
    e.fees_other_assets = any(f.commission_asset != "USDT" for f in mine)
    e.fees_usdt = sum(f.commission for f in usdt)
    if e.fees_other_assets:
        e.problems.append("commission was paid in another asset (BNB), which is not converted, so the net result would be overstated")
    e.funding_usdt = sum(i.amount for i in income if i.kind == "FUNDING_FEE" and i.symbol == t.symbol and i.asset == "USDT"
                         and t.opened_ms < i.time_ms <= t.closed_ms)
    e.net_pnl = e.gross_pnl - e.fees_usdt + e.funding_usdt
    e.notional = e.entry_avg * q_in
    ref_price = t.signal_price or t.entry_price
    if ref_price and t.stop_price:
        e.planned_risk = t.quantity * abs(ref_price - t.stop_price)
        if e.planned_risk > 0:
            e.r_net = e.net_pnl / e.planned_risk
    if ref_price:
        e.entry_slip_bps = sign * (e.entry_avg - ref_price) / ref_price * 1e4
    if t.close_reason == "stop_loss" and t.stop_price:
        e.stop_slip_bps = sign * (t.stop_price - e.exit_avg) / t.stop_price * 1e4
    ent_notional, ext_notional = sum(f.quote_qty for f in ent), sum(f.quote_qty for f in ext)
    ent_fee = sum(f.commission for f in ent if f.commission_asset == "USDT")
    ext_fee = sum(f.commission for f in ext if f.commission_asset == "USDT")
    if not any(f.commission_asset != "USDT" for f in ent) and ent_notional:
        e.entry_fee_bps = ent_fee / ent_notional * 1e4
    if not any(f.commission_asset != "USDT" for f in ext) and ext_notional:
        e.exit_fee_bps = ext_fee / ext_notional * 1e4
    e.entry_taker = all(not f.maker for f in ent)
    e.exit_taker = all(not f.maker for f in ext)
    e.hold_minutes = (t.closed_ms - t.opened_ms) / 60_000
    realized = sum(f.realized_pnl for f in mine)
    if abs(realized - e.gross_pnl) > max(PNL_TOLERANCE_ABS, 0.001 * abs(e.gross_pnl)):
        e.problems.append("computed gross pnl disagrees with the exchange's realizedPnl")
    if t.pnl_recorded is not None:
        e.recorded_vs_fills_pnl_gap = t.pnl_recorded - e.gross_pnl
    # Any problem blocks: a trade whose commission was paid in another asset has an overstated net result, so it is
    # excluded from every estimate and counted instead (BNB conversion is not supported in this version).
    e.reconciled = not e.problems
    return e


def build_ledger(trades: list[LedgerTrade], fills: list[Fill], income: list[Income]) -> list[Economics]:
    return [economics_for(t, fills, income) for t in sorted(trades, key=lambda x: (x.opened_ms or 0, x.trade_ref))]


def usable(rows: list[Economics], klass: str | None = None) -> list[Economics]:
    """Reconciled, with a net result. The only rows any estimate may use."""
    return [r for r in rows if r.reconciled and r.net_pnl is not None and (klass is None or r.klass == klass)]


def counts(rows: list[Economics]) -> dict:
    out = {"total": len(rows)}
    for k in (CLASS_NORMAL, CLASS_INCIDENT, CLASS_PAPER):
        sub = [r for r in rows if r.klass == k]
        out[k] = {"all": len(sub), "reconciled": len([r for r in sub if r.reconciled]), "unreconciled": len([r for r in sub if not r.reconciled and k != CLASS_PAPER])}
    return out


def to_json(rows: list[Economics]) -> str:
    return json.dumps([asdict(r) for r in rows], indent=1)


def median_or_none(values: list[float]) -> float | None:
    return statistics.median(values) if values else None
