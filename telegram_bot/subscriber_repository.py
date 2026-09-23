"""Lưu subscriber và watchlist bằng SQLite cục bộ."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterator

from .config import INVALID_EQUITY_SYMBOLS

try:
    from storage.migrations import migrate_database
except ImportError:
    from dnse.storage.migrations import migrate_database


class SubscriberRepository:
    """Repository nhỏ, đủ dùng cho polling và có thể thay bằng storage chung sau."""

    def __init__(self, database_path: Path, watchlist_limit: int = 50) -> None:
        self.database_path = database_path
        self.watchlist_limit = watchlist_limit
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        migrate_database(self.database_path)
        # Dọn mã nhập nhầm đã lưu từ các phiên bản trước.
        with self._connection() as connection:
            connection.executemany(
                "DELETE FROM watchlists WHERE UPPER(TRIM(symbol)) = ?",
                [(symbol,) for symbol in INVALID_EQUITY_SYMBOLS],
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Đảm bảo commit/rollback và luôn đóng kết nối trên Windows."""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS watchlists (
                    chat_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, symbol),
                    FOREIGN KEY (chat_id) REFERENCES subscribers(chat_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_watchlists_chat
                    ON watchlists(chat_id, symbol);
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def upsert_subscriber(
        self,
        chat_id: int,
        username: str | None,
        first_name: str | None,
    ) -> None:
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO subscribers (
                    chat_id, username, first_name, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    is_active = 1,
                    updated_at = excluded.updated_at
                """,
                (chat_id, username, first_name, now, now),
            )

    def list_watchlist(self, chat_id: int) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT symbol FROM watchlists WHERE chat_id = ? ORDER BY symbol",
                (chat_id,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def list_all_watchlist_symbols(self) -> list[str]:
        """Lấy hợp các mã đang được người dùng theo dõi để refresh realtime."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT w.symbol
                FROM watchlists AS w
                JOIN subscribers AS s ON s.chat_id = w.chat_id
                WHERE s.is_active = 1
                ORDER BY w.symbol
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def list_chat_ids_for_symbol(self, symbol: str) -> list[int]:
        """Lấy người nhận đang hoạt động và theo dõi một mã."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT w.chat_id
                FROM watchlists AS w
                JOIN subscribers AS s ON s.chat_id = w.chat_id
                WHERE UPPER(w.symbol) = ? AND s.is_active = 1
                ORDER BY w.chat_id
                """,
                (symbol.upper(),),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def list_active_chat_ids(self) -> list[int]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT chat_id FROM subscribers WHERE is_active = 1 ORDER BY chat_id"
            ).fetchall()
        return [int(row[0]) for row in rows]

    def add_to_watchlist(self, chat_id: int, symbol: str) -> bool:
        """Thêm mã; trả False nếu mã đã có trong danh sách."""

        symbol = symbol.strip().upper()
        if symbol in INVALID_EQUITY_SYMBOLS:
            raise ValueError(f"{symbol} không phải mã cổ phiếu niêm yết/đăng ký giao dịch.")
        with self._connection() as connection:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM watchlists WHERE chat_id = ?", (chat_id,)
                ).fetchone()[0]
            )
            exists = connection.execute(
                "SELECT 1 FROM watchlists WHERE chat_id = ? AND symbol = ?",
                (chat_id, symbol),
            ).fetchone()
            if exists:
                return False
            if count >= self.watchlist_limit:
                raise ValueError(
                    f"Watchlist đã đạt giới hạn {self.watchlist_limit} mã."
                )
            connection.execute(
                "INSERT INTO watchlists (chat_id, symbol, created_at) VALUES (?, ?, ?)",
                (chat_id, symbol, self._now()),
            )
        return True

    def remove_from_watchlist(self, chat_id: int, symbol: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM watchlists WHERE chat_id = ? AND symbol = ?",
                (chat_id, symbol),
            )
        return cursor.rowcount > 0

    def clear_watchlist(self, chat_id: int) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM watchlists WHERE chat_id = ?", (chat_id,)
            )
        return cursor.rowcount

    def get_user_settings(self, chat_id: int) -> dict[str, Any]:
        """Lấy cài đặt quản trị rủi ro SL/TP và khẩu vị đầu tư của user."""
        defaults = {
            "chat_id": chat_id,
            "sl_tp_mode": "FIXED",
            "fixed_sl_pct": 0.05,
            "fixed_tp_pct": 0.10,
            "investment_mode": "SHORT_TERM",
        }
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT chat_id, sl_tp_mode, fixed_sl_pct, fixed_tp_pct, investment_mode
                FROM user_settings
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
            if row:
                return {
                    "chat_id": row[0],
                    "sl_tp_mode": str(row[1]).upper(),
                    "fixed_sl_pct": float(row[2]),
                    "fixed_tp_pct": float(row[3]),
                    "investment_mode": str(row[4]).upper() if row[4] else "SHORT_TERM",
                }
        return defaults

    def set_sl_tp_mode(self, chat_id: int, mode: str) -> None:
        """Cập nhật nhanh chế độ SL/TP ('FIXED' hoặc 'STRUCTURE')."""
        normalized_mode = mode.strip().upper()
        if normalized_mode not in ("FIXED", "STRUCTURE"):
            raise ValueError(f"Chế độ không hợp lệ: {mode}")

        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO subscribers (chat_id, created_at, updated_at)
                VALUES (?, ?, ?)
                """,
                (chat_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO user_settings (
                    chat_id, sl_tp_mode, fixed_sl_pct, fixed_tp_pct, investment_mode, updated_at
                ) VALUES (?, ?, 0.05, 0.10, 'SHORT_TERM', ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    sl_tp_mode = excluded.sl_tp_mode,
                    updated_at = excluded.updated_at
                """,
                (chat_id, normalized_mode, now),
            )

    def set_investment_mode(self, chat_id: int, mode: str) -> None:
        """Cập nhật nhanh khẩu vị đầu tư ('SHORT_TERM', 'LONG_TERM', 'BOTH')."""
        normalized_mode = mode.strip().upper()
        if normalized_mode not in ("SHORT_TERM", "LONG_TERM", "BOTH"):
            raise ValueError(f"Khẩu vị đầu tư không hợp lệ: {mode}")

        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO subscribers (chat_id, created_at, updated_at)
                VALUES (?, ?, ?)
                """,
                (chat_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO user_settings (
                    chat_id, sl_tp_mode, fixed_sl_pct, fixed_tp_pct, investment_mode, updated_at
                ) VALUES (?, 'FIXED', 0.05, 0.10, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    investment_mode = excluded.investment_mode,
                    updated_at = excluded.updated_at
                """,
                (chat_id, normalized_mode, now),
            )
            connection.execute(
                """
                UPDATE subscribers
                SET investment_mode = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (normalized_mode, now, chat_id),
            )

    def update_user_settings(
        self,
        chat_id: int,
        sl_tp_mode: str | None = None,
        fixed_sl_pct: float | None = None,
        fixed_tp_pct: float | None = None,
        investment_mode: str | None = None,
    ) -> None:
        """Cập nhật chi tiết cài đặt quản trị rủi ro & khẩu vị đầu tư của người dùng."""
        current = self.get_user_settings(chat_id)
        mode = sl_tp_mode.strip().upper() if sl_tp_mode else current["sl_tp_mode"]
        sl = fixed_sl_pct if fixed_sl_pct is not None else current["fixed_sl_pct"]
        tp = fixed_tp_pct if fixed_tp_pct is not None else current["fixed_tp_pct"]
        inv_mode = investment_mode.strip().upper() if investment_mode else current["investment_mode"]
        now = self._now()

        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO subscribers (chat_id, created_at, updated_at)
                VALUES (?, ?, ?)
                """,
                (chat_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO user_settings (
                    chat_id, sl_tp_mode, fixed_sl_pct, fixed_tp_pct, investment_mode, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    sl_tp_mode = excluded.sl_tp_mode,
                    fixed_sl_pct = excluded.fixed_sl_pct,
                    fixed_tp_pct = excluded.fixed_tp_pct,
                    investment_mode = excluded.investment_mode,
                    updated_at = excluded.updated_at
                """,
                (chat_id, mode, sl, tp, inv_mode, now),
            )
            connection.execute(
                """
                UPDATE subscribers
                SET investment_mode = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (inv_mode, now, chat_id),
            )
