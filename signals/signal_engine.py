"""Bộ điều phối chiến lược và sinh SignalEvent từ dữ liệu đã làm sạch."""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .models import (
    ForeignFlowSession,
    MarketContext,
    PositionContext,
    SignalEvent,
    SignalRequest,
)
from .strategies import QualityTrendStrategy, SignalStrategy


class SignalEngine:
    """Đăng ký chiến lược và cung cấp API thống nhất để sinh tín hiệu."""

    def __init__(
        self,
        strategies: Iterable[SignalStrategy] | None = None,
        default_strategy: str = QualityTrendStrategy.name,
    ) -> None:
        configured = tuple(strategies or (QualityTrendStrategy(),))
        self._strategies = {strategy.name: strategy for strategy in configured}
        if not self._strategies:
            raise ValueError("SignalEngine cần ít nhất một chiến lược")
        if default_strategy not in self._strategies:
            raise ValueError(f"Không tìm thấy chiến lược mặc định {default_strategy!r}")
        self.default_strategy = default_strategy

    @property
    def strategy_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._strategies))

    def register(self, strategy: SignalStrategy, replace: bool = False) -> None:
        """Đăng ký thêm chiến lược; không ghi đè ngoài ý muốn."""

        if strategy.name in self._strategies and not replace:
            raise ValueError(f"Chiến lược {strategy.name!r} đã tồn tại")
        self._strategies[strategy.name] = strategy

    def generate(
        self,
        snapshot: Mapping[str, Any],
        *,
        market: MarketContext | Mapping[str, Any] | None = None,
        foreign_sessions: Sequence[
            ForeignFlowSession | Mapping[str, Any] | float | int
        ]
        | None = None,
        position: PositionContext | Mapping[str, Any] | None = None,
        session_low: float | None = None,
        session_high: float | None = None,
        sector: str | None = None,
        strategy_name: str | None = None,
    ) -> SignalEvent:
        """Sinh một tín hiệu; dữ liệu thiếu được chiến lược trả về NO SIGNAL."""

        strategy_key = strategy_name or self.default_strategy
        strategy = self._strategies.get(strategy_key)
        if strategy is None:
            raise ValueError(
                f"Chiến lược {strategy_key!r} chưa đăng ký. Có: {self.strategy_names}"
            )
        try:
            request = SignalRequest(
                snapshot=dict(snapshot),
                market=self._market_context(market),
                foreign_sessions=tuple(
                    ForeignFlowSession.from_value(item)
                    for item in (foreign_sessions or ())
                ),
                position=self._position_context(position),
                session_low=self._optional_number(session_low, "session_low"),
                session_high=self._optional_number(session_high, "session_high"),
                sector=sector,
            )
            if (
                request.session_low is not None
                and request.session_high is not None
                and request.session_low > request.session_high
            ):
                raise ValueError("session_low không được lớn hơn session_high")
        except (TypeError, ValueError) as exc:
            event = self._invalid_input_event(snapshot, strategy, str(exc), sector)
        else:
            event = strategy.evaluate(request)
        return self._finalize_event(event, snapshot)

    def generate_many(
        self,
        snapshots: Iterable[Mapping[str, Any]],
        *,
        markets: Mapping[str, MarketContext | Mapping[str, Any]] | None = None,
        foreign_by_symbol: Mapping[
            str, Sequence[ForeignFlowSession | Mapping[str, Any] | float | int]
        ]
        | None = None,
        positions: Mapping[str, PositionContext | Mapping[str, Any]] | None = None,
        sectors: Mapping[str, str] | None = None,
        strategy_name: str | None = None,
    ) -> list[SignalEvent]:
        """Sinh tín hiệu theo lô nhưng vẫn cô lập dữ liệu theo từng mã."""

        results: list[SignalEvent] = []
        for snapshot in snapshots:
            symbol = str(snapshot.get("symbol") or "").upper()
            market = (markets or {}).get(symbol) or (markets or {}).get("MARKET")
            results.append(
                self.generate(
                    snapshot,
                    market=market,
                    foreign_sessions=(foreign_by_symbol or {}).get(symbol, ()),
                    position=(positions or {}).get(symbol),
                    sector=(sectors or {}).get(symbol),
                    strategy_name=strategy_name,
                )
            )
        return results

    @staticmethod
    def _market_context(
        value: MarketContext | Mapping[str, Any] | None,
    ) -> MarketContext | None:
        if value is None or isinstance(value, MarketContext):
            return value
        return MarketContext.from_mapping(value)

    @staticmethod
    def _position_context(
        value: PositionContext | Mapping[str, Any] | None,
    ) -> PositionContext:
        if value is None:
            return PositionContext()
        if isinstance(value, PositionContext):
            return value
        return PositionContext.from_mapping(value)

    @staticmethod
    def _optional_number(value: Any, name: str) -> float | None:
        if value is None:
            return None
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} phải là số") from exc
        if not math.isfinite(result) or result <= 0:
            raise ValueError(f"{name} phải là số dương hữu hạn")
        return result

    @staticmethod
    def _finalize_event(
        event: SignalEvent,
        snapshot: Mapping[str, Any],
    ) -> SignalEvent:
        """Gắn metadata thực thi và signal_id ổn định theo cùng snapshot."""

        market_id = str(snapshot.get("market_id") or "").upper()
        exchange = str(snapshot.get("exchange") or "").upper() or {
            "STO": "HOSE",
            "STX": "HNX",
            "UPX": "UPCOM",
        }.get(market_id, "")
        price_unit = SignalEngine._finite_or_default(
            snapshot.get("price_unit_vnd"), 1000.0
        )
        average_volume = SignalEngine._finite_or_none(
            snapshot.get("average_volume_20d")
        )
        reference_price = event.reference_price or SignalEngine._finite_or_none(
            snapshot.get("latest_close")
        )
        execution_metadata = {
            "market_id": market_id or None,
            "board_id": snapshot.get("board_id"),
            "exchange": exchange or None,
            "price_unit_vnd": price_unit,
            "board_lot_size": int(snapshot.get("board_lot_size") or 100),
            "average_volume_20d": average_volume,
            "average_trading_value_20d": (
                average_volume * reference_price * price_unit
                if average_volume is not None and reference_price is not None
                else None
            ),
            "previous_close": SignalEngine._finite_or_none(
                snapshot.get("previous_close")
            ),
            "ceiling_price": SignalEngine._finite_or_none(
                snapshot.get("ceiling_price")
            ),
            "floor_price": SignalEngine._finite_or_none(snapshot.get("floor_price")),
            "realtime_total_volume": SignalEngine._finite_or_none(
                snapshot.get("total_volume")
            ),
        }
        metadata = {
            key: value
            for key, value in execution_metadata.items()
            if value is not None and value != ""
        }
        metadata.update(event.metadata)
        stable_key = "|".join(
            (
                event.strategy,
                event.strategy_version,
                event.symbol.upper(),
                event.action,
                event.data_as_of,
            )
        )
        return replace(
            event,
            signal_id=str(uuid.uuid5(uuid.NAMESPACE_URL, stable_key)),
            metadata=metadata,
        )

    @staticmethod
    def _finite_or_none(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _finite_or_default(value: Any, default: float) -> float:
        number = SignalEngine._finite_or_none(value)
        return number if number is not None and number > 0 else default

    @staticmethod
    def _invalid_input_event(
        snapshot: Mapping[str, Any],
        strategy: SignalStrategy,
        reason: str,
        sector: str | None,
    ) -> SignalEvent:
        """Đổi lỗi định dạng đầu vào thành NO SIGNAL thay vì làm dừng pipeline."""

        symbol = str(snapshot.get("symbol") or "UNKNOWN").upper()
        raw_price = snapshot.get("realtime_price", snapshot.get("latest_close"))
        try:
            reference_price = float(raw_price) if raw_price not in (None, "") else None
        except (TypeError, ValueError):
            reference_price = None
        if reference_price is not None and (
            not math.isfinite(reference_price) or reference_price <= 0
        ):
            reference_price = None
        data_as_of = str(
            snapshot.get("realtime_as_of")
            or snapshot.get("price_as_of")
            or snapshot.get("data_as_of")
            or ""
        )
        return SignalEvent(
            signal_id=str(uuid.uuid4()),
            symbol=symbol,
            strategy=strategy.name,
            strategy_version=strategy.version,
            action="NO SIGNAL",
            confidence=None,
            reference_price=reference_price,
            stop_loss=None,
            take_profit=None,
            reasons=[f"Dữ liệu đầu vào sai định dạng: {reason}"],
            data_as_of=data_as_of,
            generated_at=datetime.now(timezone.utc).isoformat(),
            signal_status="NO SIGNAL",
            data_status="DATA WARNING",
            sector=sector,
        )


def create_signal_engine() -> SignalEngine:
    """Factory mặc định để scheduler, CLI hoặc Telegram cùng sử dụng."""

    return SignalEngine()
