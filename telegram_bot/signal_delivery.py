"""Sinh tín hiệu chủ động, chạy Risk Gate, cập nhật portfolio và gửi Telegram."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from telegram import Bot
from telegram.constants import ParseMode

try:
    from notifications.service import OutboxNotificationService
    from signals.risk_manager import RiskGate
    from storage.repositories import PositionRepository
except ImportError:
    from dnse.notifications.service import OutboxNotificationService
    from dnse.signals.risk_manager import RiskGate
    from dnse.storage.repositories import PositionRepository

from .analytics_repository import AnalyticsRepository
from .subscriber_repository import SubscriberRepository


LOGGER = logging.getLogger("dnse.telegram.proactive")


class TelegramAsyncSender:
    """Adapter gửi HTML qua python-telegram-bot."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, recipient: str, message: str) -> str | None:
        result = await self.bot.send_message(
            chat_id=int(recipient),
            text=message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return str(result.message_id)


class ProactiveSignalService:
    """Điều phối pipeline chủ động; chống chạy chồng trong cùng process."""

    def __init__(
        self,
        analytics: AnalyticsRepository,
        risk_gate: RiskGate,
        positions: PositionRepository | None = None,
        subscribers: SubscriberRepository | None = None,
        notifications: OutboxNotificationService | None = None,
        *,
        notify_actions: Iterable[str] = ("BUY", "SELL"),
        board_lot_size: int = 100,
        outbox_batch_size: int = 50,
    ) -> None:
        self.analytics = analytics
        self.risk_gate = risk_gate
        self.positions = positions
        self.subscribers = subscribers
        self.notifications = notifications
        self.notify_actions = frozenset(action.upper() for action in notify_actions)
        self.board_lot_size = board_lot_size
        self.outbox_batch_size = max(1, outbox_batch_size)
        self._lock = asyncio.Lock()

    async def process_symbols(self, symbols: Iterable[str]) -> dict[str, int]:
        if self._lock.locked():
            return {"evaluated": 0, "queued": 0, "sent": 0, "skipped": 1}
        async with self._lock:
            evaluated = queued = 0
            for symbol in tuple(dict.fromkeys(value.upper() for value in symbols)):
                try:
                    _, signal = await asyncio.to_thread(
                        self.analytics.get_signal_view, symbol
                    )
                    if signal is None:
                        continue
                    evaluated += 1
                    if self.positions is not None and signal.reference_price is not None:
                        unit = float(signal.metadata.get("price_unit_vnd") or 1000.0)
                        await asyncio.to_thread(
                            self.positions.mark_to_market,
                            {signal.symbol: (signal.reference_price, unit)},
                        )
                    decision = await asyncio.to_thread(self.risk_gate.evaluate, signal)
                    if (
                        decision.verdict != "ALLOW"
                        or signal.action not in self.notify_actions
                    ):
                        continue
                    if self.subscribers is not None and self.notifications is not None:
                        recipients = await asyncio.to_thread(
                            self.subscribers.list_chat_ids_for_symbol, signal.symbol
                        )
                        if not recipients:
                            continue
                        for chat_id in recipients:
                            user_cfg = {}
                            if hasattr(self.subscribers, "get_user_settings"):
                                user_cfg = await asyncio.to_thread(
                                    self.subscribers.get_user_settings, int(chat_id)
                                )
                            user_inv_mode = user_cfg.get("investment_mode", "SHORT_TERM")
                            sig_inv_mode = signal.metadata.get("investment_mode", "SHORT_TERM")
                            if user_inv_mode == "LONG_TERM" and sig_inv_mode == "SHORT_TERM":
                                continue  # Lọc bỏ tiếng ồn kỹ thuật ngắn hạn cho nhà đầu tư dài hạn
                            if user_inv_mode == "SHORT_TERM" and sig_inv_mode == "LONG_TERM":
                                continue  # Lọc bỏ tín hiệu cơ bản dài hạn cho nhà đầu tư lướt sóng thuần kỹ thuật

                            result = self.notifications.enqueue(
                                signal, decision, str(chat_id)
                            )
                            queued += int(result.delivered)
                    if self.positions is not None:
                        await asyncio.to_thread(
                            self.positions.apply_allowed_signal,
                            signal,
                            decision,
                            board_lot_size=self.board_lot_size,
                        )
                except Exception:
                    LOGGER.exception("Pipeline tín hiệu chủ động lỗi cho %s", symbol)

            if self.notifications is not None:
                delivered = await self.notifications.drain(
                    limit=max(self.outbox_batch_size, queued)
                )
                sent = sum(result.delivered for result in delivered)
            else:
                sent = 0
            return {
                "evaluated": evaluated,
                "queued": queued,
                "sent": sent,
                "skipped": 0,
            }
