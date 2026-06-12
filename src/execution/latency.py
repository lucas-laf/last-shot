"""Latency instrumentation: per-hop timing and periodic venue RTT probes.

Every number this module records answers one question: how much time passes
between seeing a price and an order arriving at the venue? The shadow
capture-rate analysis consumes the RTT estimates; the hop timings show where
our own pipeline spends its budget.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import httpx

from ..storage import Store

logger = logging.getLogger(__name__)

CLOB_TIME_URL = "https://clob.polymarket.com/time"
BETFAIR_KEEPALIVE_URL = "https://identitysso.betfair.com/api/keepAlive"


class Hops:
    """Microsecond stopwatch for one decision path. Usage:
    h = Hops(); ...; h.mark("decision"); ...; h.mark("order_ready")"""

    def __init__(self):
        self.t0 = time.perf_counter_ns()
        self.marks: list[tuple[str, int]] = []

    def mark(self, name: str) -> None:
        self.marks.append((name, time.perf_counter_ns()))

    def us(self, name: str) -> float | None:
        for n, t in self.marks:
            if n == name:
                return (t - self.t0) / 1_000
        return None

    def as_dict(self) -> dict:
        return {n: (t - self.t0) / 1_000 for n, t in self.marks}


class RttMonitor:
    """Rolling RTT estimates for both venues, refreshed in the background.

    Warm-connection RTTs (the realistic case for an executor holding a
    connection pool): one httpx client per venue is reused across probes,
    so only the first probe pays TLS setup. `betfair_rtt_ms`/`polymarket_rtt_ms`
    return the rolling median of the last 10 probes, with conservative
    defaults until the first probe lands.
    """

    def __init__(self, store: Store, session_token: str = "", app_key: str = "",
                 interval_s: float = 60.0):
        self.store = store
        self.interval = interval_s
        self._bf_headers = {"X-Authentication": session_token, "X-Application": app_key}
        self._bf: deque[float] = deque(maxlen=10)
        self._pm: deque[float] = deque(maxlen=10)
        self._client = httpx.AsyncClient(timeout=10)

    @staticmethod
    def _median(d: deque, default: float) -> float:
        if not d:
            return default
        s = sorted(d)
        return s[len(s) // 2]

    def betfair_rtt_ms(self) -> float:
        return self._median(self._bf, 50.0)

    def polymarket_rtt_ms(self) -> float:
        return self._median(self._pm, 200.0)

    async def _probe(self, url: str, headers: dict | None = None) -> float | None:
        t0 = time.perf_counter_ns()
        try:
            await self._client.get(url, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("RTT probe %s failed: %s", url, e)
            return None
        return (time.perf_counter_ns() - t0) / 1e6

    async def run(self) -> None:
        while True:
            bf = await self._probe(BETFAIR_KEEPALIVE_URL, self._bf_headers)
            pm = await self._probe(CLOB_TIME_URL)
            if bf is not None:
                self._bf.append(bf)
            if pm is not None:
                self._pm.append(pm)
            self.store.save_exec_event("rtt", {
                "betfair_ms": bf, "polymarket_ms": pm,
                "betfair_median_ms": self.betfair_rtt_ms(),
                "polymarket_median_ms": self.polymarket_rtt_ms(),
            })
            await asyncio.sleep(self.interval)
