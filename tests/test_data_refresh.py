"""Kiểm tra lịch tự động refresh dữ liệu mà không gọi API thật."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram_bot.config import BotConfig
from telegram_bot.data_refresh import RealtimeRefreshService, VIETNAM_TIMEZONE


class _EmptySubscribers:
    def list_all_watchlist_symbols(self) -> tuple[str, ...]:
        return ()


def _service() -> RealtimeRefreshService:
    runtime_dir = Path("work/test_data_refresh_runtime")
    config = BotConfig(
        token="test-token",
        allowed_chat_ids=frozenset(),
        analytics_database=runtime_dir / "analytics.sqlite",
        quality_report=runtime_dir / "quality_report.json",
        bot_database=runtime_dir / "telegram.sqlite",
        data_dir=runtime_dir / "data",
        analytics_output_dir=runtime_dir / "analytics",
    )
    return RealtimeRefreshService(config, _EmptySubscribers())


def test_dung_stock_snapshot_khi_chua_co_cau_hinh_hoac_watchlist(monkeypatch) -> None:
    service = _service()
    monkeypatch.setattr(service, "_snapshot_symbols", lambda: ("FPT", "HPG"))

    assert service._symbols() == ("FPT", "HPG")


def test_bctc_den_han_theo_state_persisted() -> None:
    service = _service()
    now = datetime.now(timezone.utc)

    service._last_fundamental_at = now - timedelta(hours=23)
    assert service._fundamental_due(now) is False
    service._last_fundamental_at = now - timedelta(hours=25)
    assert service._fundamental_due(now) is True


def test_chi_poll_realtime_trong_gio_giao_dich() -> None:
    monday = datetime(2026, 9, 21, 9, 30, tzinfo=VIETNAM_TIMEZONE)
    lunch = datetime(2026, 9, 21, 12, 0, tzinfo=VIETNAM_TIMEZONE)
    sunday = datetime(2026, 9, 20, 9, 30, tzinfo=VIETNAM_TIMEZONE)

    assert RealtimeRefreshService._is_market_open(monday) is True
    assert RealtimeRefreshService._is_market_open(lunch) is False
    assert RealtimeRefreshService._is_market_open(sunday) is False
