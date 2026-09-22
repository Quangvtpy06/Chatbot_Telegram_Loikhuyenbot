"""Unit và integration test cho Signal Engine → Risk Gate → Notification."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from notifications.service import NotificationService
from signals.models import SignalEvent
from signals.risk_manager import RiskConfig, create_risk_gate
from signals.signal_engine import create_signal_engine
from storage.repositories import (
    NotificationRepository,
    PositionRepository,
    RiskDecisionRepository,
)


class _FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, recipient: str, message: str) -> str:
        self.sent.append((recipient, message))
        return f"message-{len(self.sent)}"


@pytest.fixture
def risk_db() -> Path:
    path = Path("work") / f"risk_pipeline_{uuid.uuid4().hex}.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    yield path
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _buy_signal() -> SignalEvent:
    now = datetime.now(timezone.utc)
    snapshot = {
        "symbol": "FPT",
        "data_status": "OK",
        "signal_status": "ELIGIBLE",
        "latest_close": 120.0,
        "realtime_price": 120.0,
        "realtime_as_of": (now - timedelta(seconds=5)).isoformat(),
        "sma_20": 110.0,
        "sma_50": 100.0,
        "rsi_14": 60.0,
        "macd": 2.0,
        "macd_signal": 1.0,
        "roe_ttm": 0.20,
        "debt_to_equity_quarter": 0.50,
        "indicator_data_complete": True,
        "source_consistent": True,
        "average_volume_20d": 2_000_000.0,
        "market_id": "STO",
        "previous_close": 119.0,
        "price_unit_vnd": 1000.0,
    }
    return create_signal_engine().generate(
        snapshot,
        market={"symbol": "VNINDEX", "close": 1_100.0, "sma_20": 1_000.0},
        foreign_sessions=[
            {"net_volume": 100.0, "buy_volume": 200.0, "sell_volume": 100.0}
            for _ in range(5)
        ],
        sector="Technology",
    )


def test_signal_engine_risk_notification_end_to_end(risk_db: Path) -> None:
    signal = _buy_signal()
    assert signal.action == "BUY", signal.reasons

    gate = create_risk_gate(risk_db, RiskConfig(cooldown_seconds=0))
    decision = gate.evaluate(signal)
    assert decision.verdict == "ALLOW", decision.block_reasons
    assert decision.position_size_pct == pytest.approx(0.10)

    sender = _FakeSender()
    service = NotificationService(NotificationRepository(risk_db), sender)
    first = service.dispatch(signal, decision, "chat-1")
    duplicate = service.dispatch(signal, decision, "chat-1")

    assert first.delivered is True
    assert duplicate.delivered is False
    assert len(sender.sent) == 1


def test_quality_and_timestamp_gate(risk_db: Path) -> None:
    gate = create_risk_gate(risk_db, RiskConfig(cooldown_seconds=0))
    signal = _buy_signal()
    warning = replace(
        signal,
        signal_id=str(uuid.uuid4()),
        data_status="DATA WARNING",
    )
    stale = replace(
        signal,
        signal_id=str(uuid.uuid4()),
        data_as_of=(datetime.now(timezone.utc) - timedelta(seconds=901)).isoformat(),
    )

    assert gate.evaluate(warning).verdict == "BLOCK"
    assert gate.evaluate(stale).verdict == "BLOCK"


def test_position_state_for_buy_hold_sell(risk_db: Path) -> None:
    gate = create_risk_gate(risk_db, RiskConfig(cooldown_seconds=0))
    positions = PositionRepository(risk_db)
    positions.add_position(
        "FPT", entry_price=100.0, nav_pct=0.05, sector="Technology"
    )
    buy = _buy_signal()
    assert gate.evaluate(buy).verdict == "BLOCK"

    now = datetime.now(timezone.utc)
    common = {
        "strategy": "quality_trend_v1",
        "strategy_version": "1.6.0",
        "confidence": None,
        "reference_price": 120.0,
        "stop_loss": None,
        "take_profit": None,
        "reasons": ["Kiểm tra trạng thái vị thế"],
        "data_as_of": (now - timedelta(seconds=5)).isoformat(),
        "generated_at": now.isoformat(),
        "signal_status": "ELIGIBLE",
        "data_status": "OK",
        "sector": "Technology",
    }
    hold = SignalEvent(
        signal_id=str(uuid.uuid4()), symbol="FPT", action="HOLD", **common
    )
    sell = SignalEvent(
        signal_id=str(uuid.uuid4()), symbol="FPT", action="SELL", **common
    )

    assert gate.evaluate(hold).verdict == "ALLOW"
    assert gate.evaluate(sell).verdict == "ALLOW"


def test_signal_id_idempotent_and_single_audit(risk_db: Path) -> None:
    gate = create_risk_gate(risk_db, RiskConfig(cooldown_seconds=0))
    signal = _buy_signal()

    first = gate.evaluate(signal)
    second = gate.evaluate(signal)

    assert first.decision_id == second.decision_id
    assert len(RiskDecisionRepository(risk_db).get_recent()) == 1
