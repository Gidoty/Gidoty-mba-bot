"""Setup advisor (docs/safety/setup_advisor.md). Ships behind SETUP_ADVISOR (default off). It only INFORMS: it reads an account's settings (and optionally the values the customer is about to save) and returns a short
ordered list of notes. It never changes a setting, never blocks a save, never changes how the bot trades, and makes no exchange call: it uses only data the service already holds (the account row, the cached
exchange filters, the cached Binance leverage read).

Every number and every `not_possible` comes from the SAME functions the bot enforces with (risk_limits, leverage, risk sizing chain, daily_limit, small_capital, live_multi). Nothing here copies a formula.
`not_possible` is used only where enforcement itself refuses (the account API's 422/409 rules, and a pair the small-capital check says the bot cannot trade). Anything the bot judges at trade time with live prices
is a `warning` or `tip` that states its assumption. Pure: no I/O, no clock, no database (the router supplies the facts)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import SimpleNamespace

from . import daily_limit, leverage as leverage_module, live_multi, multi_trade, risk, risk_limits, small_capital
from .setup_advice_texts import render

NOT_POSSIBLE, WARNING, TIP = "not_possible", "warning", "tip"
_ORDER = {NOT_POSSIBLE: 0, WARNING: 1, TIP: 2}
TIMEFRAME_HOURS = {"1h": 1, "4h": 4, "1d": 24}
CROSSINGS_PER_CANDLE = 0.02  # EMA 20/50 on a random price series (tests/test_setup_advisor_timeframe.py recomputes it); a property of the indicator, not market data
ASSUMED_STOP_PCT = 1.0  # a typical stop distance for the sizing illustration; stated in every note that uses it
MAX_PAIRS_NAMED = 3
STRATEGY_PRIORITY = (("donchian_breakout_v1", "Donchian"), ("ema_cross_atr_v1", "EMA"), ("rsi_mr_v1", "RSI"))


@dataclass(frozen=True)
class Note:
    id: str
    severity: str
    text: str
    action: str | None = None
    tab: str = "risk"  # where the Bot Settings screen shows it: "trading" or "risk"

    def as_dict(self) -> dict:
        return {"id": self.id, "severity": self.severity, "text": self.text, "action": self.action, "tab": self.tab}


@dataclass(frozen=True)
class Proposed:
    """What the customer is about to save (None = not changed in this request), the same names the account API takes."""
    risk_pct: float | None = None
    allocated_capital: float | None = None
    daily_loss_limit_usd: float | None = None
    live_slots: int | None = None
    max_leverage: int | None = None
    pair: str | None = None
    market_type: str | None = None
    pair_selection_mode: str | None = None
    acknowledge_high_risk: bool = False
    acknowledge_very_high_risk: bool = False
    ema_timeframe: str | None = None  # a candle size to PREVIEW (1h, 4h or 1d); the advisor never saves it


@dataclass(frozen=True)
class Facts:
    """Everything the advisor reads that is not on the account row. All of it is already held by the service."""
    switch_multi: bool
    risk_pct_max: float
    max_notional_multiplier: float
    futures_max_leverage: float | None
    live_total_risk_cap_pct: float
    donchian_risk_pct: float
    rsi_risk_pct: float
    rsi_live: bool
    donchian_live: bool
    daily_limit_now: float | None  # the amount in force today (a pending raise shown only when its day has come)
    reference_value: float | None
    acks_on_file: dict = field(default_factory=dict)  # {doc_type: bool} for the current acknowledgment version
    pair_min_notional: dict = field(default_factory=dict)  # cached exchange filters: pair -> min notional (missing = unknown)
    binance_leverage: float | None = None  # the cached read of Binance's own number for the account's pair, or None
    binance_margin_mode: str | None = None
    binance_pair: str | None = None
    precheck_min_maint_rate: float = 0.005
    precheck_safety_fraction: float = 0.75
    precheck_cross_factor: float = 0.9
    precheck_max_leverage: float = 125.0


def _num(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _money(x: float) -> str:
    return f"{math.ceil(x * 100 - 1e-9) / 100:.2f}"


def _pct(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _pick(proposed_value, current):
    return current if proposed_value is None else proposed_value


def _size_model(*, capital: float, risk_pct: float, f: Facts, market_type: str, cap: int, binance_lev: float | None):
    """One trade at ASSUMED_STOP_PCT through the bot's own sizing chain (compute_position_size, then the exposure and margin ceilings). Returns the PositionSize, the leverage multiple used and the entry price."""
    entry = 100.0
    stop = entry * (1 - ASSUMED_STOP_PCT / 100.0)
    size = risk.compute_position_size(account_balance=capital, risk_pct=risk_pct, entry_price=entry, stop_price=stop, max_risk_pct=f.risk_pct_max)
    size = risk.apply_exposure_ceiling(size, entry_price=entry, effective_capital=capital, max_notional_multiplier=f.max_notional_multiplier)
    lev = small_capital.leverage_cap_for(market_type, cap)
    if market_type == "futures" and binance_lev:
        lev = leverage_module.effective(binance_lev, cap)
    size = risk.apply_margin_ceiling(size, entry_price=entry, effective_capital=capital, leverage=lev)
    return size, lev, entry, stop


