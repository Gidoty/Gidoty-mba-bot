"""The live step shared by every profit-protection rule (docs/safety/profit_every_round.md): given a stop level worked out this round, exit, move the stop or hold. Every stop move goes through
`lifecycle.move_stop` (the new stop is placed and read back before the old one is cancelled, favourable direction only, never on the wrong side of the price) and every exit through
`lifecycle.execute_exit`, the only live exit path."""
from __future__ import annotations

from datetime import timezone

from . import lifecycle, profit_protect
from .config import Settings
from .models import Trade


def _ms(dt) -> int | None:
    if dt is None:
        return None
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


async def running_step(ctx, trade: Trade, settings: Settings, *, level: float | None, label: str, price: float, atr: float | None, now_ms: int) -> str:
    """Returns 'exit', 'moved', 'kept', 'busy', 'skipped' (from move_stop) or 'hold:<why>'. Never raises beyond what execute_exit and move_stop do."""
    initial = trade.initial_stop_price
    r = abs(trade.entry_price - initial) if initial else None
    d = profit_protect.decide(
        side=trade.side, level=level, current_stop=trade.stop_loss_price, price=price, entry=trade.entry_price, atr=atr, r=r,
        last_move_ms=_ms(trade.stop_moved_at), now_ms=now_ms, min_interval_s=settings.profit_move_min_interval_seconds, min_improve_atr=settings.profit_move_min_improve_atr,
    )
    if d.action == "exit":
        await lifecycle.execute_exit(ctx, trade, reason=label, price_hint=level)
        return "exit"
    if d.action == "move":
        return await lifecycle.move_stop(ctx, trade, new_stop=level, current_price=price)
    return f"hold:{d.why}"
