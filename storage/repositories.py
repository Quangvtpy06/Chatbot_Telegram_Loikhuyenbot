"""Repositories cho Risk Gate: CooldownRepository, PositionRepository, RiskDecisionRepository.

Tất cả đều dùng SQLite. Schema được tạo tự động (idempotent) khi khởi tạo repository.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator, Optional

from .migrations import migrate_database

LOGGER = logging.getLogger("dnse.risk_gate.repositories")


# ---------------------------------------------------------------------------
# Helper — context manager mở/đóng connection an toàn
# ---------------------------------------------------------------------------

@contextmanager
def _connect(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Mở SQLite connection, commit khi thành công, rollback khi có lỗi."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# RiskDecision — dataclass cho audit trail
# ---------------------------------------------------------------------------

@dataclass
class RiskDecision:
    """Kết quả đánh giá rủi ro của Risk Gate cho một SignalEvent."""

    decision_id: str
    signal_id: str
    symbol: str
    action: str
    verdict: str  # "ALLOW" | "BLOCK"
    block_reasons: list[str] = field(default_factory=list)
    allow_reasons: list[str] = field(default_factory=list)
    checked_at: str = ""
    reward_risk_ratio: Optional[float] = None
    position_size_pct: Optional[float] = None
    cooldown_remaining_seconds: int = 0
    planned_quantity: Optional[int] = None
    planned_notional: Optional[float] = None
    daily_loss_pct: Optional[float] = None
    drawdown_pct: Optional[float] = None
    liquidity_value_20d: Optional[float] = None

    def __post_init__(self) -> None:
        if self.verdict not in {"ALLOW", "BLOCK"}:
            raise ValueError(f"verdict phải là 'ALLOW' hoặc 'BLOCK', nhận được: {self.verdict!r}")

    @property
    def is_allowed(self) -> bool:
        return self.verdict == "ALLOW"


# ---------------------------------------------------------------------------
# CooldownRepository
# ---------------------------------------------------------------------------

_COOLDOWN_DDL = """
CREATE TABLE IF NOT EXISTS signal_cooldowns (
    symbol          TEXT NOT NULL,
    action          TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    last_allowed_at TEXT NOT NULL,
    PRIMARY KEY (symbol, action, strategy)
);
"""


class CooldownRepository:
    """Persistent cooldown store dùng SQLite."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        migrate_database(self.db_path)

    def _ensure_schema(self) -> None:
        with _connect(self.db_path) as conn:
            conn.executescript(_COOLDOWN_DDL)

    def get_last_allowed_signal(
        self, symbol: str, action: str, strategy: str,
    ) -> Optional[datetime]:
        with _connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT last_allowed_at FROM signal_cooldowns "
                "WHERE symbol = ? AND action = ? AND strategy = ?",
                (symbol.upper(), action.upper(), strategy),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        try:
            dt = datetime.fromisoformat(row["last_allowed_at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None

    def record_allowed_signal(
        self, symbol: str, action: str, strategy: str, timestamp: datetime,
    ) -> None:
        iso = timestamp.astimezone(timezone.utc).isoformat()
        with _connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO signal_cooldowns (symbol, action, strategy, last_allowed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol, action, strategy)
                    DO UPDATE SET last_allowed_at = excluded.last_allowed_at
                """,
                (symbol.upper(), action.upper(), strategy, iso),
            )


# ---------------------------------------------------------------------------
# PositionRepository
# ---------------------------------------------------------------------------

_POSITION_DDL = """
CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    sector          TEXT,
    status          TEXT NOT NULL DEFAULT 'OPEN',
    entry_price     REAL NOT NULL,
    quantity        REAL NOT NULL DEFAULT 0,
    nav_pct         REAL NOT NULL DEFAULT 0,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_symbol_status ON positions(symbol, status);
CREATE INDEX IF NOT EXISTS idx_positions_sector_status ON positions(sector, status);
"""


class PositionRepository:
    def __init__(self, db_path: Path, initial_nav: float = 1_000_000_000.0) -> None:
        self.db_path = db_path
        migrate_database(self.db_path)
        self.configure_initial_nav(initial_nav)

    def _ensure_schema(self) -> None:
        with _connect(self.db_path) as conn:
            conn.executescript(_POSITION_DDL)

    def configure_initial_nav(self, initial_nav: float) -> None:
        """Đặt NAV khởi tạo khi danh mục chưa phát sinh giao dịch."""

        if initial_nav <= 0:
            raise ValueError("initial_nav phải > 0")
        with _connect(self.db_path) as conn:
            event_count = int(
                conn.execute("SELECT COUNT(*) FROM position_events").fetchone()[0]
            )
            position_count = int(
                conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
            )
            if event_count == 0 and position_count == 0:
                conn.execute(
                    """
                    UPDATE portfolio_state
                    SET initial_nav = ?, cash_balance = ?, current_nav = ?,
                        peak_nav = ?, realized_pnl = 0, updated_at = ?
                    WHERE portfolio_id = 1
                    """,
                    (
                        float(initial_nav),
                        float(initial_nav),
                        float(initial_nav),
                        float(initial_nav),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

    def count_open_positions(self) -> int:
        with _connect(self.db_path) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'")
            return int(cursor.fetchone()[0])

    def has_open_position(self, symbol: str) -> bool:
        with _connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT 1 FROM positions WHERE symbol = ? AND status = 'OPEN' LIMIT 1",
                (symbol.upper(),),
            )
            return cursor.fetchone() is not None

    def get_symbol_exposure_pct(self, symbol: str) -> float:
        with _connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT COALESCE(SUM(nav_pct), 0.0) FROM positions "
                "WHERE symbol = ? AND status = 'OPEN'",
                (symbol.upper(),),
            )
            return float(cursor.fetchone()[0])

    def get_sector_exposure_pct(self, sector: str) -> float:
        with _connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT COALESCE(SUM(nav_pct), 0.0) FROM positions "
                "WHERE sector = ? AND status = 'OPEN'",
                (sector,),
            )
            return float(cursor.fetchone()[0])

    def get_total_exposure_pct(self) -> float:
        with _connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT COALESCE(SUM(nav_pct), 0.0) FROM positions WHERE status = 'OPEN'"
            )
            return float(cursor.fetchone()[0])

    def add_position(
        self, symbol: str, entry_price: float, nav_pct: float,
        sector: Optional[str] = None, quantity: float = 0.0,
        *, price_unit_vnd: float = 1000.0, signal_id: str | None = None,
    ) -> None:
        with _connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO positions (
                    symbol, sector, status, entry_price, quantity, nav_pct,
                    opened_at, last_price, price_unit_vnd, market_value,
                    unrealized_pnl, signal_id
                ) VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    symbol.upper(), sector, entry_price, quantity, nav_pct,
                    datetime.now(timezone.utc).isoformat(), entry_price,
                    price_unit_vnd, quantity * entry_price * price_unit_vnd,
                    signal_id,
                ),
            )

    def record_manual_open(
        self,
        symbol: str,
        entry_price: float,
        quantity: float,
        *,
        price_unit_vnd: float = 1000.0,
        sector: Optional[str] = None,
    ) -> "PortfolioExecution":
        """Ghi nhận vị thế người dùng đã mua ngoài bot."""

        symbol = symbol.upper()
        if entry_price <= 0 or quantity <= 0 or price_unit_vnd <= 0:
            return PortfolioExecution(False, "Giá và khối lượng phải lớn hơn 0")
        now = datetime.now(timezone.utc)
        notional = entry_price * quantity * price_unit_vnd
        signal_id = f"MANUAL-OPEN-{uuid.uuid4()}"
        with _connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute(
                "SELECT * FROM portfolio_state WHERE portfolio_id = 1"
            ).fetchone()
            if float(state["cash_balance"]) < notional:
                return PortfolioExecution(False, "Không đủ số dư tiền mặt trong danh mục")
            if conn.execute(
                "SELECT 1 FROM positions WHERE symbol = ? AND status = 'OPEN' LIMIT 1",
                (symbol,),
            ).fetchone() is not None:
                return PortfolioExecution(False, f"{symbol} đã có vị thế đang mở")
            current_nav = float(state["current_nav"])
            conn.execute(
                """
                INSERT INTO positions (
                    symbol, sector, status, entry_price, quantity, nav_pct,
                    opened_at, last_price, price_unit_vnd, market_value,
                    unrealized_pnl, signal_id
                ) VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    symbol, sector, entry_price, quantity,
                    notional / current_nav if current_nav > 0 else 0.0,
                    now.isoformat(), entry_price, price_unit_vnd, notional, signal_id,
                ),
            )
            conn.execute(
                """
                UPDATE portfolio_state
                SET cash_balance = cash_balance - ?, updated_at = ?
                WHERE portfolio_id = 1
                """,
                (notional, now.isoformat()),
            )
            conn.execute(
                """
                INSERT INTO position_events (
                    event_id, signal_id, symbol, action, quantity, price,
                    price_unit_vnd, notional, realized_pnl, executed_at
                ) VALUES (?, ?, ?, 'BUY', ?, ?, ?, ?, 0, ?)
                """,
                (
                    str(uuid.uuid4()), signal_id, symbol, quantity, entry_price,
                    price_unit_vnd, notional, now.isoformat(),
                ),
            )
        return PortfolioExecution(
            True, f"Đã ghi nhận mua {symbol}", quantity=quantity, notional=notional
        )

    def record_manual_close(
        self,
        symbol: str,
        close_price: float | None = None,
        *,
        price_unit_vnd: float | None = None,
    ) -> "PortfolioExecution":
        """Đóng vị thế thủ công theo giá người dùng khai báo hoặc giá cuối cùng."""

        symbol = symbol.upper()
        now = datetime.now(timezone.utc)
        signal_id = f"MANUAL-CLOSE-{uuid.uuid4()}"
        with _connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            positions = conn.execute(
                "SELECT * FROM positions WHERE symbol = ? AND status = 'OPEN'",
                (symbol,),
            ).fetchall()
            if not positions:
                return PortfolioExecution(False, f"Không có vị thế mở cho {symbol}")
            price = close_price or float(
                positions[0]["last_price"] or positions[0]["entry_price"]
            )
            unit = price_unit_vnd or float(positions[0]["price_unit_vnd"] or 1000.0)
            if price <= 0 or unit <= 0:
                return PortfolioExecution(False, "Giá đóng vị thế không hợp lệ")
            quantity = sum(float(row["quantity"] or 0.0) for row in positions)
            notional = sum(
                float(row["quantity"] or 0.0) * price
                * float(row["price_unit_vnd"] or unit)
                for row in positions
            )
            realized = sum(
                float(row["quantity"] or 0.0)
                * (price - float(row["entry_price"]))
                * float(row["price_unit_vnd"] or unit)
                for row in positions
            )
            conn.execute(
                """
                UPDATE positions
                SET status = 'CLOSED', closed_at = ?, closed_price = ?,
                    last_price = ?, market_value = 0, unrealized_pnl = 0,
                    realized_pnl = ?
                WHERE symbol = ? AND status = 'OPEN'
                """,
                (now.isoformat(), price, price, realized, symbol),
            )
            conn.execute(
                """
                UPDATE portfolio_state
                SET cash_balance = cash_balance + ?,
                    realized_pnl = realized_pnl + ?, updated_at = ?
                WHERE portfolio_id = 1
                """,
                (notional, realized, now.isoformat()),
            )
            conn.execute(
                """
                INSERT INTO position_events (
                    event_id, signal_id, symbol, action, quantity, price,
                    price_unit_vnd, notional, realized_pnl, executed_at
                ) VALUES (?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), signal_id, symbol, quantity, price, unit,
                    notional, realized, now.isoformat(),
                ),
            )
        self.mark_to_market({})
        return PortfolioExecution(
            True, f"Đã đóng vị thế {symbol}", quantity=quantity,
            notional=notional, realized_pnl=realized,
        )

    def get_open_position(self, symbol: str) -> Optional[dict[str, object]]:
        with _connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM positions
                WHERE symbol = ? AND status = 'OPEN'
                ORDER BY opened_at LIMIT 1
                """,
                (symbol.upper(),),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_open_positions(self) -> list[dict[str, object]]:
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY symbol, opened_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_portfolio_metrics(self) -> "PortfolioMetrics":
        with _connect(self.db_path) as conn:
            state = conn.execute(
                "SELECT * FROM portfolio_state WHERE portfolio_id = 1"
            ).fetchone()
            today = datetime.now(timezone.utc).date().isoformat()
            snapshot = conn.execute(
                "SELECT * FROM portfolio_snapshots WHERE trading_date = ?",
                (today,),
            ).fetchone()
        current_nav = float(state["current_nav"])
        peak_nav = float(state["peak_nav"])
        daily_pnl_pct = float(snapshot["daily_pnl_pct"]) if snapshot else 0.0
        drawdown_pct = (current_nav / peak_nav - 1.0) if peak_nav > 0 else 0.0
        return PortfolioMetrics(
            initial_nav=float(state["initial_nav"]),
            cash_balance=float(state["cash_balance"]),
            current_nav=current_nav,
            peak_nav=peak_nav,
            realized_pnl=float(state["realized_pnl"]),
            daily_pnl_pct=daily_pnl_pct,
            drawdown_pct=drawdown_pct,
            total_exposure_pct=self.get_total_exposure_pct(),
            open_positions=self.count_open_positions(),
        )

    def mark_to_market(self, prices: dict[str, tuple[float, float]]) -> "PortfolioMetrics":
        """Cập nhật giá vị thế; ``prices`` là symbol -> (price, price_unit_vnd)."""

        now = datetime.now(timezone.utc)
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status = 'OPEN'"
            ).fetchall()
            market_value = 0.0
            for row in rows:
                symbol = str(row["symbol"])
                price, unit = prices.get(
                    symbol,
                    (
                        float(row["last_price"] or row["entry_price"]),
                        float(row["price_unit_vnd"] or 1000.0),
                    ),
                )
                quantity = float(row["quantity"] or 0.0)
                value = quantity * price * unit
                unrealized = quantity * (price - float(row["entry_price"])) * unit
                market_value += value
                conn.execute(
                    """
                    UPDATE positions
                    SET last_price = ?, price_unit_vnd = ?, market_value = ?,
                        unrealized_pnl = ?
                    WHERE id = ?
                    """,
                    (price, unit, value, unrealized, row["id"]),
                )
            state = conn.execute(
                "SELECT * FROM portfolio_state WHERE portfolio_id = 1"
            ).fetchone()
            cash = float(state["cash_balance"])
            current_nav = cash + market_value
            peak_nav = max(float(state["peak_nav"]), current_nav)
            if current_nav > 0:
                conn.execute(
                    """
                    UPDATE positions
                    SET nav_pct = COALESCE(market_value, 0) / ?
                    WHERE status = 'OPEN' AND quantity > 0
                    """,
                    (current_nav,),
                )
            conn.execute(
                """
                UPDATE portfolio_state
                SET current_nav = ?, peak_nav = ?, updated_at = ?
                WHERE portfolio_id = 1
                """,
                (current_nav, peak_nav, now.isoformat()),
            )
            self._upsert_daily_snapshot(conn, current_nav, peak_nav, now)
        return self.get_portfolio_metrics()

    def apply_allowed_signal(
        self,
        signal: object,
        decision: RiskDecision,
        *,
        board_lot_size: int = 100,
    ) -> "PortfolioExecution":
        """Khớp mô phỏng BUY/SELL một lần theo signal_id."""

        if decision.verdict != "ALLOW" or decision.signal_id != getattr(signal, "signal_id"):
            return PortfolioExecution(False, "Tín hiệu chưa được Risk Gate ALLOW")
        action = str(getattr(signal, "action"))
        if action not in {"BUY", "SELL"}:
            return PortfolioExecution(False, f"{action} không làm thay đổi vị thế")
        price = float(getattr(signal, "reference_price") or 0.0)
        if price <= 0:
            return PortfolioExecution(False, "Thiếu giá thực hiện")
        metadata = getattr(signal, "metadata", {}) or {}
        price_unit = float(metadata.get("price_unit_vnd") or 1000.0)
        symbol = str(getattr(signal, "symbol")).upper()
        now = datetime.now(timezone.utc)

        with _connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM position_events WHERE signal_id = ?",
                (decision.signal_id,),
            ).fetchone()
            if existing is not None:
                return PortfolioExecution(
                    False,
                    "Signal đã cập nhật danh mục trước đó",
                    quantity=float(existing["quantity"]),
                    notional=float(existing["notional"]),
                    realized_pnl=float(existing["realized_pnl"]),
                )

            state = conn.execute(
                "SELECT * FROM portfolio_state WHERE portfolio_id = 1"
            ).fetchone()
            if action == "BUY":
                size_pct = float(decision.position_size_pct or 0.0)
                budget = min(
                    float(state["current_nav"]) * size_pct,
                    float(state["cash_balance"]),
                )
                raw_quantity = int(budget // (price * price_unit))
                quantity = (raw_quantity // board_lot_size) * board_lot_size
                if decision.planned_quantity is not None:
                    quantity = min(quantity, int(decision.planned_quantity))
                    quantity = (quantity // board_lot_size) * board_lot_size
                if quantity < board_lot_size:
                    return PortfolioExecution(False, "Không đủ NAV cho một lô chẵn")
                notional = quantity * price * price_unit
                conn.execute(
                    """
                    INSERT INTO positions (
                        symbol, sector, status, entry_price, quantity, nav_pct,
                        opened_at, last_price, price_unit_vnd, market_value,
                        unrealized_pnl, signal_id
                    ) VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        symbol, getattr(signal, "sector", None), price, quantity,
                        notional / float(state["current_nav"]), now.isoformat(),
                        price, price_unit, notional, decision.signal_id,
                    ),
                )
                conn.execute(
                    """
                    UPDATE portfolio_state
                    SET cash_balance = cash_balance - ?, updated_at = ?
                    WHERE portfolio_id = 1
                    """,
                    (notional, now.isoformat()),
                )
                realized = 0.0
            else:
                positions = conn.execute(
                    "SELECT * FROM positions WHERE symbol = ? AND status = 'OPEN'",
                    (symbol,),
                ).fetchall()
                if not positions:
                    return PortfolioExecution(False, "Không có vị thế để SELL")
                quantity = sum(float(row["quantity"] or 0.0) for row in positions)
                notional = sum(
                    float(row["quantity"] or 0.0) * price * float(row["price_unit_vnd"] or price_unit)
                    for row in positions
                )
                realized = sum(
                    float(row["quantity"] or 0.0)
                    * (price - float(row["entry_price"]))
                    * float(row["price_unit_vnd"] or price_unit)
                    for row in positions
                )
                conn.execute(
                    """
                    UPDATE positions
                    SET status = 'CLOSED', closed_at = ?, closed_price = ?,
                        last_price = ?, market_value = 0, unrealized_pnl = 0,
                        realized_pnl = ?
                    WHERE symbol = ? AND status = 'OPEN'
                    """,
                    (now.isoformat(), price, price, realized, symbol),
                )
                conn.execute(
                    """
                    UPDATE portfolio_state
                    SET cash_balance = cash_balance + ?,
                        realized_pnl = realized_pnl + ?, updated_at = ?
                    WHERE portfolio_id = 1
                    """,
                    (notional, realized, now.isoformat()),
                )

            conn.execute(
                """
                INSERT INTO position_events (
                    event_id, signal_id, symbol, action, quantity, price,
                    price_unit_vnd, notional, realized_pnl, executed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), decision.signal_id, symbol, action,
                    quantity, price, price_unit, notional, realized, now.isoformat(),
                ),
            )

        self.mark_to_market({symbol: (price, price_unit)})
        return PortfolioExecution(
            True,
            f"Đã mô phỏng {action} {symbol}",
            quantity=quantity,
            notional=notional,
            realized_pnl=realized,
        )

    @staticmethod
    def _upsert_daily_snapshot(
        conn: sqlite3.Connection,
        current_nav: float,
        peak_nav: float,
        now: datetime,
    ) -> None:
        trading_date = now.date().isoformat()
        existing = conn.execute(
            "SELECT opening_nav FROM portfolio_snapshots WHERE trading_date = ?",
            (trading_date,),
        ).fetchone()
        opening_nav = float(existing["opening_nav"]) if existing else current_nav
        daily_pnl = current_nav - opening_nav
        daily_pct = daily_pnl / opening_nav if opening_nav > 0 else 0.0
        drawdown = current_nav / peak_nav - 1.0 if peak_nav > 0 else 0.0
        conn.execute(
            """
            INSERT INTO portfolio_snapshots (
                trading_date, opening_nav, closing_nav, peak_nav,
                daily_pnl, daily_pnl_pct, drawdown_pct, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trading_date) DO UPDATE SET
                closing_nav = excluded.closing_nav,
                peak_nav = excluded.peak_nav,
                daily_pnl = excluded.daily_pnl,
                daily_pnl_pct = excluded.daily_pnl_pct,
                drawdown_pct = excluded.drawdown_pct,
                updated_at = excluded.updated_at
            """,
            (
                trading_date, opening_nav, current_nav, peak_nav,
                daily_pnl, daily_pct, drawdown, now.isoformat(),
            ),
        )


@dataclass(frozen=True)
class PortfolioMetrics:
    initial_nav: float
    cash_balance: float
    current_nav: float
    peak_nav: float
    realized_pnl: float
    daily_pnl_pct: float
    drawdown_pct: float
    total_exposure_pct: float
    open_positions: int


@dataclass(frozen=True)
class PortfolioExecution:
    applied: bool
    reason: str
    quantity: float = 0.0
    notional: float = 0.0
    realized_pnl: float = 0.0


# ---------------------------------------------------------------------------
# RiskDecisionRepository — audit trail
# ---------------------------------------------------------------------------

_RISK_DECISION_DDL = """
CREATE TABLE IF NOT EXISTS risk_decisions (
    decision_id                 TEXT PRIMARY KEY,
    signal_id                   TEXT NOT NULL,
    symbol                      TEXT NOT NULL,
    action                      TEXT NOT NULL,
    verdict                     TEXT NOT NULL,
    block_reasons               TEXT NOT NULL DEFAULT '[]',
    allow_reasons               TEXT NOT NULL DEFAULT '[]',
    reward_risk_ratio           REAL,
    position_size_pct           REAL,
    cooldown_remaining_seconds  INTEGER NOT NULL DEFAULT 0,
    checked_at                  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_symbol_time ON risk_decisions(symbol, checked_at);
CREATE INDEX IF NOT EXISTS idx_risk_verdict_time ON risk_decisions(verdict, checked_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_risk_signal_id ON risk_decisions(signal_id);
"""


class RiskDecisionRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        migrate_database(self.db_path)

    def _ensure_schema(self) -> None:
        with _connect(self.db_path) as conn:
            conn.executescript(_RISK_DECISION_DDL)

    def save(self, decision: RiskDecision) -> RiskDecision:
        with _connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO risk_decisions
                    (decision_id, signal_id, symbol, action, verdict,
                     block_reasons, allow_reasons,
                     reward_risk_ratio, position_size_pct,
                     cooldown_remaining_seconds, checked_at,
                     planned_quantity, planned_notional, daily_loss_pct,
                     drawdown_pct, liquidity_value_20d)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id, decision.signal_id, decision.symbol,
                    decision.action, decision.verdict,
                    json.dumps(decision.block_reasons, ensure_ascii=False),
                    json.dumps(decision.allow_reasons, ensure_ascii=False),
                    decision.reward_risk_ratio, decision.position_size_pct,
                    decision.cooldown_remaining_seconds, decision.checked_at,
                    decision.planned_quantity, decision.planned_notional,
                    decision.daily_loss_pct, decision.drawdown_pct,
                    decision.liquidity_value_20d,
                ),
            )
        return self.get_by_signal_id(decision.signal_id) or decision

    def get_by_signal_id(self, signal_id: str) -> Optional[RiskDecision]:
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM risk_decisions WHERE signal_id = ? LIMIT 1",
                (signal_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_decision(row)

    def get_recent(self, limit: int = 100) -> list[RiskDecision]:
        with _connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM risk_decisions ORDER BY checked_at DESC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
        return [d for row in rows if (d := self._row_to_decision(row)) is not None]

    def count_by_verdict(self) -> dict[str, int]:
        with _connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT verdict, COUNT(*) AS cnt FROM risk_decisions GROUP BY verdict"
            )
            return {row["verdict"]: row["cnt"] for row in cursor.fetchall()}

    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> Optional[RiskDecision]:
        try:
            return RiskDecision(
                decision_id=row["decision_id"],
                signal_id=row["signal_id"],
                symbol=row["symbol"],
                action=row["action"],
                verdict=row["verdict"],
                block_reasons=json.loads(row["block_reasons"] or "[]"),
                allow_reasons=json.loads(row["allow_reasons"] or "[]"),
                reward_risk_ratio=row["reward_risk_ratio"],
                position_size_pct=row["position_size_pct"],
                cooldown_remaining_seconds=row["cooldown_remaining_seconds"] or 0,
                checked_at=row["checked_at"],
                planned_quantity=row["planned_quantity"],
                planned_notional=row["planned_notional"],
                daily_loss_pct=row["daily_loss_pct"],
                drawdown_pct=row["drawdown_pct"],
                liquidity_value_20d=row["liquidity_value_20d"],
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            return None


# ---------------------------------------------------------------------------
# NotificationRepository — chống gửi trùng
# ---------------------------------------------------------------------------

_NOTIFICATION_DDL = """
CREATE TABLE IF NOT EXISTS notification_deliveries (
    notification_id TEXT PRIMARY KEY,
    signal_id       TEXT NOT NULL,
    decision_id     TEXT NOT NULL,
    recipient       TEXT NOT NULL,
    channel         TEXT NOT NULL,
    message_id      TEXT,
    sent_at         TEXT NOT NULL,
    UNIQUE(signal_id, recipient, channel)
);
"""


class NotificationRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        migrate_database(self.db_path)

    def was_sent(self, signal_id: str, recipient: str, channel: str) -> bool:
        with _connect(self.db_path) as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM notification_deliveries "
                    "WHERE signal_id = ? AND recipient = ? AND channel = ? LIMIT 1",
                    (signal_id, recipient, channel),
                ).fetchone()
                is not None
            )

    def record_sent(
        self, *, notification_id: str, signal_id: str, decision_id: str,
        recipient: str, channel: str, message_id: Optional[str], sent_at: datetime,
        outbox_id: str | None = None,
    ) -> None:
        with _connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO notification_deliveries
                    (notification_id, signal_id, decision_id, recipient,
                     channel, message_id, sent_at, outbox_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (notification_id, signal_id, decision_id, recipient,
                 channel, message_id, sent_at.astimezone(timezone.utc).isoformat(),
                 outbox_id),
            )


