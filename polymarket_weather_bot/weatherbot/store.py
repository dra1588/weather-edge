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
        CREATE TABLE IF NOT EXISTS trade_exits (
          id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL, tranche TEXT NOT NULL,
          trigger_loss REAL, exit_price REAL NOT NULL, size_usd REAL NOT NULL,
          pnl REAL NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(trade_id) REFERENCES trades(id)
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
            "remaining_size_usd": "REAL",
            "realized_pnl": "REAL NOT NULL DEFAULT 0",
            "unrealized_pnl": "REAL NOT NULL DEFAULT 0",
            "stop_stage": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {definition}")
        self.conn.execute(
            "UPDATE trades SET remaining_size_usd=size_usd WHERE remaining_size_usd IS NULL"
        )
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
              created_at, status, external_order_id, current_price, pnl, updated_at,
              remaining_size_usd, realized_pnl, unrealized_pnl, stop_stage
            ) VALUES(?,?,?,?,?,?,?,?,'open',?,?,0,?,?,0,0,0)
        """, (
            signal.market.market_id, signal.token_id, signal.market.city_key,
            signal.market.target_date.isoformat(), mode, signal.entry_price, signal.size_usd,
            now, external_order_id, signal.entry_price, now, signal.size_usd,
        ))
        self.conn.commit()

    def open_trades(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY id"
        ).fetchall()]

    def _exit_tranche(self, trade: sqlite3.Row, label: str, trigger: float | None,
                      current_price: float, size_usd: float):
        size_usd = min(size_usd, float(trade["remaining_size_usd"]))
        if size_usd <= 0:
            return
        pnl = (current_price - float(trade["entry_price"])) * (size_usd / float(trade["entry_price"]))
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO trade_exits(trade_id,tranche,trigger_loss,exit_price,size_usd,pnl,created_at)
            VALUES(?,?,?,?,?,?,?)
        """, (trade["id"], label, trigger, current_price, size_usd, round(pnl, 4), now))
        self.conn.execute("""
            UPDATE trades SET remaining_size_usd=MAX(0,remaining_size_usd-?),
              realized_pnl=realized_pnl+?, updated_at=? WHERE id=?
        """, (size_usd, round(pnl, 4), now, trade["id"]))

    def mark_trade(self, trade_id: int, current_price: float, settled: bool = False,
                   apply_stops: bool = True):
        row = self.conn.execute(
            "SELECT * FROM trades WHERE id=?", (trade_id,)
        ).fetchone()
        if not row:
            return
        loss = current_price / float(row["entry_price"]) - 1
        # Paper exits are measured against original cost so each tranche is deterministic.
        if apply_stops and not settled:
            stages = ((-0.15, 0.50, "SL1"), (-0.25, 0.25, "SL2"), (-0.35, 0.25, "SL3"))
            stage = int(row["stop_stage"] or 0)
            while stage < len(stages) and loss <= stages[stage][0]:
                trigger, fraction, label = stages[stage]
                self._exit_tranche(row, label, trigger, current_price, float(row["size_usd"]) * fraction)
                stage += 1
                self.conn.execute("UPDATE trades SET stop_stage=? WHERE id=?", (stage, trade_id))
                row = self.conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if settled and float(row["remaining_size_usd"]) > 0:
            self._exit_tranche(row, "settlement", None, current_price, float(row["remaining_size_usd"]))
            row = self.conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        unrealized = 0.0 if settled else (
            (current_price - float(row["entry_price"]))
            * (float(row["remaining_size_usd"]) / float(row["entry_price"]))
        )
        total_pnl = round(float(row["realized_pnl"]) + unrealized, 4)
        now = datetime.now(timezone.utc).isoformat()
        if settled:
            result = "win" if total_pnl > 0.005 else "loss" if total_pnl < -0.005 else "breakeven"
            self.conn.execute("""
                UPDATE trades SET current_price=?, exit_price=?, pnl=?, unrealized_pnl=0,
                  result=?, status='settled', updated_at=?, settled_at=? WHERE id=?
            """, (current_price, current_price, total_pnl, result, now, now, trade_id))
        elif float(row["remaining_size_usd"]) <= 0:
            result = "win" if total_pnl > 0.005 else "loss" if total_pnl < -0.005 else "breakeven"
            self.conn.execute("""
                UPDATE trades SET current_price=?, exit_price=?, pnl=?, unrealized_pnl=0,
                  result=?, status='stopped', updated_at=?, settled_at=? WHERE id=?
            """, (current_price, current_price, total_pnl, result, now, now, trade_id))
        else:
            self.conn.execute(
                "UPDATE trades SET current_price=?, pnl=?, unrealized_pnl=?, updated_at=? WHERE id=?",
                (current_price, total_pnl, round(unrealized, 4), now, trade_id),
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
              COALESCE(SUM(realized_pnl),0) realized,
              COALESCE(SUM(CASE WHEN status='open' THEN unrealized_pnl ELSE 0 END),0) unrealized,
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
            "stop_plan": [
                {"trigger": -0.15, "sell_fraction": 0.50},
                {"trigger": -0.25, "sell_fraction": 0.25},
                {"trigger": -0.35, "sell_fraction": 0.25},
            ],
        }
