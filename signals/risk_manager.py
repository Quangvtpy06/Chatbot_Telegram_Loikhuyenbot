"""Risk Gate — lớp kiểm soát rủi ro chạy sau Signal Engine, trước Telegram Bot.

Luồng xử lý:
    SignalEvent  →  RiskGate.evaluate()  →  RiskDecision (ALLOW | BLOCK)

Sáu cổng chặn theo thứ tự:
    Gate 0  — Signal Status Gate      : chặn signal_status != ELIGIBLE
    Gate 1  — Stop Loss Gate          : chặn BUY thiếu hoặc sai stop loss
    Gate 2  — Reward/Risk Gate        : chặn BUY khi R:R dưới ngưỡng
    Gate 3  — Cooldown Gate           : chặn tín hiệu lặp lại trong cửa sổ thời gian
    Gate 4  — Position Limit Gate     : chặn BUY vượt giới hạn vị thế / ngành
    Gate 5  — Position State Gate     : chặn BUY/HOLD/SELL theo trạng thái vị thế

Phiên bản tích hợp cho project dnse — tương thích với signals.models.SignalEvent.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from signals.models import SignalEvent
except ImportError:
    from .models import SignalEvent

try:
    from storage.repositories import (
        CooldownRepository,
        PortfolioMetrics,
        PositionRepository,
        RiskDecision,
        RiskDecisionRepository,
    )
except ImportError:
    from dnse.storage.repositories import (
        CooldownRepository,
        PortfolioMetrics,
        PositionRepository,
        RiskDecision,
        RiskDecisionRepository,
    )

LOGGER = logging.getLogger("dnse.risk_gate")


# ---------------------------------------------------------------------------
# RiskConfig
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskConfig:
    """Cấu hình Risk Gate — tất cả ngưỡng đặt ở đây, không hardcode trong logic."""

    min_reward_risk_ratio: float = 2.0
    max_position_pct_per_symbol: float = 0.10
    max_position_pct_per_sector: float = 0.30
    max_open_positions: int = 10
    cooldown_seconds: int = 3_600
    require_stop_loss: bool = True
    require_take_profit: bool = True
    allow_short_sell: bool = True
    allow_position_addition: bool = False
    require_position_for_hold: bool = False
    require_sector_for_buy: bool = True
    max_data_age_seconds: int = 900
    max_signal_age_seconds: int = 120
    max_future_skew_seconds: int = 30
    risk_budget_pct_per_trade: float = 0.01
    min_position_size_pct: float = 0.01
    max_total_exposure_pct: float = 1.0
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.10
    min_average_trading_value_vnd: float = 5_000_000_000.0
    max_order_average_volume_pct: float = 0.10
    require_liquidity_data: bool = True
    enforce_board_lot: bool = True
    default_board_lot_size: int = 100
    enforce_tick_size: bool = True
    reject_buy_at_ceiling: bool = True

    def __post_init__(self) -> None:
        if self.min_reward_risk_ratio <= 0:
            raise ValueError("min_reward_risk_ratio phải > 0")
        if not 0 < self.max_position_pct_per_symbol <= 1:
            raise ValueError("max_position_pct_per_symbol phải trong khoảng (0, 1]")
        if not 0 < self.max_position_pct_per_sector <= 1:
            raise ValueError("max_position_pct_per_sector phải trong khoảng (0, 1]")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions phải >= 1")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds phải >= 0")
        if self.max_data_age_seconds <= 0:
            raise ValueError("max_data_age_seconds phải > 0")
        if self.max_signal_age_seconds <= 0:
            raise ValueError("max_signal_age_seconds phải > 0")
        if self.max_future_skew_seconds < 0:
            raise ValueError("max_future_skew_seconds phải >= 0")
        if not 0 < self.risk_budget_pct_per_trade <= 1:
            raise ValueError("risk_budget_pct_per_trade phải trong khoảng (0, 1]")
        if not 0 < self.min_position_size_pct <= 1:
            raise ValueError("min_position_size_pct phải trong khoảng (0, 1]")
        if not 0 < self.max_total_exposure_pct <= 1:
            raise ValueError("max_total_exposure_pct phải trong khoảng (0, 1]")
        if not 0 < self.max_daily_loss_pct <= 1:
            raise ValueError("max_daily_loss_pct phải trong khoảng (0, 1]")
        if not 0 < self.max_drawdown_pct <= 1:
            raise ValueError("max_drawdown_pct phải trong khoảng (0, 1]")
        if self.min_average_trading_value_vnd < 0:
            raise ValueError("min_average_trading_value_vnd phải >= 0")
        if not 0 < self.max_order_average_volume_pct <= 1:
            raise ValueError("max_order_average_volume_pct phải trong khoảng (0, 1]")
        if self.default_board_lot_size < 1:
            raise ValueError("default_board_lot_size phải >= 1")


# ---------------------------------------------------------------------------
# _GateResult
# ---------------------------------------------------------------------------

@dataclass
class _GateResult:
    passed: bool
    block_reason: Optional[str] = None
    allow_note: Optional[str] = None


# ---------------------------------------------------------------------------
# RiskGate
# ---------------------------------------------------------------------------

class RiskGate:
    """Đánh giá rủi ro và quyết định ALLOW/BLOCK cho từng SignalEvent."""

    def __init__(
        self,
        config: RiskConfig,
        cooldown_repo: CooldownRepository,
        position_repo: PositionRepository,
        risk_decision_repo: RiskDecisionRepository,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self._cooldown = cooldown_repo
        self._position = position_repo
        self._decisions = risk_decision_repo
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def evaluate(self, signal: SignalEvent) -> RiskDecision:
        """Đánh giá các cổng theo thứ tự, trả về quyết định idempotent."""
        existing = self._decisions.get_by_signal_id(signal.signal_id)
        if existing is not None:
            LOGGER.info("Bỏ qua signal_id đã đánh giá: %s", signal.signal_id)
            return existing

        now_utc = self._now_provider()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        else:
            now_utc = now_utc.astimezone(timezone.utc)

        block_reasons: list[str] = []
        allow_notes: list[str] = []
        rr_ratio: Optional[float] = None
        position_size_pct: Optional[float] = None
        cooldown_remaining: int = 0
        planned_quantity: Optional[int] = None
        planned_notional: Optional[float] = None
        liquidity_value_20d: Optional[float] = None
        portfolio_metrics = self._position.get_portfolio_metrics()
        daily_loss_pct = portfolio_metrics.daily_pnl_pct
        drawdown_pct = portfolio_metrics.drawdown_pct
        run_id = str(uuid.uuid4())

        LOGGER.info(
            "[%s] Bắt đầu đánh giá: %s %s (strategy=%s v%s)",
            run_id[:8], signal.symbol, signal.action,
            signal.strategy, signal.strategy_version,
        )

        # ── Gate 0: chất lượng và thời điểm dữ liệu ───────────────────
        g0 = self._gate0_signal_status(signal, now_utc)
        if not g0.passed:
            block_reasons.append(g0.block_reason)
        else:
            allow_notes.append(g0.allow_note)

        if block_reasons:
            return self._make_decision(
                run_id=run_id, signal=signal, verdict="BLOCK",
                block_reasons=block_reasons, allow_reasons=allow_notes,
                rr_ratio=None, position_size_pct=None,
                cooldown_remaining=0, now_utc=now_utc,
                planned_quantity=None, planned_notional=None,
                daily_loss_pct=daily_loss_pct, drawdown_pct=drawdown_pct,
                liquidity_value_20d=None,
            )

        # ── Gate 5: Trạng thái vị thế ─────────────────────────────────
        position_gate = self._gate_position_state(signal)
        if not position_gate.passed:
            block_reasons.append(position_gate.block_reason)
        else:
            allow_notes.append(position_gate.allow_note)

        # ── Gate 6: Daily loss và drawdown của danh mục ──────────────
        if signal.action == "BUY" and not block_reasons:
            g6 = self._gate_portfolio_loss(portfolio_metrics)
            if not g6.passed:
                block_reasons.append(g6.block_reason)
            else:
                allow_notes.append(g6.allow_note)

        # ── Gate 1: Stop Loss ─────────────────────────────────────────
        if signal.action == "BUY" and not block_reasons:
            g1 = self._gate1_stop_loss(signal)
            if not g1.passed:
                block_reasons.append(g1.block_reason)
            else:
                allow_notes.append(g1.allow_note)

        # ── Gate 2: Reward/Risk ────────────────────────────────────────
        if signal.action == "BUY" and not block_reasons:
            g2, rr_ratio = self._gate2_reward_risk(signal)
            if not g2.passed:
                block_reasons.append(g2.block_reason)
            else:
                allow_notes.append(g2.allow_note)

        # ── Gate 4: Position sizing và giới hạn danh mục ──────────────
        if signal.action == "BUY" and not block_reasons:
            g4, position_size_pct = self._gate4_position_limit(signal)
            if not g4.passed:
                block_reasons.append(g4.block_reason)
            else:
                allow_notes.append(g4.allow_note)

        # ── Gate 7: thanh khoản, biên giá, bước giá và lô chẵn ───────
        if signal.action == "BUY" and not block_reasons:
            g7, planned_quantity, planned_notional, liquidity_value_20d = (
                self._gate_market_execution(
                    signal,
                    position_size_pct or 0.0,
                    portfolio_metrics.current_nav,
                )
            )
            if not g7.passed:
                block_reasons.append(g7.block_reason)
            else:
                allow_notes.append(g7.allow_note)

        # ── Gate 3: Cooldown chiến lược ───────────────────────────────
        if signal.action in ("BUY", "SELL") and not block_reasons:
            g3, cooldown_remaining = self._gate3_cooldown(signal, now_utc)
            if not g3.passed:
                block_reasons.append(g3.block_reason)
            else:
                allow_notes.append(g3.allow_note)

        # ── Kết luận ───────────────────────────────────────────────────
        verdict = "BLOCK" if block_reasons else "ALLOW"

        if verdict == "ALLOW" and signal.action in ("BUY", "SELL"):
            self._cooldown.record_allowed_signal(
                symbol=signal.symbol, action=signal.action,
                strategy=signal.strategy, timestamp=now_utc,
            )

        decision = self._make_decision(
            run_id=run_id, signal=signal, verdict=verdict,
            block_reasons=block_reasons, allow_reasons=allow_notes,
            rr_ratio=rr_ratio, position_size_pct=position_size_pct,
            cooldown_remaining=cooldown_remaining, now_utc=now_utc,
            planned_quantity=planned_quantity,
            planned_notional=planned_notional,
            daily_loss_pct=daily_loss_pct,
            drawdown_pct=drawdown_pct,
            liquidity_value_20d=liquidity_value_20d,
        )

        log_level = logging.INFO if verdict == "ALLOW" else logging.WARNING
        LOGGER.log(
            log_level, "[%s] %s %s → %s | block=%s",
            run_id[:8], signal.symbol, signal.action, verdict,
            block_reasons if block_reasons else "—",
        )
        return decision

    # ------------------------------------------------------------------
    # Gate implementations
    # ------------------------------------------------------------------

    def _gate0_signal_status(self, signal: SignalEvent, now_utc: datetime) -> _GateResult:
        """Chặn NO SIGNAL, data lỗi, timestamp cũ/tương lai."""
        if signal.action == "NO SIGNAL":
            return _GateResult(passed=False,
                               block_reason="[Gate0] action=NO SIGNAL không được phép đi qua Risk Gate")
        if signal.action == "BUY" and signal.data_status != "OK":
            return _GateResult(passed=False,
                               block_reason=f"[Gate0] data_status={signal.data_status!r}; cần 'OK'")
        if signal.action != "BUY" and signal.data_status not in ("OK", "DATA WARNING"):
            return _GateResult(passed=False,
                               block_reason=f"[Gate0] data_status={signal.data_status!r}; cần 'OK' hoặc 'DATA WARNING'")
        if signal.signal_status != "ELIGIBLE":
            return _GateResult(passed=False,
                               block_reason=f"[Gate0] signal_status={signal.signal_status!r} (cần ELIGIBLE)")

        for label, raw_value, maximum_age in (
            ("data_as_of", signal.data_as_of, self.config.max_data_age_seconds),
            ("generated_at", signal.generated_at, self.config.max_signal_age_seconds),
        ):
            parsed = self._parse_utc_timestamp(raw_value)
            if parsed is None:
                return _GateResult(passed=False,
                                   block_reason=f"[Gate0] {label} thiếu timezone hoặc sai ISO-8601")
            age_seconds = (now_utc - parsed).total_seconds()
            if age_seconds < -self.config.max_future_skew_seconds:
                return _GateResult(passed=False,
                                   block_reason=f"[Gate0] {label} nằm trong tương lai ({-age_seconds:.0f}s)")
            if age_seconds > maximum_age:
                return _GateResult(passed=False,
                                   block_reason=f"[Gate0] {label} quá cũ ({age_seconds:.0f}/{maximum_age}s)")

        return _GateResult(passed=True, allow_note="[Gate0] chất lượng và thời điểm dữ liệu hợp lệ ✓")

    def _gate1_stop_loss(self, signal: SignalEvent) -> _GateResult:
        ref_price = signal.reference_price or 0.0
        if self.config.require_stop_loss and signal.stop_loss is None:
            return _GateResult(passed=False,
                               block_reason=f"[Gate1] BUY {signal.symbol} thiếu stop_loss")
        if signal.stop_loss is not None and signal.stop_loss >= ref_price:
            return _GateResult(passed=False,
                               block_reason=f"[Gate1] stop_loss ({signal.stop_loss:,.0f}) phải nhỏ hơn reference_price ({ref_price:,.0f})")
        note = (f"[Gate1] stop_loss={signal.stop_loss:,.0f} ✓" if signal.stop_loss is not None
                else "[Gate1] stop_loss không bắt buộc ✓")
        return _GateResult(passed=True, allow_note=note)

    def _gate2_reward_risk(self, signal: SignalEvent) -> tuple[_GateResult, Optional[float]]:
        ref_price = signal.reference_price or 0.0
        if signal.take_profit is None:
            if self.config.require_take_profit:
                return (_GateResult(passed=False,
                                    block_reason=f"[Gate2] BUY {signal.symbol} thiếu take_profit"), None)
            return (_GateResult(passed=True, allow_note="[Gate2] take_profit không bắt buộc ✓"), None)

        if signal.take_profit <= ref_price:
            return (_GateResult(passed=False,
                                block_reason=f"[Gate2] take_profit ({signal.take_profit:,.0f}) phải lớn hơn reference_price ({ref_price:,.0f})"), None)

        if signal.stop_loss is None:
            return (_GateResult(passed=True, allow_note="[Gate2] Không tính R:R vì thiếu stop_loss ✓"), None)

        risk = ref_price - signal.stop_loss
        if risk <= 0:
            return (_GateResult(passed=False, block_reason=f"[Gate2] risk = {risk:.2f} ≤ 0"), None)

        reward = signal.take_profit - ref_price
        rr = reward / risk
        if rr + 1e-9 < self.config.min_reward_risk_ratio:
            return (_GateResult(passed=False,
                                block_reason=f"[Gate2] R:R = {rr:.2f} < ngưỡng {self.config.min_reward_risk_ratio:.2f}"), rr)
        return (_GateResult(passed=True,
                            allow_note=f"[Gate2] R:R = {rr:.2f} ≥ {self.config.min_reward_risk_ratio:.2f} ✓"), rr)

    def _gate3_cooldown(self, signal: SignalEvent, now_utc: datetime) -> tuple[_GateResult, int]:
        if self.config.cooldown_seconds == 0:
            return (_GateResult(passed=True, allow_note="[Gate3] Cooldown tắt ✓"), 0)
        last = self._cooldown.get_last_allowed_signal(
            symbol=signal.symbol, action=signal.action, strategy=signal.strategy)
        if last is None:
            return (_GateResult(passed=True, allow_note="[Gate3] Chưa có signal trước ✓"), 0)
        elapsed = int((now_utc - last).total_seconds())
        remaining = max(0, self.config.cooldown_seconds - elapsed)
        if remaining > 0:
            return (_GateResult(passed=False,
                                block_reason=f"[Gate3] Cooldown còn {remaining // 60}m{remaining % 60}s"), remaining)
        return (_GateResult(passed=True, allow_note=f"[Gate3] Cooldown đã hết ✓"), 0)

    def _gate4_position_limit(self, signal: SignalEvent) -> tuple[_GateResult, Optional[float]]:
        ref_price = signal.reference_price or 0.0
        if signal.stop_loss is None:
            return (_GateResult(passed=False, block_reason="[Gate4] Thiếu stop_loss để tính position size"), None)
        stop_distance_pct = (ref_price - signal.stop_loss) / ref_price if ref_price > 0 else 0
        if stop_distance_pct <= 0:
            return (_GateResult(passed=False, block_reason="[Gate4] Khoảng cách stop loss phải lớn hơn 0"), None)
        if stop_distance_pct > 0.20:
            return (
                _GateResult(
                    passed=False,
                    block_reason=f"[Gate4] Khoảng cách cắt lỗ quá xa ({stop_distance_pct:.1%} > 20%), rủi ro vượt ngưỡng an toàn",
                ),
                None,
            )

        open_count = self._position.count_open_positions()
        has_position = self._position.has_open_position(signal.symbol)
        if not has_position and open_count >= self.config.max_open_positions:
            return (_GateResult(passed=False,
                                block_reason=f"[Gate4] Đã đạt tối đa {self.config.max_open_positions} vị thế mở"), None)

        symbol_exp = self._position.get_symbol_exposure_pct(signal.symbol)
        total_exp = self._position.get_total_exposure_pct()
        if self.config.require_sector_for_buy and not signal.sector:
            return (_GateResult(passed=False, block_reason="[Gate4] BUY thiếu sector"), None)
        sector_exp = self._position.get_sector_exposure_pct(signal.sector) if signal.sector else 0.0

        effective_stop_distance = max(0.01, stop_distance_pct)
        raw_size = self.config.risk_budget_pct_per_trade / effective_stop_distance
        available = min(
            self.config.max_position_pct_per_symbol - symbol_exp,
            self.config.max_position_pct_per_sector - sector_exp,
            self.config.max_total_exposure_pct - total_exp,
        )
        position_size = min(raw_size, available)
        if position_size < self.config.min_position_size_pct:
            return (_GateResult(passed=False,
                                block_reason=f"[Gate4] Dung lượng không đủ: khả dụng={max(0.0, available):.1%}"), None)

        clamped_note = " (áp dụng sàn rủi ro 1.0%)" if stop_distance_pct < 0.01 else ""
        return (_GateResult(passed=True,
                            allow_note=f"[Gate4] position_size={position_size:.1%}{clamped_note} ✓"), position_size)

    def _gate_portfolio_loss(self, metrics: PortfolioMetrics) -> _GateResult:
        if metrics.daily_pnl_pct <= -self.config.max_daily_loss_pct:
            return _GateResult(
                passed=False,
                block_reason=(
                    f"[Gate6] Lỗ trong ngày {metrics.daily_pnl_pct:.2%} đã chạm "
                    f"ngưỡng -{self.config.max_daily_loss_pct:.2%}"
                ),
            )
        if metrics.drawdown_pct <= -self.config.max_drawdown_pct:
            return _GateResult(
                passed=False,
                block_reason=(
                    f"[Gate6] Drawdown {metrics.drawdown_pct:.2%} đã chạm "
                    f"ngưỡng -{self.config.max_drawdown_pct:.2%}"
                ),
            )
        return _GateResult(
            passed=True,
            allow_note=(
                f"[Gate6] daily={metrics.daily_pnl_pct:.2%}, "
                f"drawdown={metrics.drawdown_pct:.2%} ✓"
            ),
        )

    def _gate_market_execution(
        self,
        signal: SignalEvent,
        position_size_pct: float,
        current_nav: float,
    ) -> tuple[_GateResult, Optional[int], Optional[float], Optional[float]]:
        metadata = signal.metadata or {}
        ref_price = float(signal.reference_price or 0.0)
        price_unit = self._positive_number(metadata.get("price_unit_vnd")) or 1000.0
        average_volume = self._positive_number(metadata.get("average_volume_20d"))
        liquidity_value = self._positive_number(
            metadata.get("average_trading_value_20d")
        )
        if liquidity_value is None and average_volume is not None:
            liquidity_value = average_volume * ref_price * price_unit
        if self.config.require_liquidity_data and liquidity_value is None:
            return (
                _GateResult(False, "[Gate7] Thiếu thanh khoản bình quân 20 phiên"),
                None, None, None,
            )
        if (
            liquidity_value is not None
            and liquidity_value < self.config.min_average_trading_value_vnd
        ):
            return (
                _GateResult(
                    False,
                    "[Gate7] GTGD bình quân 20 phiên "
                    f"{liquidity_value:,.0f} VND < "
                    f"{self.config.min_average_trading_value_vnd:,.0f} VND",
                ),
                None, None, liquidity_value,
            )

        exchange = str(metadata.get("exchange") or "").upper()
        tick_size = self._tick_size(ref_price, price_unit, exchange)
        if self.config.enforce_tick_size and tick_size is not None:
            nearest = round(ref_price / tick_size) * tick_size
            if not math.isclose(ref_price, nearest, abs_tol=max(1e-8, tick_size / 1000)):
                return (
                    _GateResult(
                        False,
                        f"[Gate7] Giá {ref_price:g} không đúng bước giá {tick_size:g}",
                    ),
                    None, None, liquidity_value,
                )

        ceiling = self._positive_number(metadata.get("ceiling_price"))
        floor = self._positive_number(metadata.get("floor_price"))
        previous = self._positive_number(metadata.get("previous_close"))
        band = {"HOSE": 0.07, "HSX": 0.07, "HNX": 0.10, "UPCOM": 0.15}.get(exchange)
        if previous is not None and band is not None:
            ceiling = ceiling or previous * (1 + band)
            floor = floor or previous * (1 - band)
        tolerance = tick_size or max(ref_price * 0.0001, 1e-6)
        if floor is not None and ref_price < floor - tolerance:
            return (
                _GateResult(False, "[Gate7] Giá nằm dưới giá sàn hợp lệ"),
                None, None, liquidity_value,
            )
        if (
            self.config.reject_buy_at_ceiling
            and ceiling is not None
            and ref_price >= ceiling - tolerance
        ):
            return (
                _GateResult(False, "[Gate7] Không BUY khi giá đang ở sát/trên giá trần"),
                None, None, liquidity_value,
            )

        lot_size = int(
            self._positive_number(metadata.get("board_lot_size"))
            or self.config.default_board_lot_size
        )
        raw_quantity = int((current_nav * position_size_pct) // (ref_price * price_unit))
        quantity = (
            (raw_quantity // lot_size) * lot_size
            if self.config.enforce_board_lot
            else raw_quantity
        )
        if quantity <= 0 or (self.config.enforce_board_lot and quantity < lot_size):
            return (
                _GateResult(False, f"[Gate7] Quy mô lệnh không đủ một lô {lot_size}"),
                None, None, liquidity_value,
            )
        if (
            average_volume is not None
            and quantity > average_volume * self.config.max_order_average_volume_pct
        ):
            return (
                _GateResult(
                    False,
                    f"[Gate7] Khối lượng {quantity:,} vượt "
                    f"{self.config.max_order_average_volume_pct:.0%} KLGD bình quân",
                ),
                None, None, liquidity_value,
            )
        notional = quantity * ref_price * price_unit
        return (
            _GateResult(
                True,
                f"[Gate7] thanh khoản/lô/bước giá hợp lệ; KL={quantity:,} ✓",
            ),
            quantity,
            notional,
            liquidity_value,
        )

    @staticmethod
    def _positive_number(value: object) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    @staticmethod
    def _tick_size(
        price: float,
        price_unit_vnd: float,
        exchange: str,
    ) -> Optional[float]:
        price_vnd = price * price_unit_vnd
        if exchange in {"HOSE", "HSX"}:
            tick_vnd = 10.0 if price_vnd < 10_000 else 50.0 if price_vnd < 50_000 else 100.0
        elif exchange in {"HNX", "UPCOM"}:
            tick_vnd = 100.0
        else:
            return None
        return tick_vnd / price_unit_vnd

    def _gate_position_state(self, signal: SignalEvent) -> _GateResult:
        has_pos = self._position.has_open_position(signal.symbol)
        if signal.action == "BUY" and has_pos and not self.config.allow_position_addition:
            return _GateResult(passed=False,
                               block_reason=f"[Gate5] BUY {signal.symbol}: đã có vị thế mở")
        if signal.action == "HOLD" and self.config.require_position_for_hold and not has_pos:
            return _GateResult(passed=False,
                               block_reason=f"[Gate5] HOLD {signal.symbol}: không có vị thế mở")
        if signal.action == "SELL" and not has_pos and not self.config.allow_short_sell:
            return _GateResult(passed=False,
                               block_reason=f"[Gate5] SELL {signal.symbol}: không có vị thế mở")
        return _GateResult(passed=True,
                           allow_note=f"[Gate5] trạng thái vị thế phù hợp với {signal.action} ✓")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_decision(
        self, run_id: str, signal: SignalEvent, verdict: str,
        block_reasons: list[str], allow_reasons: list[str],
        rr_ratio: Optional[float], position_size_pct: Optional[float],
        cooldown_remaining: int, now_utc: datetime,
        planned_quantity: Optional[int], planned_notional: Optional[float],
        daily_loss_pct: Optional[float], drawdown_pct: Optional[float],
        liquidity_value_20d: Optional[float],
    ) -> RiskDecision:
        decision = RiskDecision(
            decision_id=run_id, signal_id=signal.signal_id,
            symbol=signal.symbol, action=signal.action, verdict=verdict,
            block_reasons=list(block_reasons), allow_reasons=list(allow_reasons),
            checked_at=now_utc.isoformat(),
            reward_risk_ratio=rr_ratio, position_size_pct=position_size_pct,
            cooldown_remaining_seconds=cooldown_remaining,
            planned_quantity=planned_quantity,
            planned_notional=planned_notional,
            daily_loss_pct=daily_loss_pct,
            drawdown_pct=drawdown_pct,
            liquidity_value_20d=liquidity_value_20d,
        )
        return self._decisions.save(decision)

    @staticmethod
    def _parse_utc_timestamp(value: str) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_risk_gate(
    db_path: Path,
    config: Optional[RiskConfig] = None,
    now_provider: Callable[[], datetime] | None = None,
    initial_nav: float = 1_000_000_000.0,
) -> RiskGate:
    """Tạo RiskGate đã kết nối với SQLite tại db_path."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return RiskGate(
        config=config or RiskConfig(),
        cooldown_repo=CooldownRepository(db_path),
        position_repo=PositionRepository(db_path, initial_nav=initial_nav),
        risk_decision_repo=RiskDecisionRepository(db_path),
        now_provider=now_provider,
    )
