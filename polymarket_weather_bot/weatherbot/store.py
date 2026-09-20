import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Signal


class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
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
        self._migrate_trades()

    def _migrate_trades(self):
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(trades)")}
        additions = {
            "current_price": "REAL",
            "exit_price": "REAL",
            "pnl": "REAL NOT NULL DEFAULT 0",
            "result": "TEXT",
            "updated_at": "TEXT",
            "settled_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {definition}")
        self.conn.commit()

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
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO trades (
              market_id, token_id, city, target_date, mode, entry_price, size_usd,
              created_at, status, external_order_id, current_price, pnl, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,'open',?,?,0,?)
        """, (
            signal.market.market_id, signal.token_id, signal.market.city_key,
            signal.market.target_date.isoformat(), mode, signal.entry_price, signal.size_usd,
            now, external_order_id, signal.entry_price, now,
        ))
        self.conn.commit()

    def open_trades(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY id"
        ).fetchall()]

    def mark_trade(self, trade_id: int, current_price: float, settled: bool = False):
        row = self.conn.execute(
            "SELECT entry_price, size_usd FROM trades WHERE id=?", (trade_id,)
        ).fetchone()
        if not row:
            return
        shares = float(row["size_usd"]) / float(row["entry_price"])
        pnl = round((current_price - float(row["entry_price"])) * shares, 4)
        now = datetime.now(timezone.utc).isoformat()
        if settled:
            result = "win" if pnl > 0.005 else "loss" if pnl < -0.005 else "breakeven"
            self.conn.execute("""
                UPDATE trades SET current_price=?, exit_price=?, pnl=?, result=?,
                  status='settled', updated_at=?, settled_at=? WHERE id=?
            """, (current_price, current_price, pnl, result, now, now, trade_id))
        else:
            self.conn.execute(
                "UPDATE trades SET current_price=?, pnl=?, updated_at=? WHERE id=?",
                (current_price, pnl, now, trade_id),
            )
        self.conn.commit()

    def dashboard(self, limit: int = 100) -> dict:
        signals = [dict(row) for row in self.conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
        trades = [dict(row) for row in self.conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
        totals = self.conn.execute("""
            SELECT
              COALESCE(SUM(CASE WHEN status='settled' THEN pnl ELSE 0 END),0) realized,
              COALESCE(SUM(CASE WHEN status='open' THEN pnl ELSE 0 END),0) unrealized,
              SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) wins,
              SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) losses
            FROM trades
        """).fetchone()
        realized = float(totals["realized"])
        unrealized = float(totals["unrealized"])
        return {
            "signals": signals,
            "trades": trades,
            "open_positions": self.open_count(),
            "risk_today": self.risk_today(),
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": realized + unrealized,
            "wins": int(totals["wins"] or 0),
            "losses": int(totals["losses"] or 0),
        }
