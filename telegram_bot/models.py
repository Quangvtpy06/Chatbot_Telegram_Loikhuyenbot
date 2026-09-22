"""Các model nội bộ, không phụ thuộc model của Signal Engine đang phát triển."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SystemStatus:
    """Trạng thái dữ liệu analytics gần nhất."""

    report_status: str
    generated_at: str | None
    database_available: bool
    report_available: bool
    eligible_symbols: int = 0
    blocked_symbols: int = 0
    snapshot_symbols: int = 0
    latest_realtime_at: str | None = None
    latest_history_date: str | None = None
    latest_fundamental_period: str | None = None
    latest_fundamental_source: str | None = None
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class SignalView:
    """Kết quả an toàn để handler hiển thị cho một mã cổ phiếu."""

    symbol: str
    action: str
    data_status: str
    signal_status: str
    as_of: str | None = None
    price: float | None = None
    confidence: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    strategy: str = "Chưa xác định"
    reasons: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
