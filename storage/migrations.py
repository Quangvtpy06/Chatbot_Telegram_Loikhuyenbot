"""Migration SQLite có version cho bot, Risk Gate và danh mục mô phỏng."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


LATEST_SCHEMA_VERSION = 6


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _add_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    if column not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migration_1(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS signal_cooldowns (
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            strategy TEXT NOT NULL,
            last_allowed_at TEXT NOT NULL,
            PRIMARY KEY (symbol, action, strategy)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            sector TEXT,
            status TEXT NOT NULL DEFAULT 'OPEN',
            entry_price REAL NOT NULL,
            quantity REAL NOT NULL DEFAULT 0,
            nav_pct REAL NOT NULL DEFAULT 0,
            opened_at TEXT NOT NULL,
            closed_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_positions_symbol_status ON positions(symbol, status)",
        "CREATE INDEX IF NOT EXISTS idx_positions_sector_status ON positions(sector, status)",
        """
        CREATE TABLE IF NOT EXISTS risk_decisions (
            decision_id TEXT PRIMARY KEY,
            signal_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            verdict TEXT NOT NULL,
            block_reasons TEXT NOT NULL DEFAULT '[]',
            allow_reasons TEXT NOT NULL DEFAULT '[]',
            reward_risk_ratio REAL,
            position_size_pct REAL,
            cooldown_remaining_seconds INTEGER NOT NULL DEFAULT 0,
            checked_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_risk_symbol_time ON risk_decisions(symbol, checked_at)",
        "CREATE INDEX IF NOT EXISTS idx_risk_verdict_time ON risk_decisions(verdict, checked_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_risk_signal_id ON risk_decisions(signal_id)",
        """
        CREATE TABLE IF NOT EXISTS notification_deliveries (
            notification_id TEXT PRIMARY KEY,
            signal_id TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            recipient TEXT NOT NULL,
            channel TEXT NOT NULL,
            message_id TEXT,
            sent_at TEXT NOT NULL,
            UNIQUE(signal_id, recipient, channel)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS watchlists (
            chat_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, symbol),
            FOREIGN KEY (chat_id) REFERENCES subscribers(chat_id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_watchlists_chat ON watchlists(chat_id, symbol)",
    )
    for statement in statements:
        connection.execute(statement)


def _migration_2(connection: sqlite3.Connection) -> None:
    for name, definition in (
        ("last_price", "REAL"),
        ("price_unit_vnd", "REAL NOT NULL DEFAULT 1000"),
        ("market_value", "REAL"),
        ("unrealized_pnl", "REAL"),
        ("realized_pnl", "REAL NOT NULL DEFAULT 0"),
        ("closed_price", "REAL"),
        ("signal_id", "TEXT"),
    ):
        _add_column(connection, "positions", name, definition)

    for name, definition in (
        ("planned_quantity", "INTEGER"),
        ("planned_notional", "REAL"),
        ("daily_loss_pct", "REAL"),
        ("drawdown_pct", "REAL"),
        ("liquidity_value_20d", "REAL"),
    ):
        _add_column(connection, "risk_decisions", name, definition)

    statements = (
        """
        CREATE TABLE IF NOT EXISTS portfolio_state (
            portfolio_id INTEGER PRIMARY KEY CHECK (portfolio_id = 1),
            initial_nav REAL NOT NULL,
            cash_balance REAL NOT NULL,
            current_nav REAL NOT NULL,
            peak_nav REAL NOT NULL,
            realized_pnl REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            trading_date TEXT PRIMARY KEY,
            opening_nav REAL NOT NULL,
            closing_nav REAL NOT NULL,
            peak_nav REAL NOT NULL,
            daily_pnl REAL NOT NULL,
            daily_pnl_pct REAL NOT NULL,
            drawdown_pct REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS position_events (
            event_id TEXT PRIMARY KEY,
            signal_id TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            price_unit_vnd REAL NOT NULL DEFAULT 1000,
            notional REAL NOT NULL,
            realized_pnl REAL NOT NULL DEFAULT 0,
            executed_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_position_events_symbol_time ON position_events(symbol, executed_at)",
    )
    for statement in statements:
        connection.execute(statement)

    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT OR IGNORE INTO portfolio_state (
            portfolio_id, initial_nav, cash_balance, current_nav,
            peak_nav, realized_pnl, updated_at
        ) VALUES (1, 1000000000, 1000000000, 1000000000, 1000000000, 0, ?)
        """,
        (now,),
    )


def _migration_3(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS notification_outbox (
            outbox_id TEXT PRIMARY KEY,
            signal_id TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            recipient TEXT NOT NULL,
            channel TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            claimed_at TEXT,
            worker_id TEXT,
            last_error TEXT,
            message_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(signal_id, recipient, channel)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_outbox_claim ON notification_outbox(status, available_at, attempts)",
        "CREATE INDEX IF NOT EXISTS idx_outbox_worker ON notification_outbox(worker_id, status)",
    )
    for statement in statements:
        connection.execute(statement)


def _migration_4(connection: sqlite3.Connection) -> None:
    _add_column(connection, "notification_deliveries", "outbox_id", "TEXT")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_outbox ON notification_deliveries(outbox_id)"
    )


def _migration_5(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            chat_id INTEGER PRIMARY KEY,
            sl_tp_mode TEXT NOT NULL DEFAULT 'FIXED',
            fixed_sl_pct REAL NOT NULL DEFAULT 0.05,
            fixed_tp_pct REAL NOT NULL DEFAULT 0.10,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (chat_id) REFERENCES subscribers(chat_id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_user_settings_chat ON user_settings(chat_id)",
    )
    for statement in statements:
        connection.execute(statement)


def _migration_6(connection: sqlite3.Connection) -> None:
    _add_column(
        connection,
        "user_settings",
        "investment_mode",
        "TEXT NOT NULL DEFAULT 'SHORT_TERM'",
    )
    _add_column(
        connection,
        "subscribers",
        "investment_mode",
        "TEXT NOT NULL DEFAULT 'SHORT_TERM'",
    )


MIGRATIONS = {
    1: _migration_1,
    2: _migration_2,
    3: _migration_3,
    4: _migration_4,
    5: _migration_5,
    6: _migration_6,
}


def migrate_database(database_path: Path) -> int:
    """Nâng schema tuần tự và trả version hiện tại."""

    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = {
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        for version in range(1, LATEST_SCHEMA_VERSION + 1):
            if version in applied:
                continue
            connection.execute("BEGIN IMMEDIATE")
            try:
                MIGRATIONS[version](connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, datetime.now(timezone.utc).isoformat()),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    return LATEST_SCHEMA_VERSION


def current_schema_version(database_path: Path) -> int:
    path = Path(database_path)
    if not path.is_file():
        return 0
    with closing(sqlite3.connect(path, timeout=10)) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if table is None:
            return 0
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
    return int(row[0])
