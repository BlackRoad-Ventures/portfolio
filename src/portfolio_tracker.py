#!/usr/bin/env python3
"""BlackRoad Ventures — Portfolio Tracker with PRISM analytics"""
import json, sqlite3, time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

DB = Path.home() / ".blackroad" / "ventures_portfolio.db"
DB.parent.mkdir(parents=True, exist_ok=True)

@dataclass
class Position:
    symbol: str
    asset_type: str  # equity | crypto | defi | fund
    quantity: float
    cost_basis: float
    current_price: float = 0.0
    currency: str = "USD"
    notes: str = ""

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def pnl(self) -> float:
        return self.market_value - (self.quantity * self.cost_basis)

    @property
    def pnl_pct(self) -> float:
        cost = self.quantity * self.cost_basis
        return ((self.market_value - cost) / cost * 100) if cost else 0.0

def init_db(db):
    db.execute("""CREATE TABLE IF NOT EXISTS positions (
        symbol TEXT, asset_type TEXT, quantity REAL, cost_basis REAL,
        current_price REAL DEFAULT 0, currency TEXT DEFAULT 'USD',
        notes TEXT, updated_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT, tx_type TEXT, quantity REAL, price REAL,
        fee REAL DEFAULT 0, executed_at TEXT, notes TEXT
    )""")
    db.commit()

class PortfolioTracker:
    def __init__(self):
        self.db = sqlite3.connect(str(DB))
        self.db.row_factory = sqlite3.Row
        init_db(self.db)

    def add_position(self, pos: Position):
        self.db.execute(
            "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?)",
            (pos.symbol, pos.asset_type, pos.quantity, pos.cost_basis,
             pos.current_price, pos.currency, pos.notes, datetime.utcnow().isoformat())
        )
        self.db.commit()

    def update_price(self, symbol: str, price: float):
        self.db.execute(
            "UPDATE positions SET current_price=?, updated_at=? WHERE symbol=?",
            (price, datetime.utcnow().isoformat(), symbol)
        )
        self.db.commit()

    def summary(self) -> dict:
        rows = self.db.execute("SELECT * FROM positions").fetchall()
        positions = []
        total_value = 0
        total_cost = 0
        for r in rows:
            pos = Position(**{k: r[k] for k in r.keys() if k != "updated_at"})
            total_value += pos.market_value
            total_cost += pos.quantity * pos.cost_basis
            positions.append({
                "symbol": pos.symbol,
                "type": pos.asset_type,
                "qty": pos.quantity,
                "value": round(pos.market_value, 2),
                "pnl": round(pos.pnl, 2),
                "pnl_pct": round(pos.pnl_pct, 1)
            })
        return {
            "positions": sorted(positions, key=lambda x: x["value"], reverse=True),
            "total_value": round(total_value, 2),
            "total_pnl": round(total_value - total_cost, 2),
            "total_pnl_pct": round(((total_value - total_cost) / total_cost * 100) if total_cost else 0, 1),
            "generated_at": datetime.utcnow().isoformat()
        }

if __name__ == "__main__":
    tracker = PortfolioTracker()
    # Sample positions
    tracker.add_position(Position("BTC", "crypto", 0.5, 42000, 68000))
    tracker.add_position(Position("ETH", "crypto", 5.0, 2200, 3800))
    tracker.add_position(Position("NVDA", "equity", 10, 450, 875))
    tracker.add_position(Position("BRDN", "fund", 1000, 1.0, 1.28, notes="BlackRoad Ventures seed fund"))
    print(json.dumps(tracker.summary(), indent=2))
