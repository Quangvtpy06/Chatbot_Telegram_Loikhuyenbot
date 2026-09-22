"""Mô hình dữ liệu dùng chung cho Signal Engine và Risk Gate."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Sequence


SignalAction = Literal["BUY", "SELL", "HOLD", "NO SIGNAL"]
ALLOWED_ACTIONS = {"BUY", "SELL", "HOLD", "NO SIGNAL"}


def _finite_number(value: Any) -> float | None:
    """Đổi giá trị thành số hữu hạn; trả None với null, NaN hoặc chuỗi rỗng."""

    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class MarketContext:
    """Trạng thái VN-Index/VN30 tại thời điểm chạy chiến lược."""

    close: float
    sma_20: float
    symbol: str = "VNINDEX"
    as_of: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.close) or self.close <= 0:
            raise ValueError("market.close phải là số dương hữu hạn")
        if not math.isfinite(self.sma_20) or self.sma_20 <= 0:
            raise ValueError("market.sma_20 phải là số dương hữu hạn")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MarketContext":
        close = _finite_number(
            value.get("close", value.get("market_close", value.get("index_close")))
        )
        sma_20 = _finite_number(
            value.get("sma_20", value.get("market_sma_20", value.get("index_sma_20")))
        )
        if close is None or sma_20 is None:
            raise ValueError("MarketContext cần close và sma_20")
        return cls(
            close=close,
            sma_20=sma_20,
            symbol=str(value.get("symbol") or value.get("market_symbol") or "VNINDEX"),
            as_of=str(value["as_of"]) if value.get("as_of") else None,
        )


@dataclass(frozen=True)
class ForeignFlowSession:
    """Dòng tiền khối ngoại của một phiên giao dịch."""

    net_volume: float
    as_of: str | None = None
    buy_volume: float | None = None
    sell_volume: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.net_volume):
            raise ValueError("foreign net_volume phải là số hữu hạn")
        for name, value in (
            ("buy_volume", self.buy_volume),
            ("sell_volume", self.sell_volume),
        ):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"foreign {name} phải là số không âm hữu hạn")

    @classmethod
    def from_value(
        cls, value: "ForeignFlowSession | Mapping[str, Any] | float | int"
    ) -> "ForeignFlowSession":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            buy = _finite_number(
                value.get("foreign_buy_volume", value.get("buy_volume"))
            )
            sell = _finite_number(
                value.get("foreign_sell_volume", value.get("sell_volume"))
            )
            net = _finite_number(
                value.get("foreign_net_volume", value.get("net_volume"))
            )
            if net is None and buy is not None and sell is not None:
                net = buy - sell
            if net is None:
                raise ValueError("Phiên khối ngoại cần net_volume hoặc buy/sell volume")
            return cls(
                net_volume=net,
                buy_volume=buy,
                sell_volume=sell,
                as_of=str(
                    value.get("as_of")
                    or value.get("timestamp")
                    or value.get("collected_at")
                    or ""
                )
                or None,
            )
        number = _finite_number(value)
        if number is None:
            raise ValueError("Dòng tiền khối ngoại không hợp lệ")
        return cls(net_volume=number)


@dataclass(frozen=True)
class ForeignFlowSummary:
    """Tổng hợp đúng năm phiên gần nhất dùng trong chiến lược."""

    session_count: int
    net_volume: float
    net_buy_sessions: int
    net_sell_sessions: int
    total_buy_volume: float | None = None
    total_sell_volume: float | None = None

    @classmethod
    def from_sessions(
        cls, sessions: Sequence[ForeignFlowSession], window: int = 5
    ) -> "ForeignFlowSummary":
        selected = tuple(sessions[-window:])
        total_buy = (
            sum(session.buy_volume or 0.0 for session in selected)
            if selected and all(session.buy_volume is not None for session in selected)
            else None
        )
        total_sell = (
            sum(session.sell_volume or 0.0 for session in selected)
            if selected and all(session.sell_volume is not None for session in selected)
            else None
        )
        return cls(
            session_count=len(selected),
            net_volume=sum(session.net_volume for session in selected),
            net_buy_sessions=sum(session.net_volume > 0 for session in selected),
            net_sell_sessions=sum(session.net_volume < 0 for session in selected),
            total_buy_volume=total_buy,
            total_sell_volume=total_sell,
        )


@dataclass(frozen=True)
class PositionContext:
    """Thông tin vị thế cần thiết để sinh HOLD hoặc SELL."""

    has_position: bool = False
    entry_price: float | None = None

    def __post_init__(self) -> None:
        if self.has_position and (
            self.entry_price is None
            or not math.isfinite(self.entry_price)
            or self.entry_price <= 0
        ):
            raise ValueError("Vị thế đang mở phải có entry_price dương hữu hạn")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PositionContext":
        raw_has_position = value.get("has_position", value.get("is_open", False))
        if isinstance(raw_has_position, str):
            has_position = raw_has_position.strip().lower() in {"1", "true", "yes", "open"}
        else:
            has_position = bool(raw_has_position)
        return cls(
            has_position=has_position,
            entry_price=_finite_number(
                value.get("entry_price", value.get("position_reference_price"))
            ),
        )


@dataclass(frozen=True)
class SignalRequest:
    """Đầu vào đã tách context cho một lần đánh giá chiến lược."""

    snapshot: Mapping[str, Any]
    market: MarketContext | None = None
    foreign_sessions: tuple[ForeignFlowSession, ...] = ()
    position: PositionContext = field(default_factory=PositionContext)
    session_low: float | None = None
    session_high: float | None = None
    sector: str | None = None


@dataclass(frozen=True)
class SignalEvent:
    """Đầu ra chuẩn của Signal Engine và đầu vào của Risk Gate."""

    signal_id: str
    symbol: str
    strategy: str
    strategy_version: str
    action: SignalAction
    confidence: float | None
    reference_price: float | None
    stop_loss: float | None
    take_profit: float | None
    reasons: list[str]
    data_as_of: str
    generated_at: str
    signal_status: str
    data_status: str
    sector: str | None = None
    component_scores: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol không được để trống")
        if self.action not in ALLOWED_ACTIONS:
            raise ValueError(f"action không hợp lệ: {self.action!r}")
        if self.confidence is not None and (
            not math.isfinite(self.confidence) or not 0 <= self.confidence <= 100
        ):
            raise ValueError("confidence phải nằm trong khoảng 0–100")
        for name, value in (
            ("reference_price", self.reference_price),
            ("stop_loss", self.stop_loss),
            ("take_profit", self.take_profit),
        ):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} phải là số dương hữu hạn")
        if not self.reasons:
            raise ValueError("SignalEvent phải có ít nhất một reason")

    @property
    def is_actionable(self) -> bool:
        """True với BUY/SELL/HOLD có dữ liệu đủ điều kiện."""

        return self.action != "NO SIGNAL" and self.signal_status == "ELIGIBLE"

    def to_dict(self) -> dict[str, Any]:
        """Chuyển thành dictionary để lưu SQLite hoặc gửi qua service."""

        return asdict(self)
