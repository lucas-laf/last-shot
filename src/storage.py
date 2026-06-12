"""SQLite for markets/matches/paper trades; parquet for tick data."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .models import (
    Market,
    MatchedPair,
    MatchStatus,
    MatchVerdict,
    Outcome,
    PaperTrade,
    Platform,
    Tick,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    platform TEXT NOT NULL,
    market_id TEXT NOT NULL,
    event_name TEXT,
    market_name TEXT,
    category TEXT,
    raw_category TEXT,
    market_type TEXT,
    outcomes_json TEXT,
    start_time TEXT,
    resolution_text TEXT,
    liquidity REAL,
    volume REAL,
    taker_fee REAL,
    active INTEGER,
    first_seen TEXT,
    last_seen TEXT,
    PRIMARY KEY (platform, market_id)
);

CREATE TABLE IF NOT EXISTS match_verdicts (
    betfair_market_id TEXT NOT NULL,
    polymarket_market_id TEXT NOT NULL,
    verdict_json TEXT NOT NULL,
    status TEXT NOT NULL,
    matched_at TEXT NOT NULL,
    PRIMARY KEY (betfair_market_id, polymarket_market_id)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    betfair_market_id TEXT,
    polymarket_market_id TEXT,
    outcome_name TEXT,
    bet_platform TEXT,
    side TEXT,
    entry_prob REAL,
    reference_prob REAL,
    edge_after_fees REAL,
    stake REAL,
    betfair_book TEXT,
    polymarket_book TEXT,
    settled INTEGER DEFAULT 0,
    won INTEGER,
    pnl REAL
);
"""


