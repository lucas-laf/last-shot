"""Polymarket LP (liquidity-reward) market-making executor — live, no hedge.

Posts two-sided resting GTC orders (buy-YES near mid, buy-NO near 1-mid) on slow,
mean-reverting reward markets to earn Polymarket's daily liquidity subsidy, managing
inventory by skewing quotes. Mirrors `MakerExecutor`'s loop skeleton but strips ALL
Betfair/hedge/unwind logic. See docs/LP_PILOT_SPEC.md and the approved plan.

Execution tiers (via the shared PolymarketExecutor): no creds -> SHADOW (nothing
built); creds + armed=False -> BUILT_NOT_SENT (built, not posted); armed=True -> live.

Risk rails (load-bearing, unattended): hard budget cap (reserved + cost-basis),
per-market book-share + inventory caps, per-market jump-halt, aggregate kill-switch,
event blackout, startup orphan cleanup, cancel_all on every exit. A kill cancels
quotes but does NOT auto-sell inventory (V2 sell-side unproven) — it alerts for manual
flatten; inventory is bounded by the caps so max loss ~= budget.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..storage import Store
from .polymarket_executor import PolymarketExecutor, _num
from .lp_quoter import _get, _snap, CLOB

logger = logging.getLogger(__name__)


@dataclass
class MarketState:
    condition_id: str
    question: str
    yes_token: str
    no_token: str
    tick: float
    max_spread: float          # fraction of price (rewards.max_spread / 100)
    min_size: float            # qualifying min order size = our per-side share count
    pool_day: float            # reward pool $/day
    qual: float                # competing balanced qualifying shares (reward-share proxy)
    end_ts: float | None = None  # resolution time (epoch) for blackout, if known
    mid: float = 0.0
    prev_mid: float | None = None
    mid_ts: float = 0.0        # perf_counter of last good book poll
    book_qual_bid: float = 0.0
    book_qual_ask: float = 0.0
    q_yes: float = 0.0         # YES tokens held (from buy-YES fills)
    q_no: float = 0.0          # NO tokens held (from buy-NO fills) = short YES
    cash: float = 0.0          # signed USDC from fills (buys spend -> goes negative)
    reward_model: float = 0.0  # modelled size-share accrual (proxy)
    reward_onchain: float = 0.0  # actual earnings from rewards endpoint
    halted: bool = False       # jump-halt latch
    blackout: bool = False     # near-resolution / not-accepting latch

    @property
    def inv(self) -> float:
        """Net YES-equivalent inventory (for skew): buy-YES long, buy-NO short."""
        return self.q_yes - self.q_no

    def mark_value(self) -> float:
        return self.q_yes * self.mid + self.q_no * (1.0 - self.mid)

    def trading_pnl(self) -> float:
        return self.mark_value() + self.cash

    def cost_basis(self) -> float:
        return max(0.0, -self.cash)


@dataclass
class LpQuote:
    market: MarketState
    side: str                  # "yes" (buy YES) | "no" (buy NO)
    exec_token: str
    exec_price: float
    size: float
    order_id: str
    placed_ts: float
    reserved_usd: float
    status: str = "resting"    # resting | filling
    reprice_count: int = 0


class LpExecutor:
    def __init__(self, *, markets: list[MarketState], pm_exec: PolymarketExecutor,
                 store: Store, armed: bool = False,
                 refresh_s: float = 20.0, poll_s: float = 5.0, book_poll_s: float = 10.0,
                 reward_poll_s: float = 900.0, metrics_s: float = 60.0,
                 reprice_eps: float = 0.005, half_spread_ticks: int = 1,
                 max_open_quotes: int = 20, budget_usd: float = 500.0,
                 size_mult: float = 1.0, inv_cap_mult: float = 3.0,
                 inv_book_frac: float = 0.5, jump_halt_pct: float = 0.15,
                 kill_drawdown_frac: float = 0.15, blackout_hours: float = 48.0,
                 min_pm_notional: float = 1.0, maker_address: str = "",
                 signature_type: int = 3):
        self.markets = {m.condition_id: m for m in markets}
        self.pm_exec = pm_exec
        self.store = store
        self.armed = armed
        self.refresh_s = refresh_s
        self.poll_s = poll_s
        self.book_poll_s = book_poll_s
        self.reward_poll_s = reward_poll_s
        self.metrics_s = metrics_s
        self.reprice_eps = reprice_eps
        self.half_spread_ticks = half_spread_ticks
        self.max_open_quotes = max_open_quotes
        self.budget_usd = budget_usd
        self.size_mult = size_mult
        self.inv_cap_mult = inv_cap_mult
        self.inv_book_frac = inv_book_frac
        self.jump_halt_pct = jump_halt_pct
        self.kill_drawdown_frac = kill_drawdown_frac
        self.blackout_hours = blackout_hours
        self.min_pm_notional = min_pm_notional
        self.maker_address = maker_address
        self.signature_type = signature_type

        self._quotes: dict[str, LpQuote] = {}   # key = f"{side}:{condition_id}"
        self._reserved_usd = 0.0
        self._inflight: set[str] = set()
        self._killed = False
        self._shutdown = False
        self._lock = asyncio.Lock()
        self._last_accrual = time.perf_counter()

    # ----------------------------------------------------------------- helpers
    def _deployed_usd(self) -> float:
        return self._reserved_usd + sum(m.cost_basis() for m in self.markets.values())

    def _net_pnl(self) -> float:
        trading = sum(m.trading_pnl() for m in self.markets.values())
        reward = sum((m.reward_onchain or m.reward_model) for m in self.markets.values())
        return trading + reward

    def _should_quote(self) -> bool:
        return self.armed and not self._shutdown and not self._killed

    def _alert(self, kind: str, payload: dict) -> None:
        logger.error("LP ALERT [%s]: %s", kind, payload)
        self.store.save_exec_event(f"lp_alert_{kind}", payload)

    # --------------------------------------------------------------- eligibility
    def _eligible(self, m: MarketState) -> bool:
        fresh = m.mid_ts and (time.perf_counter() - m.mid_ts) <= 3 * self.book_poll_s
        return bool(fresh and not m.halted and not m.blackout
                    and m.tick < m.mid < 1 - m.tick)

    def _skew(self, m: MarketState) -> tuple[bool, bool]:
        cap = self.inv_cap_mult * m.min_size
        return (m.inv < cap, m.inv > -cap)   # (quote_yes, quote_no)

    def _target(self, m: MarketState, side: str):
        """(exec_token, exec_price, size) for a side, or None if out-of-band/too small."""
        h = self.half_spread_ticks * m.tick
        if side == "yes":
            ref, token = m.mid, m.yes_token
            price = _snap(m.mid - h, m.tick)
        else:
            ref, token = 1.0 - m.mid, m.no_token
            price = _snap((1.0 - m.mid) - h, m.tick)
        if not (m.tick <= price <= 1 - m.tick):
            return None
        if abs(price - ref) > m.max_spread:        # must stay within reward band
            return None
        size = round(m.min_size * self.size_mult, 2)
        if size * price < self.min_pm_notional:
            return None
        return token, price, size

    # ------------------------------------------------------------- risk latches
    def _check_jump(self, m: MarketState) -> None:
        if m.prev_mid and m.prev_mid > 0 and \
                abs(m.mid - m.prev_mid) / m.prev_mid > self.jump_halt_pct and not m.halted:
            m.halted = True
            self._alert("jump_halt", {"condition_id": m.condition_id,
                                      "prev_mid": m.prev_mid, "mid": m.mid})

    def _check_blackout(self, m: MarketState) -> None:
        if m.end_ts is not None:
            hrs = (m.end_ts - datetime.now(timezone.utc).timestamp()) / 3600.0
            if hrs < self.blackout_hours and not m.blackout:
                m.blackout = True
                self.store.save_exec_event("lp_blackout",
                                           {"condition_id": m.condition_id, "hrs": hrs})

    def _check_killswitch(self) -> None:
        dep = self._deployed_usd()
        if dep > 0 and self._net_pnl() <= -self.kill_drawdown_frac * dep and not self._killed:
            self._killed = True
            self._alert("killswitch", {"net": round(self._net_pnl(), 2),
                                       "deployed": round(dep, 2)})

    # ------------------------------------------------------------------- loops
    async def book_refresh_loop(self) -> None:
        while not self._shutdown:
            for m in list(self.markets.values()):
                try:
                    b = await asyncio.to_thread(_get, f"{CLOB}/book?token_id={m.yes_token}")
                except Exception as e:  # noqa: BLE001 — a stalled poll must not crash
                    logger.warning("book poll %s failed: %s", m.condition_id[:10], e)
                    continue
                bids = [(float(o["price"]), float(o["size"])) for o in b.get("bids", [])]
                asks = [(float(o["price"]), float(o["size"])) for o in b.get("asks", [])]
                if not bids or not asks:
                    continue
                bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
                mid = (bb + ba) / 2
                sp = m.max_spread
                async with self._lock:
                    m.prev_mid = m.mid or mid
                    m.mid = mid
                    m.book_qual_bid = sum(s for p, s in bids if (mid - p) <= sp)
                    m.book_qual_ask = sum(s for p, s in asks if (p - mid) <= sp)
                    m.mid_ts = time.perf_counter()
                    self._check_jump(m)
                    self._check_blackout(m)
            await asyncio.sleep(self.book_poll_s)

    async def quoting_loop(self) -> None:
        await self._startup_cleanup()
        passes = 0
        while not self._shutdown:
            await asyncio.sleep(self.refresh_s)
            if not self._should_quote():
                if self._quotes:
                    await self.cancel_all()
                continue
            async with self._lock:
                for m in self.markets.values():
                    for side in ("yes", "no"):
                        await self._reconcile(m, side)
            passes += 1
            if passes % 10 == 0:
                logger.info("LP: %d quotes, $%.1f reserved, net $%.2f, deployed $%.1f",
                            len(self._quotes), self._reserved_usd, self._net_pnl(),
                            self._deployed_usd())

    async def fill_poll_loop(self) -> None:
        while not self._shutdown:
            await asyncio.sleep(self.poll_s)
            if not self.armed:
                continue
            async with self._lock:
                snapshot = [(k, q) for k, q in self._quotes.items()
                            if q.status == "resting" and q.order_id not in self._inflight
                            and not str(q.order_id).startswith("shadow-")]
            for key, q in snapshot:
                info = await self.pm_exec.get_order(q.order_id)
                matched = _num(info.get("size_matched")) or _num(info.get("sizeMatched")) or 0.0
                status = str(info.get("status", "")).upper()
                if matched > 1e-6 or status in ("MATCHED", "FILLED"):
                    await self._handle_fill(key, q, matched, info)

    async def reward_poll_loop(self) -> None:
        while not self._shutdown:
            await asyncio.sleep(self.reward_poll_s)
            if self.pm_exec._client is None:
                continue
            try:
                earned = await self._fetch_onchain_rewards()
            except Exception as e:  # noqa: BLE001
                logger.warning("reward fetch failed: %s", e)
                continue
            if not earned:
                continue
            async with self._lock:
                for cid, val in earned.items():
                    if cid in self.markets:
                        self.markets[cid].reward_onchain = val
            self.store.save_exec_event("lp_rewards_onchain",
                                       {"date": datetime.now(timezone.utc).date().isoformat(),
                                        "by_market": earned})

    async def metrics_loop(self) -> None:
        while not self._shutdown:
            await asyncio.sleep(self.metrics_s)
            async with self._lock:
                # accrue model reward for markets currently quoting (proxy)
                now = time.perf_counter()
                dt = now - self._last_accrual
                self._last_accrual = now
                resting_cids = {q.market.condition_id for q in self._quotes.values()}
                for m in self.markets.values():
                    if m.condition_id in resting_cids and m.qual >= 0:
                        S = m.min_size * self.size_mult
                        m.reward_model += m.pool_day * (S / (m.qual + S)) * (dt / 86400.0)
                self._check_killswitch()
                agg = {"net": round(self._net_pnl(), 4),
                       "reward_model": round(sum(m.reward_model for m in self.markets.values()), 4),
                       "reward_onchain": round(sum(m.reward_onchain for m in self.markets.values()), 4),
                       "trading_pnl": round(sum(m.trading_pnl() for m in self.markets.values()), 4),
                       "deployed": round(self._deployed_usd(), 2),
                       "reserved": round(self._reserved_usd, 2),
                       "open_quotes": len(self._quotes), "killed": self._killed}
                for m in self.markets.values():
                    self.store.save_exec_event("lp_market_metric", {
                        "condition_id": m.condition_id, "mid": m.mid, "inv": round(m.inv, 2),
                        "q_yes": round(m.q_yes, 2), "q_no": round(m.q_no, 2),
                        "mark": round(m.mark_value(), 4), "cash": round(m.cash, 4),
                        "reward_model": round(m.reward_model, 4),
                        "reward_onchain": round(m.reward_onchain, 4),
                        "halted": m.halted, "blackout": m.blackout})
            self.store.save_exec_event("lp_aggregate", agg)
            logger.info("LP metrics: net $%.2f (rew_model $%.2f rew_onchain $%.2f trade $%.2f) "
                        "deployed $%.1f quotes %d%s", agg["net"], agg["reward_model"],
                        agg["reward_onchain"], agg["trading_pnl"], agg["deployed"],
                        agg["open_quotes"], " KILLED" if self._killed else "")

    async def _fetch_onchain_rewards(self) -> dict[str, float]:
        """Actual earnings per condition_id, today. Best-effort via the V2 client;
        returns {} if the client doesn't expose an earnings helper (pre-flight will
        confirm the exact method on the armed box). Never raises into the loop."""
        client = self.pm_exec._client
        date = datetime.now(timezone.utc).date().isoformat()
        for meth in ("get_user_earnings", "get_earnings", "get_rewards_user_markets"):
            fn = getattr(client, meth, None)
            if fn is None:
                continue
            rows = await asyncio.to_thread(fn) if not _takes_args(fn) else \
                await asyncio.to_thread(fn, date, self.maker_address, self.signature_type)
            out: dict[str, float] = {}
            for r in (rows or []):
                cid = r.get("condition_id") or r.get("conditionId")
                earns = r.get("earnings") or []
                out[cid] = out.get(cid, 0.0) + sum(_num(e.get("earnings")) or 0.0 for e in earns)
            return out
        logger.info("LP: no earnings helper on V2 client; capture-rate pending manual reconcile")
        return {}

    # --------------------------------------------------------- reconcile / orders
    async def _reconcile(self, m: MarketState, side: str) -> None:
        key = f"{side}:{m.condition_id}"
        existing = self._quotes.get(key)
        quote_yes, quote_no = self._skew(m)
        want = (side == "yes" and quote_yes) or (side == "no" and quote_no)
        tgt = self._target(m, side) if (self._eligible(m) and want) else None

        if tgt is None:
            if existing and existing.status == "resting":
                await self._cancel_quote(key, reason="ineligible/skew/halt")
            return
        token, price, size = tgt
        # Market-level thinness gate (jump-risk cap): only quote if joining leaves us
        # at most inv_book_frac of the resulting book on BOTH sides -> book >= size*(1-f)/f.
        # Applied to both sides so we never quote one-sided (qScore rewards 2-sidedness).
        thin = size * (1 - self.inv_book_frac) / self.inv_book_frac
        if min(m.book_qual_bid, m.book_qual_ask) < thin:
            if existing and existing.status == "resting":
                await self._cancel_quote(key, reason="thin_book")
            return
        if existing is None:
            reserve = size * price
            if (len(self._quotes) >= self.max_open_quotes
                    or self._reserved_usd + sum(mm.cost_basis() for mm in self.markets.values())
                    + reserve > self.budget_usd):
                return
            await self._place_quote(key, m, side, token, price, size, reserve)
            return
        if existing.status != "resting":
            return
        moved = abs(price - existing.exec_price) >= self.reprice_eps
        too_young = (time.perf_counter() - existing.placed_ts) < self.refresh_s
        if not moved or too_young:
            return
        rc = existing.reprice_count + 1
        await self._cancel_quote(key, reason="reprice")
        reserve = size * price
        if (self._reserved_usd + sum(mm.cost_basis() for mm in self.markets.values())
                + reserve <= self.budget_usd):
            await self._place_quote(key, m, side, token, price, size, reserve, reprice_count=rc)

    async def _place_quote(self, key, m, side, token, price, size, reserve, reprice_count=0):
        order = self.pm_exec.build_order(token, "buy", price, size)
        ack = await self.pm_exec.place(order, order_type="GTC")
        oid = ack.get("order_id") or f"shadow-{key}"
        self._quotes[key] = LpQuote(market=m, side=side, exec_token=token,
                                    exec_price=ack.get("snapped_price") or price, size=size,
                                    order_id=oid, placed_ts=time.perf_counter(),
                                    reserved_usd=reserve, reprice_count=reprice_count)
        self._reserved_usd += reserve
        self.store.save_exec_event("lp_quote", {
            "condition_id": m.condition_id, "side": side, "token": token,
            "price": ack.get("snapped_price") or price, "size": size,
            "order_id": oid, "status": ack.get("status"), "reprice_count": reprice_count})

    async def _cancel_quote(self, key, reason="") -> None:
        q = self._quotes.pop(key, None)
        if not q:
            return
        self._reserved_usd -= q.reserved_usd
        if not str(q.order_id).startswith("shadow-"):
            await self.pm_exec.cancel(q.order_id)
        logger.info("LP cancel %s (%s)", key, reason)

    async def _handle_fill(self, key, q: LpQuote, matched: float, info: dict) -> None:
        async with self._lock:
            if self._quotes.get(key) is not q or q.order_id in self._inflight:
                return
            self._inflight.add(q.order_id)
            q.status = "filling"
        fill = min(matched, q.size) if matched > 0 else q.size
        m = q.market
        async with self._lock:
            if q.side == "yes":
                m.q_yes += fill
            else:
                m.q_no += fill
            m.cash -= q.exec_price * fill               # both sides are buys -> spend USDC
            self._reserved_usd -= q.reserved_usd
            self._quotes.pop(key, None)
            self._inflight.discard(q.order_id)
            self._check_killswitch()
        self.store.save_exec_event("lp_fill", {
            "condition_id": m.condition_id, "side": q.side, "token": q.exec_token,
            "price": q.exec_price, "size": fill, "inv_after": round(m.inv, 2),
            "time_resting_ms": round((time.perf_counter() - q.placed_ts) * 1e3, 1),
            "reprice_count": q.reprice_count})

    # ------------------------------------------------------------------ safety
    async def _startup_cleanup(self) -> None:
        ids = await self.pm_exec.open_order_ids()
        if ids:
            n = await self.pm_exec.cancel_all(ids)
            logger.warning("LP startup: cancelled %d orphaned resting order(s)", n)
            self.store.save_exec_event("lp_startup_cleanup", {"cancelled": n})

    async def cancel_all(self) -> None:
        async with self._lock:
            keys = list(self._quotes)
        for key in keys:
            await self._cancel_quote(key, reason="cancel_all")

    def shutdown(self) -> None:
        self._shutdown = True


def _takes_args(fn) -> bool:
    try:
        import inspect
        return len(inspect.signature(fn).parameters) > 0
    except (TypeError, ValueError):
        return False