def stop_pct_for(timeframe: str) -> float:
    """The stop distance the preview assumes: ASSUMED_STOP_PCT on 1h, scaled with the square root of the candle length (volatility over a candle grows about that way). An assumption, stated in the note."""
    return ASSUMED_STOP_PCT * math.sqrt(TIMEFRAME_HOURS[timeframe])


def _timeframe_notes(tf, *, market, pair, risk_pct, capital, size_ok, f, cap, binance_lev) -> list[Note]:
    if tf not in TIMEFRAME_HOURS:
        return [Note("tf_unknown", WARNING, render("tf_unknown"), None, "trading")]
    out: list[Note] = []
    stop_pct = stop_pct_for(tf)
    per_year = CROSSINGS_PER_CANDLE * 8760 / TIMEFRAME_HOURS[tf]
    out.append(Note("tf_signals", TIP, render("tf_signals", per_hundred=round(CROSSINGS_PER_CANDLE * 100, 1), per_year=float(f"{per_year:.2g}"), tf=tf), None, "trading"))
    if not (size_ok and capital is not None and capital > 0 and risk_pct is not None):
        return out
    entry = 100.0
    stop = entry * (1 - stop_pct / 100.0)
    try:
        size = risk.compute_position_size(account_balance=capital, risk_pct=risk_pct, entry_price=entry, stop_price=stop, max_risk_pct=f.risk_pct_max)
        size = risk.apply_exposure_ceiling(size, entry_price=entry, effective_capital=capital, max_notional_multiplier=f.max_notional_multiplier)
        lev = small_capital.leverage_cap_for(market, cap)
        if market == "futures" and binance_lev:
            lev = leverage_module.effective(binance_lev, cap)
        size = risk.apply_margin_ceiling(size, entry_price=entry, effective_capital=capital, leverage=lev)
    except risk.InvalidRiskParameters:
        return out
    if market == "futures":
        tier = risk.MaintenanceTier(tier=1, floor=0.0, cap=1e12, rate=f.precheck_min_maint_rate, cum=0.0)
        check_lev = float(binance_lev) if binance_lev else float(lev)  # the liquidation distance follows the leverage SET ON BINANCE for the pair when it is known, else the customer's limit
        lev_phrase = f"{check_lev:g}x, the leverage set on Binance for {pair}," if binance_lev else f"your leverage limit of {check_lev:g}x (Binance's own number for {pair} is not known here)"
        v = risk.assess_stop_viability(side="long", entry_price=entry, stop_price=stop, quantity=size.quantity, leverage=check_lev, effective_capital=capital, margin_mode="isolated", tiers=[tier],
                                       other_open_positions=0, min_maint_rate=f.precheck_min_maint_rate, safety_fraction=f.precheck_safety_fraction, cross_collateral_factor=f.precheck_cross_factor,
                                       max_leverage=f.precheck_max_leverage)
        max_pct = (v.max_allowed_stop_frac or 0.0) * 100.0
        key = "tf_stop_ok" if v.viable else "tf_stop_refused"
        out.append(Note("tf_stop", TIP if v.viable else WARNING, render(key, tf=tf, stop_pct=stop_pct, base_pct=ASSUMED_STOP_PCT, lev_phrase=lev_phrase, max_pct=max_pct), None if v.viable else render("tf_stop_refused_action"), "risk"))
        mn = f.pair_min_notional.get(pair)
        order = size.quantity * entry
        args = dict(risk_pct=risk_pct, stop_pct=stop_pct, capital=_money(capital), unit="USDT", order=_money(order), pair=pair, minimum="" if mn is None else _money(mn))
        if mn is None:
            out.append(Note("tf_order", TIP, render("tf_order_unknown", **args), None, "trading"))
        elif order < mn:
            out.append(Note("tf_order", WARNING, render("tf_order_too_small", **args), render("tf_order_too_small_action"), "trading"))
        else:
            out.append(Note("tf_order", TIP, render("tf_order_ok", **args), None, "trading"))
    else:
        out.append(Note("tf_stop", TIP, render("tf_stop_spot", tf=tf, stop_pct=stop_pct), None, "risk"))
    return out


