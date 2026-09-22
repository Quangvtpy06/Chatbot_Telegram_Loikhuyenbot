"""Notification service độc lập với quyết định quản trị rủi ro."""

from __future__ import annotations

import uuid
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Protocol

try:
    from signals.models import SignalEvent
except ImportError:
    from dnse.signals.models import SignalEvent

try:
    from storage.repositories import (
        NotificationOutboxRepository,
        NotificationRepository,
        OutboxMessage,
        RiskDecision,
    )
except ImportError:
    from dnse.storage.repositories import (
        NotificationOutboxRepository,
        NotificationRepository,
        OutboxMessage,
        RiskDecision,
    )


LOGGER = logging.getLogger("dnse.notifications")


class MessageSender(Protocol):
    """Adapter tối thiểu để Telegram sender thật hoặc fake sender cùng sử dụng."""

    def send(self, recipient: str, message: str) -> str | None: ...


class AsyncMessageSender(Protocol):
    """Adapter sender bất đồng bộ dùng cho Telegram Bot API."""

    def send(self, recipient: str, message: str) -> Awaitable[str | None]: ...


@dataclass(frozen=True)
class NotificationResult:
    delivered: bool
    reason: str
    message_id: str | None = None


class NotificationService:
    """Chỉ gửi quyết định ALLOW và chống gửi trùng theo người nhận/kênh."""

    def __init__(
        self,
        repository: NotificationRepository,
        sender: MessageSender,
        renderer: Callable[[SignalEvent, RiskDecision], str] | None = None,
        channel: str = "telegram",
    ) -> None:
        self.repository = repository
        self.sender = sender
        self.renderer = renderer or self._default_renderer
        self.channel = channel

    def dispatch(
        self,
        signal: SignalEvent,
        decision: RiskDecision,
        recipient: str,
    ) -> NotificationResult:
        if decision.signal_id != signal.signal_id:
            return NotificationResult(False, "decision không thuộc signal")
        if decision.verdict != "ALLOW":
            return NotificationResult(False, "Risk Gate không ALLOW")
        if self.repository.was_sent(signal.signal_id, recipient, self.channel):
            return NotificationResult(False, "Thông báo đã gửi trước đó")

        message_id = self.sender.send(recipient, self.renderer(signal, decision))
        self.repository.record_sent(
            notification_id=str(uuid.uuid4()),
            signal_id=signal.signal_id,
            decision_id=decision.decision_id,
            recipient=recipient,
            channel=self.channel,
            message_id=message_id,
            sent_at=datetime.now(timezone.utc),
        )
        return NotificationResult(True, "Đã gửi", message_id)

    @staticmethod
    def _default_renderer(signal: SignalEvent, decision: RiskDecision) -> str:
        return (
            f"{signal.action} {signal.symbol} @ {signal.reference_price:,.2f} | "
            f"SL={signal.stop_loss} TP={signal.take_profit} "
            f"R:R={decision.reward_risk_ratio}"
        )


class OutboxNotificationService:
    """Xếp hàng và gửi notification theo outbox có claim lease."""

    def __init__(
        self,
        repository: NotificationOutboxRepository,
        sender: AsyncMessageSender,
        renderer: Callable[[SignalEvent, RiskDecision], str] | None = None,
        *,
        channel: str = "telegram",
        worker_id: str | None = None,
        max_attempts: int = 5,
    ) -> None:
        self.repository = repository
        self.sender = sender
        self.renderer = renderer or NotificationService._default_renderer
        self.channel = channel
        self.worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self.max_attempts = max_attempts

    def enqueue(
        self,
        signal: SignalEvent,
        decision: RiskDecision,
        recipient: str,
    ) -> NotificationResult:
        if decision.signal_id != signal.signal_id:
            return NotificationResult(False, "decision không thuộc signal")
        if decision.verdict != "ALLOW":
            return NotificationResult(False, "Risk Gate không ALLOW")
        inserted = self.repository.enqueue(
            signal_id=signal.signal_id,
            decision_id=decision.decision_id,
            recipient=recipient,
            channel=self.channel,
            payload=self.renderer(signal, decision),
        )
        return NotificationResult(
            inserted,
            "Đã xếp hàng" if inserted else "Thông báo đã tồn tại trong outbox",
        )

    async def drain(self, *, limit: int = 20) -> list[NotificationResult]:
        messages = self.repository.claim_batch(
            worker_id=self.worker_id,
            limit=limit,
            max_attempts=self.max_attempts,
        )
        results: list[NotificationResult] = []
        for message in messages:
            results.append(await self._deliver(message))
        return results

    async def _deliver(self, message: OutboxMessage) -> NotificationResult:
        try:
            message_id = await self.sender.send(message.recipient, message.payload)
        except Exception as exc:
            self.repository.mark_failed(message, f"{type(exc).__name__}: {exc}")
            LOGGER.exception(
                "Gửi notification thất bại: outbox=%s recipient=%s",
                message.outbox_id,
                message.recipient,
            )
            return NotificationResult(False, f"Gửi thất bại: {type(exc).__name__}")
        self.repository.mark_sent(message, message_id)
        return NotificationResult(True, "Đã gửi", message_id)
