"""The pairs a customer can choose for a market, with per-customer availability.

Spot and the flag-off Futures answer are always today's four pairs, all
available, with no exchange lookup. With the Futures expansion on, Futures adds
the extra manual-only pairs and each pair is marked unavailable (with a
customer-safe reason) when it is not listed on Binance Futures or the
customer's allocated capital cannot reach its minimum order size - see
pair_catalog for exactly what that does and does not claim."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ... import pair_catalog
from ...config import SUPPORTED_MARKET_TYPES, Settings, manual_pairs
from ...models import Customer
from ..deps import get_current_customer, get_pair_facts, get_settings
from ..schemas import PairOut, PairsOut

router = APIRouter(prefix="/api/pairs", tags=["pairs"])


@router.get("", response_model=PairsOut)
async def list_pairs(
    market_type: str = Query(...),
    customer: Customer = Depends(get_current_customer),
    settings: Settings = Depends(get_settings),
    facts: dict | None = Depends(get_pair_facts),
) -> PairsOut:
    if market_type not in SUPPORTED_MARKET_TYPES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"market_type must be one of {SUPPORTED_MARKET_TYPES}")
    pairs = manual_pairs(market_type, futures_expansion=settings.futures_pair_expansion)
    check = market_type == "futures" and settings.futures_pair_expansion and facts is not None
    account = customer.account
    capital = account.allocated_capital if account is not None else None
    out = []
    for pair in pairs:
        if check:
            st = pair_catalog.assess_pair(
                pair, facts.get(pair), allocated_capital=capital, max_notional_multiplier=settings.max_notional_multiplier
            )
            out.append(PairOut(pair=pair, available=st.available, reason=st.reason))
        else:
            out.append(PairOut(pair=pair, available=True))
    return PairsOut(market_type=market_type, pairs=out)