def advise(account, f: Facts, proposed: Proposed | None = None) -> list[dict]:
    p = proposed or Proposed()
    notes: list[Note] = []

    market = _pick(p.market_type, account.market_type)
    pair = _pick(p.pair, account.pair)
    risk_pct = _num(_pick(p.risk_pct, account.risk_pct))
    capital = _num(_pick(p.allocated_capital, account.allocated_capital))
    limit = _num(_pick(p.daily_loss_limit_usd, f.daily_limit_now))
    slots_now = account.live_slots
    mode_now = account.pair_selection_mode
    cap = leverage_module.account_cap(_pick(p.max_leverage, account.max_leverage), f.futures_max_leverage)
    unit = "USDT"
    binance_lev = f.binance_leverage if (market == "futures" and f.binance_pair == pair and f.binance_leverage) else None

    # --- 9 and 1: the risk setting itself (the account API's own rule) ---------------------------------------------------------------------------------------------------------------------------------
    size_ok = False
    if risk_pct is not None:
        problem, _ = risk_limits.risk_setting_problem(
            risk_pct, f.risk_pct_max, on_file=dict(f.acks_on_file),
            ticked={risk_limits.ACK_HIGH: p.acknowledge_high_risk, risk_limits.ACK_VERY_HIGH: p.acknowledge_very_high_risk},
        )
        if problem is not None:
            notes.append(Note("risk_refused", NOT_POSSIBLE, problem, "Change the risk per trade or tick the confirmation, then save.", "risk"))
        else:
            size_ok = True
            band = risk_limits.band(risk_pct)
            key = {risk_limits.BAND_RECOMMENDED: "risk_band_recommended", risk_limits.BAND_HIGH: "risk_band_high", risk_limits.BAND_VERY_HIGH: "risk_band_very_high"}.get(band)
            if key:
                notes.append(Note("risk_band", TIP if key == "risk_band_recommended" else WARNING, render(key, risk_pct=risk_pct), None, "risk"))

    model = None
    if size_ok and capital is not None and capital > 0:
        try:
            model = _size_model(capital=capital, risk_pct=risk_pct, f=f, market_type=market, cap=cap, binance_lev=binance_lev)
        except risk.InvalidRiskParameters:
            model = None
    elif capital is None or capital <= 0:
        notes.append(Note("no_capital", WARNING, render("no_capital"), render("no_capital_action"), "trading"))

    risk_amount = capital * risk_pct / 100.0 if (model and capital) else None
    if model:
        size, lev, entry, stop = model
        loss = size.risk_amount
        if loss < risk_amount - 1e-9:
            notes.append(Note("size_cap_binds", WARNING, render(
                "size_cap_binds", risk_pct=risk_pct, risk_amount=_money(risk_amount), unit=unit, leverage=lev, cap_value=_money(size.quantity * entry),
                stop_pct=ASSUMED_STOP_PCT, loss=_money(loss), loss_pct=_pct(loss / capital * 100)), render("size_cap_action"), "risk"))
        else:
            notes.append(Note("size_cap_ok", TIP, render("size_cap_ok", risk_pct=risk_pct, risk_amount=_money(risk_amount), unit=unit, leverage=lev, stop_pct=ASSUMED_STOP_PCT), None, "risk"))

    # --- 2 and 3: the daily dollar limit ------------------------------------------------------------------------------------------------------------------------------------------------------------------
    if p.daily_loss_limit_usd is not None:
        problem = daily_limit.validate_amount(p.daily_loss_limit_usd, reference_value=f.reference_value)
        if problem is not None:
            notes.append(Note("daily_refused", NOT_POSSIBLE, problem, "Enter a smaller amount, then save.", "risk"))
            limit = None  # a refused amount is not a limit the bot will use
    if limit is None and p.daily_loss_limit_usd is None and f.daily_limit_now is None:
        notes.append(Note("daily_missing", WARNING, render("daily_missing"), render("daily_missing_action"), "risk"))
    if limit is not None and capital is not None and risk_pct is not None and size_ok:
        if daily_limit.one_trade_can_exceed(risk_pct, capital, limit):
            notes.append(Note("daily_below_one_trade", WARNING, render("daily_below_one_trade", risk_pct=risk_pct, capital=_money(capital), unit=unit, risk_amount=_money(capital * risk_pct / 100), limit=_money(limit)),
                              render("daily_below_one_trade_action"), "risk"))
        elif risk_amount:
            notes.append(Note("daily_covers_n_trades", TIP, render("daily_covers_n_trades", limit=_money(limit), unit=unit, n=round(limit / risk_amount, 1), risk_amount=_money(risk_amount)), None, "risk"))
    if limit is not None and f.reference_value:
        stop_level = daily_limit.total_stop_level_usd(f.reference_value)
        pct = daily_limit.pct_of(limit, f.reference_value)
        if stop_level is not None and pct is not None:
            notes.append(Note("daily_levels", TIP, render("daily_levels", limit=_money(limit), unit=unit, limit_pct=_pct(pct), value=_money(f.reference_value), stop_pct=risk_limits.TOTAL_STOP_PCT, stop_value=_money(stop_level)), None, "risk"))

    # --- 4 and 5: more than one open trade ---------------------------------------------------------------------------------------------------------------------------------------------------------------
    refusal = live_multi.slots_refusal(new_slots=p.live_slots, new_selection_mode=p.pair_selection_mode, current_slots=slots_now, current_selection_mode=mode_now, switch_on=f.switch_multi)
    if refusal is not None:
        notes.append(Note("slots_refused", NOT_POSSIBLE, refusal[1], "Change the number of open trades or the pair selection, then save.", "trading"))
    slots = live_multi.slot_cap(_pick(p.live_slots, slots_now), switch_on=f.switch_multi)
    if slots > 1 and refusal is None:
        pairs = live_multi.live_pairs(SimpleNamespace(pair=pair, pair_list=getattr(account, "pair_list", None)))
        if len(pairs) < slots:
            notes.append(Note("slots_pairs", WARNING, render("slots_pairs", slots=slots, pairs=len(pairs), pair_list=", ".join(pairs[:MAX_PAIRS_NAMED])), render("slots_pairs_action"), "trading"))
        if model and capital and risk_amount is not None:
            size, lev, entry, stop = model
            trade = SimpleNamespace(quantity=size.quantity, entry_price=entry, stop_loss_price=stop, side="long")
            margin = size.quantity * entry / lev
            first_empty = next((k for k in range(1, slots) if live_multi.free_capital(capital, [trade] * k, lev) <= 1e-9), None)
            if first_empty is not None:
                notes.append(Note("slots_capital", WARNING, render("slots_capital_none", k=first_empty, next_n=first_empty + 1), None, "trading"))
            else:
                free = live_multi.free_capital(capital, [trade], lev)
                notes.append(Note("slots_capital", TIP, render("slots_capital", risk_pct=risk_pct, margin=_money(margin), unit=unit, capital=_money(capital), k=1, free=_money(free)), None, "trading"))
            refused = None
            for n in range(2, slots + 1):
                reason = live_multi.check_additional_entry([trade] * (n - 1), new_risk=size.risk_amount, capital=capital, cap_pct=f.live_total_risk_cap_pct, daily_limit_usd=limit)
                if reason is not None:
                    refused = (n, reason)
                    break
            if refused is not None:
                n, reason = refused
                total = size.risk_amount * n
                if reason == live_multi.R_TOTAL_RISK:
                    rule = render("open_risk_rule_percent", cap_pct=f.live_total_risk_cap_pct, cap_value=_money(capital * f.live_total_risk_cap_pct / 100), unit=unit)
                    limit_text = f"{_money(capital * f.live_total_risk_cap_pct / 100)} {unit}"
                else:
                    rule = render("open_risk_rule_daily", limit=_money(limit), unit=unit)
                    limit_text = f"{_money(limit)} {unit}"
                notes.append(Note("open_risk", WARNING, render("open_risk_refused", slots=slots, risk_pct=risk_pct, n=n, rule=rule, total=_money(total), unit=unit, limit_text=limit_text), "Lower the risk per trade or the number of open trades.", "risk"))
            else:
                notes.append(Note("open_risk", TIP, render("open_risk_ok", slots=slots, risk_pct=risk_pct, total=_money(size.risk_amount * slots), unit=unit), None, "risk"))

    # --- 6: can the chosen pair be traded with this capital ---------------------------------------------------------------------------------------------------------------------------------------------
    if market == "futures" and pair:
        mn = f.pair_min_notional.get(pair)
        if mn is None:
            notes.append(Note("pair_unknown", TIP, render("pair_unknown", pair=pair), None, "trading"))
        elif capital is not None and capital > 0:
            lev_cap = small_capital.leverage_cap_for(market, cap)
            v = small_capital.assess(SimpleNamespace(min_notional=mn, market_min_qty=0, step=0), None, capital=capital, leverage_cap=lev_cap)
            args = dict(pair=pair, need=_money(v.min_capital) if v.min_capital is not None else "more", unit=unit, leverage=lev_cap, capital=_money(capital))
            if v.viable is False:
                notes.append(Note("pair_viability", NOT_POSSIBLE, render("pair_not_viable", **args), render("pair_not_viable_action"), "trading"))
            elif v.viable:
                notes.append(Note("pair_viability", TIP, render("pair_viable", **args), None, "trading"))

    # --- 7: strategies ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    opted = multi_trade.account_strategies(account)
    running = set()
    off = []
    for sid, name in (("donchian_breakout_v1", "Donchian"), ("rsi_mr_v1", "RSI")):
        if sid in opted:
            (running.add(sid) if (f.donchian_live if sid == "donchian_breakout_v1" else f.rsi_live) else off.append(name))
    if running or off:
        active = [(sid, name) for sid, name in STRATEGY_PRIORITY if sid == "ema_cross_atr_v1" or sid in running]
        if risk_pct is not None and size_ok:
            cap_of = {"donchian_breakout_v1": f.donchian_risk_pct, "rsi_mr_v1": f.rsi_risk_pct}
            risks = ", ".join(f"{name} {_pct(min(risk_pct, cap_of[sid]) if cap_of.get(sid) else risk_pct)}%" for sid, name in active)
            notes.append(Note("strategies", TIP, render("strategies", order=", ".join(n for _, n in active), risks=risks), None, "trading"))
        if slots == 1 and len(active) > 1:
            notes.append(Note("strategies_one_slot", TIP, render("strategies_one_slot"), None, "trading"))
        if off:
            many = len(off) > 1
            notes.append(Note("strategies_not_running", TIP, render("strategies_not_running", names=" and ".join(off), is_are="are" if many else "is"), None, "trading"))

    # --- 11: a candle size to preview (never saved) ---------------------------------------------------------------------------------------------------------------------------------------------------
    if p.ema_timeframe is not None:
        notes += _timeframe_notes(p.ema_timeframe, market=market, pair=pair, risk_pct=risk_pct, capital=capital, size_ok=size_ok, f=f, cap=cap, binance_lev=binance_lev)

    # --- 8: leverage -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    if market == "futures":
        if f.binance_pair == pair and f.binance_margin_mode is not None and f.binance_margin_mode != "isolated":
            notes.append(Note("margin_not_isolated", WARNING, render("margin_not_isolated", pair=pair), None, "trading"))
        if binance_lev:
            notes.append(Note("leverage", TIP, render("leverage_binance", cap=float(cap), pair=pair, binance=binance_lev, effective=leverage_module.effective(binance_lev, cap)), None, "risk"))
        else:
            notes.append(Note("leverage", TIP, render("leverage_cap", cap=float(cap)), None, "risk"))

    # --- 10: money moved during the day -----------------------------------------------------------------------------------------------------------------------------------------------------------------
    notes.append(Note("money_moves", TIP, render("money_moves"), None, "risk"))

    notes.sort(key=lambda n: _ORDER[n.severity])  # stable: the rule order above is kept inside a severity
    return [n.as_dict() for n in notes]
