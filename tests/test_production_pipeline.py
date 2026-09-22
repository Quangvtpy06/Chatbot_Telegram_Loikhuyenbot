"""Kiểm thử các cổng production ngoài phạm vi backtest."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from notifications.service import OutboxNotificationService
from signals.models import SignalEvent
from signals.risk_manager import RiskConfig, create_risk_gate
from storage.migrations import LATEST_SCHEMA_VERSION, current_schema_version, migrate_database
from storage.repositories import (
    NotificationOutboxRepository,
    PortfolioExecution,
    PositionRepository,
    RiskDecision,
)
from telegram_bot.formatter import format_proactive_signal
from telegram_bot.formatter import format_system_status
from telegram_bot.models import SystemStatus
from telegram_bot.bot import create_application
from telegram_bot.config import BotConfig
from telegram_bot.signal_delivery import ProactiveSignalService


def _database() -> Path:
    path = Path("work") / f"production_pipeline_{uuid.uuid4().hex}.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _signal(
        symbol: str = "FPT",
        *,
        price: float = 120.0,
        signal_id: str | None = None,
        average_volume: float = 2_000_000.0,
        previous_close: float = 119.0,
) -> SignalEvent:
    now = datetime.now(timezone.utc)
    return SignalEvent(
        signal_id=signal_id or str(uuid.uuid4()),
        symbol=symbol,
        strategy="quality_trend_v1",
        strategy_version="1.6.0",
        action="BUY",
        confidence=80.0,
        reference_price=price,
        stop_loss=price * 0.95,
        take_profit=price * 1.10,
        reasons=["Test production gate"],
        data_as_of=(now - timedelta(seconds=5)).isoformat(),
        generated_at=(now - timedelta(seconds=4)).isoformat(),
        signal_status="ELIGIBLE",
        data_status="OK",
        sector="Technology",
        metadata={
            "exchange": "HOSE",
            "price_unit_vnd": 1000.0,
            "board_lot_size": 100,
            "average_volume_20d": average_volume,
            "average_trading_value_20d": average_volume * price * 1000.0,
            "previous_close": previous_close,
        },
    )


def _allowed_decision(signal: SignalEvent, size: float = 0.10) -> RiskDecision:
    return RiskDecision(
        decision_id=str(uuid.uuid4()),
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        action=signal.action,
        verdict="ALLOW",
        allow_reasons=["Test"],
        checked_at=datetime.now(timezone.utc).isoformat(),
        position_size_pct=size,
        planned_quantity=800,
    )


def test_migration_upgrades_legacy_database() -> None:
    path = _database()
    try:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                """
                CREATE TABLE positions
                (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol      TEXT NOT NULL,
                    sector      TEXT,
                    status      TEXT NOT NULL DEFAULT 'OPEN',
                    entry_price REAL NOT NULL,
                    quantity    REAL NOT NULL DEFAULT 0,
                    nav_pct     REAL NOT NULL DEFAULT 0,
                    opened_at   TEXT NOT NULL,
                    closed_at   TEXT
                )
                """
            )
            connection.commit()
        assert migrate_database(path) == LATEST_SCHEMA_VERSION
        assert current_schema_version(path) == LATEST_SCHEMA_VERSION
        with closing(sqlite3.connect(path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            position_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(positions)")
            }
        assert {"notification_outbox", "portfolio_state", "position_events"} <= tables
        assert {"last_price", "market_value", "signal_id"} <= position_columns
    finally:
        _cleanup(path)


def test_outbox_claim_is_atomic_and_delivery_is_idempotent() -> None:
    path = _database()
    try:
        repository = NotificationOutboxRepository(path)
        assert repository.enqueue(
            signal_id="sig-1",
            decision_id="dec-1",
            recipient="1001",
            payload="BUY FPT",
        )
        assert not repository.enqueue(
            signal_id="sig-1",
            decision_id="dec-1",
            recipient="1001",
            payload="BUY FPT",
        )
        first = repository.claim_batch(worker_id="worker-1")
        second = repository.claim_batch(worker_id="worker-2")
        assert len(first) == 1
        assert second == []
        repository.mark_sent(first[0], "message-1")
        assert repository.count_by_status() == {"SENT": 1}

        base = datetime.now(timezone.utc)
        assert repository.enqueue(
            signal_id="sig-2",
            decision_id="dec-2",
            recipient="1001",
            payload="SELL FPT",
            now=base,
        )
        failed = repository.claim_batch(worker_id="worker-1", now=base)
        assert len(failed) == 1
        repository.mark_failed(failed[0], "Telegram timeout", now=base)
        assert repository.claim_batch(
            worker_id="worker-2", now=base + timedelta(seconds=1)
        ) == []
        retry = repository.claim_batch(
            worker_id="worker-2", now=base + timedelta(seconds=3)
        )
        assert len(retry) == 1
        repository.mark_sent(retry[0], "message-2", now=base + timedelta(seconds=3))
        assert repository.count_by_status() == {"SENT": 2}
    finally:
        _cleanup(path)


def test_portfolio_daily_loss_blocks_new_buy() -> None:
    path = _database()
    try:
        positions = PositionRepository(path, initial_nav=1_000_000_000.0)
        fpt = _signal()
        execution: PortfolioExecution = positions.apply_allowed_signal(
            fpt, _allowed_decision(fpt), board_lot_size=100
        )
        assert execution.applied
        positions.mark_to_market({"FPT": (50.0, 1000.0)})
        metrics = positions.get_portfolio_metrics()
        assert metrics.daily_pnl_pct < -0.03

        gate = create_risk_gate(
            path,
            RiskConfig(cooldown_seconds=0),
            initial_nav=1_000_000_000.0,
        )
        decision = gate.evaluate(_signal("HPG", price=25.0, previous_close=24.5))
        assert decision.verdict == "BLOCK"
        assert any("Gate6" in reason for reason in decision.block_reasons)
    finally:
        _cleanup(path)


def test_liquidity_tick_ceiling_and_board_lot_gates() -> None:
    cases = (
        (_signal(average_volume=1_000.0), "GTGD bình quân"),
        (_signal(price=120.03), "bước giá"),
        (_signal(price=127.3, previous_close=119.0), "giá trần"),
    )
    for signal, expected in cases:
        path = _database()
        try:
            gate = create_risk_gate(path, RiskConfig(cooldown_seconds=0))
            decision = gate.evaluate(signal)
            assert decision.verdict == "BLOCK"
            assert any(expected.lower() in reason.lower() for reason in decision.block_reasons)
        finally:
            _cleanup(path)

    path = _database()
    try:
        gate = create_risk_gate(
            path,
            RiskConfig(cooldown_seconds=0),
            initial_nav=5_000_000.0,
        )
        decision = gate.evaluate(_signal())
        assert decision.verdict == "BLOCK"
        assert any("một lô" in reason for reason in decision.block_reasons)
    finally:
        _cleanup(path)


class _FakeAsyncSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send(self, recipient: str, message: str) -> str:
        self.messages.append((recipient, message))
        return f"msg-{len(self.messages)}"


class _FakeAnalytics:
    def __init__(self, signal: SignalEvent) -> None:
        self.signal = signal

    def get_signal_view(self, symbol: str):
        return None, self.signal


class _FakeSubscribers:
    def list_chat_ids_for_symbol(self, symbol: str) -> list[int]:
        return [1001, 1002]


def test_proactive_pipeline_sends_once_and_updates_simulated_position() -> None:
    path = _database()
    try:
        signal = _signal(signal_id="stable-signal")
        positions = PositionRepository(path)
        gate = create_risk_gate(path, RiskConfig(cooldown_seconds=0))
        sender = _FakeAsyncSender()
        notifications = OutboxNotificationService(
            NotificationOutboxRepository(path),
            sender,
            renderer=format_proactive_signal,
            worker_id="test-worker",
        )
        service = ProactiveSignalService(
            analytics=_FakeAnalytics(signal),  # type: ignore[arg-type]
            risk_gate=gate,
            positions=positions,
            subscribers=_FakeSubscribers(),  # type: ignore[arg-type]
            notifications=notifications,
        )

        async def _run_twice():
            return (
                await service.process_symbols(["FPT"]),
                await service.process_symbols(["FPT"]),
            )

        first, second = asyncio.run(_run_twice())

        assert first == {"evaluated": 1, "queued": 2, "sent": 2, "skipped": 0}
        assert second == {"evaluated": 1, "queued": 0, "sent": 0, "skipped": 0}
        assert len(sender.messages) == 2
        assert positions.has_open_position("FPT")
        assert len(positions.list_open_positions()) == 1
    finally:
        _cleanup(path)


def test_bot_application_wires_proactive_pipeline_and_portfolio_command() -> None:
    path = _database()
    runtime = path.parent / f"runtime_{uuid.uuid4().hex}"
    try:
        config = BotConfig(
            token="123456:TESTTOKEN",
            allowed_chat_ids=frozenset({1001}),
            analytics_database=runtime / "analytics.sqlite",
            quality_report=runtime / "quality.json",
            bot_database=path,
            data_dir=runtime / "data",
            analytics_output_dir=runtime / "analytics",
        )
        application = create_application(config)
        handler_count = sum(len(group) for group in application.handlers.values())
        assert handler_count == 15
        assert "proactive_signal_service" in application.bot_data
        assert "realtime_refresher" in application.bot_data
    finally:
        _cleanup(path)


def test_system_status_formatter_reports_live_data_feed() -> None:
    message = format_system_status(
        SystemStatus(
            report_status="PASS",
            generated_at="2026-09-22T07:45:20+00:00",
            database_available=True,
            report_available=True,
            eligible_symbols=3,
            blocked_symbols=1,
            snapshot_symbols=4,
            latest_realtime_at="2026-09-22T14:45:15+07:00",
            latest_history_date="2026-09-21",
            latest_fundamental_period="2026-Q2",
            latest_fundamental_source="KBS",
        ),
        uptime_seconds=5 * 86_400 + 12 * 3_600,
        ping_ms=45,
    )

    assert "Uptime: 5d 12h 0m" in message
    assert "45ms (Rất mượt)" in message
    assert "22/09/2026 14:45:15" in message
    assert "2026-Q2 (KBS)" in message
    assert "Sẵn sàng nhận lệnh" in message
