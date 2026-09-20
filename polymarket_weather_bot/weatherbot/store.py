import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Signal


class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
          id INTEGER PRIMARY KEY, market_id TEXT NOT NULL, scanned_at TEXT NOT NULL,
          city TEXT NOT NULL, target_date TEXT NOT NULL, probability REAL NOT NULL,
          entry_price REAL NOT NULL, edge REAL NOT NULL, size_usd REAL NOT NULL, reason TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trades (
          id INTEGER PRIMARY KEY, market_id TEXT NOT NULL UNIQUE, token_id TEXT NOT NULL,
          city TEXT NOT NULL, target_date TEXT NOT NULL, mode TEXT NOT NULL,
          entry_price REAL NOT NULL, size_usd REAL NOT NULL, created_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open', external_order_id TEXT
        );
        """)

    def log_signal(self, signal: Signal):
        self.conn.execute("INSERT INTO signals VALUES(NULL,?,?,?,?,?,?,?,?,?)", (
            signal.market.market_id, datetime.now(timezone.utc).isoformat(), signal.market.city_key,
            signal.market.target_date.isoformat(), signal.model_probability, signal.entry_price,
            signal.edge, signal.size_usd, signal.reason,
        ))
        self.conn.commit()

    def already_traded(self, market_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM trades WHERE market_id=?", (market_id,)).fetchone() is not None

    def open_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0])

    def risk_today(self, city: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(size_usd),0) FROM trades WHERE status='open' AND date(created_at)=date('now')"
        args = ()
        if city:
            sql += " AND city=?"
            args = (city,)
        return float(self.conn.execute(sql, args).fetchone()[0])

    def record_trade(self, signal: Signal, mode: str, external_order_id: str | None = None):
        self.conn.execute("INSERT INTO trades VALUES(NULL,?,?,?,?,?,?,?,?, 'open', ?)", (
            signal.market.market_id, signal.token_id, signal.market.city_key,
            signal.market.target_date.isoformat(), mode, signal.entry_price, signal.size_usd,
            datetime.now(timezone.utc).isoformat(), external_order_id,
        ))
        self.conn.commit()
