"""Kiểm tra adapter nối SQLite analytics với Signal Engine."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from telegram_bot.analytics_repository import AnalyticsRepository


def test_signal_repository_sinh_buy_tu_du_lieu_day_du() -> None:
    database = Path("work/test_signal_repository.sqlite")
    database.parent.mkdir(parents=True, exist_ok=True)
    database.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                """
                CREATE TABLE stock_snapshot (
                    symbol TEXT PRIMARY KEY,
                    price_as_of TEXT,
                    latest_close REAL,
                    realtime_price REAL,
                    sma_20 REAL,
                    sma_50 REAL,
                    ema_20 REAL,
                    rsi_14 REAL,
                    macd REAL,
                    macd_signal REAL,
                    roe_ttm REAL,
                    roe_quarter REAL,
                    debt_to_equity_quarter REAL,
                    pe_quarter REAL,
                    pb_quarter REAL,
                    trend_state TEXT,
                    indicator_data_complete INTEGER,
                    source_consistent INTEGER,
                    data_status TEXT,
                    signal_status TEXT,
                    quality_reasons TEXT,
                    market_context_symbol TEXT,
                    foreign_net_volume_5d REAL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO stock_snapshot VALUES (
                    'FPT', '2026-09-21', 120, 120, 110, 100, 112, 60, 2, 1,
                    0.20, 0.18, 0.50, 15, 3, 'BULLISH', 1, 1,
                    'OK', 'ELIGIBLE', '', 'VNINDEX', 500
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE market_context (
                    symbol TEXT PRIMARY KEY, as_of TEXT, close REAL, sma_20 REAL
                )
                """
            )
            connection.execute(
                "INSERT INTO market_context VALUES ('VNINDEX', '2026-09-21', 1100, 1000)"
            )
            connection.execute(
                """
                CREATE TABLE foreign_flow (
                    symbol TEXT, trading_date TEXT, foreign_buy_volume REAL,
                    foreign_sell_volume REAL, foreign_net_volume REAL,
                    source TEXT, collected_at TEXT
                )
                """
            )
            connection.executemany(
                "INSERT INTO foreign_flow VALUES ('FPT', ?, 200, 100, 100, 'DNSE', ?)",
                [
                    (f"2026-09-{day:02d}", f"2026-09-{day:02d}T08:00:00+00:00")
                    for day in range(15, 20)
                ],
            )
            connection.commit()

        repository = AnalyticsRepository(
            database, Path("work/nonexistent_quality_report.json")
        )
        signal, event = repository.get_signal_view("FPT")

        assert signal.action == "BUY"
        assert event is not None
        assert event.action == "BUY"
        assert signal.signal_status == "ELIGIBLE"
        assert signal.confidence is not None
        assert signal.stop_loss == 114.0
        assert signal.take_profit == 132.0
        assert signal.strategy.startswith("quality_trend_v1 v")
        assert signal.metrics["foreign_net_volume_5d"] == 500
    finally:
        database.unlink(missing_ok=True)