@dataclass(frozen=True)
class OutboxMessage:
    outbox_id: str
    signal_id: str
    decision_id: str
    recipient: str
    channel: str
    payload: str
    attempts: int


class NotificationOutboxRepository:
    """Outbox SQLite có claim lease để nhiều worker không gửi cùng một bản tin."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        migrate_database(self.db_path)

    def enqueue(
        self,
        *,
        signal_id: str,
        decision_id: str,
        recipient: str,
        payload: str,
        channel: str = "telegram",
        now: datetime | None = None,
    ) -> bool:
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with _connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO notification_outbox (
                    outbox_id, signal_id, decision_id, recipient, channel,
                    payload, status, attempts, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), signal_id, decision_id, recipient, channel,
                    payload, timestamp.isoformat(), timestamp.isoformat(),
                    timestamp.isoformat(),
                ),
            )
            return cursor.rowcount > 0

    def claim_batch(
        self,
        *,
        worker_id: str,
        limit: int = 20,
        lease_seconds: int = 120,
        max_attempts: int = 5,
        now: datetime | None = None,
    ) -> list[OutboxMessage]:
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        expired = timestamp - timedelta(seconds=lease_seconds)
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE notification_outbox
                SET status = 'PENDING', worker_id = NULL, claimed_at = NULL,
                    updated_at = ?
                WHERE status = 'PROCESSING' AND claimed_at < ?
                """,
                (timestamp.isoformat(), expired.isoformat()),
            )
            rows = connection.execute(
                """
                SELECT * FROM notification_outbox
                WHERE status IN ('PENDING', 'FAILED')
                  AND available_at <= ? AND attempts < ?
                ORDER BY created_at
                LIMIT ?
                """,
                (timestamp.isoformat(), max_attempts, max(1, limit)),
            ).fetchall()
            ids = [str(row["outbox_id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"""
                    UPDATE notification_outbox
                    SET status = 'PROCESSING', worker_id = ?, claimed_at = ?,
                        attempts = attempts + 1, updated_at = ?
                    WHERE outbox_id IN ({placeholders})
                    """,
                    (worker_id, timestamp.isoformat(), timestamp.isoformat(), *ids),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return [
            OutboxMessage(
                outbox_id=str(row["outbox_id"]),
                signal_id=str(row["signal_id"]),
                decision_id=str(row["decision_id"]),
                recipient=str(row["recipient"]),
                channel=str(row["channel"]),
                payload=str(row["payload"]),
                attempts=int(row["attempts"]) + 1,
            )
            for row in rows
        ]

    def mark_sent(
        self,
        message: OutboxMessage,
        message_id: str | None,
        *,
        now: datetime | None = None,
    ) -> None:
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with _connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE notification_outbox
                SET status = 'SENT', message_id = ?, worker_id = NULL,
                    claimed_at = NULL, last_error = NULL, updated_at = ?
                WHERE outbox_id = ? AND status = 'PROCESSING'
                """,
                (message_id, timestamp.isoformat(), message.outbox_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO notification_deliveries (
                    notification_id, signal_id, decision_id, recipient,
                    channel, message_id, sent_at, outbox_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), message.signal_id, message.decision_id,
                    message.recipient, message.channel, message_id,
                    timestamp.isoformat(), message.outbox_id,
                ),
            )

    def mark_failed(
        self,
        message: OutboxMessage,
        error: str,
        *,
        now: datetime | None = None,
        max_backoff_seconds: int = 900,
    ) -> None:
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        backoff = min(max_backoff_seconds, 2 ** min(message.attempts, 10))
        with _connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE notification_outbox
                SET status = 'FAILED', available_at = ?, worker_id = NULL,
                    claimed_at = NULL, last_error = ?, updated_at = ?
                WHERE outbox_id = ? AND status = 'PROCESSING'
                """,
                (
                    (timestamp + timedelta(seconds=backoff)).isoformat(),
                    error[:1000], timestamp.isoformat(), message.outbox_id,
                ),
            )

    def count_by_status(self) -> dict[str, int]:
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM notification_outbox GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}
