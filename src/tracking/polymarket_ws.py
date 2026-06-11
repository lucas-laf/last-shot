"""Polymarket market-channel websocket: maintains YES-token books, emits Ticks.

Public endpoint, no auth. `book` messages are full snapshots; `price_change`
messages patch individual levels.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

import websockets

from ..models import Platform, Quote, Tick
from .normalize import quote_from_polymarket_book

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

TickHandler = Callable[[Tick], Awaitable[None]]


class PolymarketFeed:
    # The initial book snapshot for many tokens arrives as one huge frame, so
    # raise the client frame limit and shard subscriptions across connections.
    TOKENS_PER_CONN = 250
    MAX_FRAME_BYTES = 32 * 1024 * 1024

    def __init__(self, token_to_market: dict[str, str], on_tick: TickHandler,
                 backoff: list[float] | None = None):
        # token id -> our market_id (conditionId), for tick labeling
        self.token_to_market = token_to_market
        self.on_tick = on_tick
        self.backoff = backoff or [1, 2, 5, 10, 30]
        self._books: dict[str, tuple[dict[float, float], dict[float, float]]] = {}

    async def run(self) -> None:
        tokens = list(self.token_to_market)
        n = self.TOKENS_PER_CONN
        shards = [tokens[i:i + n] for i in range(0, len(tokens), n)]
        await asyncio.gather(*(self._run_shard(s) for s in shards))

    async def _run_shard(self, token_ids: list[str]) -> None:
        attempt = 0
        while True:
            try:
                await self._run_once(token_ids)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                logger.warning("Polymarket WS error (%s); reconnecting in %ss", e, delay)
                await asyncio.sleep(delay)
                attempt += 1

    async def _run_once(self, token_ids: list[str]) -> None:
        async with websockets.connect(
            WS_URL, ping_interval=10, max_size=self.MAX_FRAME_BYTES
        ) as ws:
            await ws.send(json.dumps({"type": "market", "assets_ids": token_ids}))
            logger.info("Polymarket WS shard subscribed to %d tokens", len(token_ids))
            async for raw in ws:
                for msg in self._as_list(json.loads(raw)):
                    await self._handle(msg)

    @staticmethod
    def _as_list(payload) -> list[dict]:
        return payload if isinstance(payload, list) else [payload]

    async def _handle(self, msg: dict) -> None:
        ev = msg.get("event_type")
        token = msg.get("asset_id", "")
        if token not in self.token_to_market:
            return
        if ev == "book":
            bids = {float(l["price"]): float(l["size"]) for l in msg.get("bids", [])}
            asks = {float(l["price"]): float(l["size"]) for l in msg.get("asks", [])}
            self._books[token] = (bids, asks)
        elif ev == "price_change":
            if token not in self._books:
                return
            bids, asks = self._books[token]
            for ch in msg.get("changes", []):
                price, size = float(ch["price"]), float(ch["size"])
                side = bids if ch.get("side") == "BUY" else asks
                if size == 0:
                    side.pop(price, None)
                else:
                    side[price] = size
        else:
            return
        bids, asks = self._books[token]
        await self.on_tick(Tick(
            ts=datetime.now(timezone.utc),
            platform=Platform.POLYMARKET,
            market_id=self.token_to_market[token],
            outcome_id=token,
            quote=quote_from_polymarket_book(bids, asks),
            source_mode="stream",
        ))

    def depth(self, token: str, n: int = 5) -> dict:
        """Top-N levels for paper-trade snapshots."""
        if token not in self._books:
            return {}
        bids, asks = self._books[token]
        return {
            "bids": sorted(bids.items(), reverse=True)[:n],
            "asks": sorted(asks.items())[:n],
        }
