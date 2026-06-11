"""Betfair Stream API feed: market changes for tracked (market, selection) pairs.

betfairlightweight's listener runs in its own thread and pushes MarketBook
snapshots onto a queue; we bridge that into asyncio. Requires a live app key
(ours is confirmed live).
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Awaitable, Callable

from betfairlightweight import APIClient, StreamListener
from betfairlightweight import filters

from ..models import Platform, Quote, Tick
from .normalize import quote_from_betfair

logger = logging.getLogger(__name__)

TickHandler = Callable[[Tick], Awaitable[None]]


class BetfairFeed:
    # Betfair rejects subscriptions above 200 markets (SUBSCRIPTION_LIMIT_EXCEEDED)
    # and allows up to 10 concurrent stream connections, so shard.
    MARKETS_PER_STREAM = 180

    def __init__(
        self,
        client: APIClient,
        selections: dict[tuple[str, str], None] | set[tuple[str, str]],
        on_tick: TickHandler,
        backoff: list[float] | None = None,
    ):
        # tracked (market_id, selection_id) pairs
        self.client = client
        self.selections = set(selections)
        self.market_ids = sorted({m for m, _ in self.selections})
        self.on_tick = on_tick
        self.backoff = backoff or [1, 2, 5, 10, 30]
        self._queue: queue.Queue = queue.Queue()
        self._depth: dict[tuple[str, str], dict] = {}

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        n = self.MARKETS_PER_STREAM
        shards = [self.market_ids[i:i + n] for i in range(0, len(self.market_ids), n)]
        if len(shards) > 9:
            raise RuntimeError(f"{len(self.market_ids)} markets needs {len(shards)} "
                               "stream connections; Betfair allows ~10")
        for shard in shards:
            threading.Thread(target=self._stream_thread, args=(shard,), daemon=True).start()
        while True:
            books = await loop.run_in_executor(None, self._queue.get)
            if books is None:
                continue
            ts = datetime.now(timezone.utc)
            for book in books:
                await self._emit(book, ts)

    def _stream_thread(self, market_ids: list[str]) -> None:
        attempt = 0
        while True:
            try:
                listener = StreamListener(output_queue=self._queue)
                stream = self.client.streaming.create_stream(listener=listener)
                stream.subscribe_to_markets(
                    market_filter=filters.streaming_market_filter(
                        market_ids=market_ids
                    ),
                    market_data_filter=filters.streaming_market_data_filter(
                        fields=["EX_BEST_OFFERS", "EX_MARKET_DEF"],
                        ladder_levels=5,
                    ),
                )
                logger.info("Betfair stream shard subscribed to %d markets", len(market_ids))
                attempt = 0
                stream.start()  # blocks until error/disconnect
            except Exception as e:
                import time
                delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                logger.warning("Betfair stream error (%s); reconnecting in %ss", e, delay)
                time.sleep(delay)
                attempt += 1

    async def _emit(self, book, ts: datetime) -> None:
        market_id = book.market_id
        for runner in book.runners or []:
            sel = str(runner.selection_id)
            if (market_id, sel) not in self.selections:
                continue
            ex = runner.ex
            backs = [(p.price, p.size) for p in (ex.available_to_back or [])] if ex else []
            lays = [(p.price, p.size) for p in (ex.available_to_lay or [])] if ex else []
            self._depth[(market_id, sel)] = {"back": backs[:5], "lay": lays[:5]}
            await self.on_tick(Tick(
                ts=ts,
                platform=Platform.BETFAIR,
                market_id=market_id,
                outcome_id=sel,
                quote=quote_from_betfair(
                    backs[0] if backs else None,
                    lays[0] if lays else None,
                ),
                source_mode="stream",
            ))

    def depth(self, market_id: str, selection_id: str) -> dict:
        return self._depth.get((market_id, selection_id), {})
