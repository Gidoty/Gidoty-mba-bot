"""A client that routes every call to one FakeBinance per symbol, so a live account can hold trades on several pairs in a test. Calls that name no symbol (balance, positions of all symbols) go to the first fake.
The fakes keep their own balances: this proves control flow and the order of calls, not Binance's shared-margin behaviour."""
from __future__ import annotations

from helpers_live import futures_client


class RoutedClient:
    def __init__(self, fakes: dict):
        self._fakes = fakes  # exchange symbol -> FakeBinance
        self._primary = next(iter(fakes.values()))

    @property
    def markets(self):
        merged = {}
        for f in self._fakes.values():
            merged.update(f.markets or {})
        return merged

    @markets.setter
    def markets(self, value):
        pass

    def _target(self, args, kwargs):
        for v in list(args) + list(kwargs.values()):
            if isinstance(v, str) and v in self._fakes:
                return self._fakes[v]
            if isinstance(v, (list, tuple)):
                for item in v:
                    if isinstance(item, str) and item in self._fakes:
                        return self._fakes[item]
        return self._primary

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        primary_attr = getattr(self._primary, name)
        if not callable(primary_attr):
            return primary_attr

        async def call(*args, **kwargs):
            if name == "load_markets":
                for f in self._fakes.values():
                    await f.load_markets()
                return self.markets
            return await getattr(self._target(args, kwargs), name)(*args, **kwargs)

        return call

    async def close(self):
        for f in self._fakes.values():
            await f.close()

    @property
    def calls(self):
        out = []
        for f in self._fakes.values():
            out.extend(f.calls)
        return out


def routed_futures(pairs: dict, **kw) -> RoutedClient:
    """pairs: {"BTC/USDT": (ohlcv, price), ...}"""
    fakes = {}
    for pair, (ohlcv, price) in pairs.items():
        fakes[f"{pair}:USDT"] = futures_client(pair=pair, ohlcv=ohlcv, price=price, leverage_by_symbol={pair: 1.0}, **kw)
    return RoutedClient(fakes)