class Store:
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "ticks").mkdir(exist_ok=True)
        self.db_path = self.data_dir / "lastshot.db"
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        # WAL lets ad-hoc reader connections coexist with the tracker's writes;
        # busy_timeout makes writers wait out short locks instead of raising
        # (2026-06-12: a locked-database error killed the live tracker).
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    # ---------- markets ----------

    def upsert_markets(self, markets: list[Market]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                m.platform.value, m.market_id, m.event_name, m.market_name,
                m.category, m.raw_category, m.market_type,
                json.dumps([o.model_dump() for o in m.outcomes]),
                m.start_time.isoformat() if m.start_time else None,
                m.resolution_text, m.liquidity, m.volume, m.taker_fee,
                int(m.active), now, now,
            )
            for m in markets
        ]
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(platform, market_id) DO UPDATE SET
                     event_name=excluded.event_name, market_name=excluded.market_name,
                     category=excluded.category, raw_category=excluded.raw_category,
                     market_type=excluded.market_type,
                     outcomes_json=excluded.outcomes_json, start_time=excluded.start_time,
                     resolution_text=excluded.resolution_text, liquidity=excluded.liquidity,
                     volume=excluded.volume, taker_fee=excluded.taker_fee,
                     active=excluded.active, last_seen=excluded.last_seen""",
                rows,
            )

    def mark_unseen_inactive(self, platform: Platform, seen_ids: set[str]) -> int:
        """Markets we previously had but didn't see this scan are closed/suspended."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "SELECT market_id FROM markets WHERE platform=? AND active=1",
                (platform.value,),
            )
            stale = [r[0] for r in cur.fetchall() if r[0] not in seen_ids]
            self._conn.executemany(
                "UPDATE markets SET active=0 WHERE platform=? AND market_id=?",
                [(platform.value, mid) for mid in stale],
            )
        return len(stale)

    def get_markets(self, platform: Platform, active_only: bool = True) -> list[Market]:
        q = "SELECT * FROM markets WHERE platform=?" + (" AND active=1" if active_only else "")
        with self._lock:
            cur = self._conn.execute(q, (platform.value,))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return [self._row_to_market(r) for r in rows]

    @staticmethod
    def _row_to_market(r: dict) -> Market:
        return Market(
            platform=Platform(r["platform"]),
            market_id=r["market_id"],
            event_name=r["event_name"] or "",
            market_name=r["market_name"] or "",
            category=r["category"] or "",
            raw_category=r["raw_category"] or "",
            market_type=r["market_type"] or "",
            outcomes=[Outcome(**o) for o in json.loads(r["outcomes_json"] or "[]")],
            start_time=datetime.fromisoformat(r["start_time"]) if r["start_time"] else None,
            resolution_text=r["resolution_text"] or "",
            liquidity=r["liquidity"] or 0.0,
            volume=r["volume"] or 0.0,
            taker_fee=r["taker_fee"] or 0.0,
            active=bool(r["active"]),
        )

    # ---------- match verdicts ----------

    def has_verdict(self, betfair_id: str, polymarket_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM match_verdicts WHERE betfair_market_id=? AND polymarket_market_id=?",
                (betfair_id, polymarket_id),
            )
            return cur.fetchone() is not None

    def save_verdict(self, pair: MatchedPair) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO match_verdicts VALUES (?,?,?,?,?)""",
                (
                    pair.betfair_market_id, pair.polymarket_market_id,
                    pair.verdict.model_dump_json(), pair.status.value,
                    pair.matched_at.isoformat(),
                ),
            )

    def get_pairs(self, statuses: list[MatchStatus] | None = None) -> list[MatchedPair]:
        q = "SELECT * FROM match_verdicts"
        params: tuple = ()
        if statuses:
            q += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params = tuple(s.value for s in statuses)
        with self._lock:
            cur = self._conn.execute(q, params)
            rows = cur.fetchall()
        return [
            MatchedPair(
                betfair_market_id=r[0],
                polymarket_market_id=r[1],
                verdict=MatchVerdict.model_validate_json(r[2]),
                status=MatchStatus(r[3]),
                matched_at=datetime.fromisoformat(r[4]),
            )
            for r in rows
        ]

    def set_pair_status(self, betfair_id: str, polymarket_id: str, status: MatchStatus) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE match_verdicts SET status=? WHERE betfair_market_id=? AND polymarket_market_id=?",
                (status.value, betfair_id, polymarket_id),
            )

    # ---------- paper trades ----------

    def save_paper_trade(self, t: PaperTrade) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO paper_trades
                   (ts, signal_type, betfair_market_id, polymarket_market_id, outcome_name,
                    bet_platform, side, entry_prob, reference_prob, edge_after_fees, stake,
                    betfair_book, polymarket_book, settled, won, pnl)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    t.ts.isoformat(), t.signal_type.value, t.betfair_market_id,
                    t.polymarket_market_id, t.outcome_name, t.bet_platform.value, t.side,
                    t.entry_prob, t.reference_prob, t.edge_after_fees, t.stake,
                    t.betfair_book, t.polymarket_book, int(t.settled),
                    None if t.won is None else int(t.won), t.pnl,
                ),
            )


class TickWriter:
    """Buffers ticks and appends them to hourly parquet files."""

    SCHEMA = pa.schema(
        [
            ("ts", pa.timestamp("us", tz="UTC")),
            ("platform", pa.string()),
            ("market_id", pa.string()),
            ("outcome_id", pa.string()),
            ("bid", pa.float64()),
            ("ask", pa.float64()),
            ("bid_size", pa.float64()),
            ("ask_size", pa.float64()),
            ("source_mode", pa.string()),
        ]
    )

    def __init__(self, data_dir: str, flush_rows: int = 500):
        self.dir = Path(data_dir) / "ticks"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.flush_rows = flush_rows
        self._buf: list[Tick] = []
        self._lock = threading.Lock()

    def write(self, tick: Tick) -> None:
        with self._lock:
            self._buf.append(tick)
            if len(self._buf) >= self.flush_rows:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buf:
            return
        batch = self._buf
        self._buf = []
        table = pa.table(
            {
                "ts": [t.ts for t in batch],
                "platform": [t.platform.value for t in batch],
                "market_id": [t.market_id for t in batch],
                "outcome_id": [t.outcome_id for t in batch],
                "bid": [t.quote.bid for t in batch],
                "ask": [t.quote.ask for t in batch],
                "bid_size": [t.quote.bid_size for t in batch],
                "ask_size": [t.quote.ask_size for t in batch],
                "source_mode": [t.source_mode for t in batch],
            },
            schema=self.SCHEMA,
        )
        fname = self.dir / f"ticks_{datetime.now(timezone.utc):%Y%m%d_%H}.parquet"
        if fname.exists():
            existing = pq.read_table(fname)
            table = pa.concat_tables([existing, table])
        pq.write_table(table, fname)
