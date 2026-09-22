"""Chiến lược quality_trend_v1 theo strategy_spec phiên bản 1.5.0."""

from __future__ import annotations

import math
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from .models import ForeignFlowSummary, SignalEvent, SignalRequest


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def _number(data: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = data.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, (int, float, str)):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _text(data: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is not None:
            text = f"{value}".strip()
            if text:
                return text
    return None


def _bool_value(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return bool(value)


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.casefold())
    return "".join(character for character in decomposed if unicodedata.category(character) != "Mn")


class SignalStrategy(Protocol):
    """Interface tối thiểu để Signal Engine đăng ký nhiều chiến lược."""

    name: str
    version: str

    def evaluate(self, request: SignalRequest) -> SignalEvent: ...


@dataclass(frozen=True)
class QualityTrendConfig:
    """Ngưỡng của quality_trend_v1, tách khỏi logic đánh giá."""

    roe_ttm_min: float = 0.15
    debt_to_equity_max: float = 2.0
    rsi_min: float = 50.0
    rsi_max: float = 70.0
    foreign_window: int = 5
    foreign_min_positive_sessions: int = 3
    foreign_min_negative_sessions: int = 3
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.10
    max_position_realtime_age_minutes: float = 15.0
    market_weight: float = 0.20
    fundamental_weight: float = 0.25
    technical_weight: float = 0.35
    foreign_flow_weight: float = 0.20
    confluence_k: int | None = 4
    pure_asset_exit: bool = True
    market_regime: str = "SMA20"

    def __post_init__(self) -> None:
        if self.market_regime not in {"SMA20", "SMA50", "SMA200", "DUAL"}:
            raise ValueError("market_regime phải là một trong {'SMA20', 'SMA50', 'SMA200', 'DUAL'}")
        if self.confluence_k is not None and not (1 <= self.confluence_k <= 9):
            raise ValueError("confluence_k phải nằm trong khoảng [1, 9]")
        if not 0 < self.roe_ttm_min <= 1:
            raise ValueError("roe_ttm_min phải nằm trong khoảng (0, 1]")
        if self.debt_to_equity_max <= 0:
            raise ValueError("debt_to_equity_max phải > 0")
        if not 0 <= self.rsi_min < self.rsi_max <= 100:
            raise ValueError("Khoảng RSI không hợp lệ")
        if self.foreign_window < 1:
            raise ValueError("foreign_window phải >= 1")
        if not 1 <= self.foreign_min_positive_sessions <= self.foreign_window:
            raise ValueError("foreign_min_positive_sessions không hợp lệ")
        if not 1 <= self.foreign_min_negative_sessions <= self.foreign_window:
            raise ValueError("foreign_min_negative_sessions không hợp lệ")
        if not 0 < self.stop_loss_pct < 1 or not 0 < self.take_profit_pct < 1:
            raise ValueError("Stop loss/take profit phải nằm trong khoảng (0, 1)")
        if self.max_position_realtime_age_minutes <= 0:
            raise ValueError("max_position_realtime_age_minutes phải > 0")
        total_weight = (
            self.market_weight
            + self.fundamental_weight
            + self.technical_weight
            + self.foreign_flow_weight
        )
        if not math.isclose(total_weight, 1.0, rel_tol=1e-9):
            raise ValueError("Tổng trọng số confidence phải bằng 1.0")


class QualityTrendStrategy:
    """Sinh BUY/SELL/HOLD/NO SIGNAL theo đặc tả quality_trend_v1."""

    name = "quality_trend_v1"
    version = "1.6.0"
    FINANCIAL_SECTOR_KEYWORDS = {
        "bank",
        "banking",
        "securities",
        "financial",
        "finance",
        "ngan hang",
        "chung khoan",
        "tai chinh",
    }

    def __init__(self, config: QualityTrendConfig | None = None) -> None:
        self.config = config or QualityTrendConfig()

    def evaluate(self, request: SignalRequest) -> SignalEvent:
        snapshot = request.snapshot
        symbol = (_text(snapshot, "symbol") or "UNKNOWN").upper()
        data_status = (_text(snapshot, "data_status") or "DATA WARNING").upper()
        input_signal_status = (_text(snapshot, "signal_status") or "NO SIGNAL").upper()
        realtime_price = _number(snapshot, "realtime_price")
        reference_price = realtime_price or _number(snapshot, "latest_close")
        data_as_of = (
            _text(snapshot, "realtime_as_of", "price_as_of", "data_as_of") or ""
        )
        sector = request.sector or _text(snapshot, "sector", "industry")

        foreign = ForeignFlowSummary.from_sessions(
            request.foreign_sessions, self.config.foreign_window
        )

        if request.position.has_position:
            position_gate_reasons = self._position_price_gate_reasons(snapshot)
            if position_gate_reasons:
                return self._event(
                    symbol=symbol,
                    action="NO SIGNAL",
                    confidence=None,
                    reference_price=reference_price,
                    stop_loss=None,
                    take_profit=None,
                    reasons=position_gate_reasons,
                    data_as_of=data_as_of,
                    data_status="DATA WARNING",
                    signal_status="NO SIGNAL",
                    sector=sector,
                )
            assert realtime_price is not None
            return self._evaluate_position(
                request=request,
                symbol=symbol,
                sector=sector,
                data_status=data_status,
                data_as_of=data_as_of,
                close=realtime_price,
                sma_20=_number(snapshot, "sma_20"),
                sma_50=_number(snapshot, "sma_50"),
                foreign=foreign,
            )

        gate_reasons = self._entry_gate_reasons(
            snapshot, request, data_status, input_signal_status
        )
        if gate_reasons:
            return self._event(
                symbol=symbol,
                action="NO SIGNAL",
                confidence=None,
                reference_price=reference_price,
                stop_loss=None,
                take_profit=None,
                reasons=gate_reasons,
                data_as_of=data_as_of,
                data_status=data_status,
                signal_status="NO SIGNAL",
                sector=sector,
            )

        close = _number(snapshot, "realtime_price", "latest_close") or reference_price
        sma_20 = _number(snapshot, "sma_20")
        sma_50 = _number(snapshot, "sma_50")
        assert close is not None and sma_20 is not None and sma_50 is not None
        return self._evaluate_entry(
            request=request,
            symbol=symbol,
            sector=sector,
            data_status=data_status,
            data_as_of=data_as_of,
            close=close,
            sma_20=sma_20,
            sma_50=sma_50,
            foreign=foreign,
        )

    def _entry_gate_reasons(
        self,
        snapshot: Mapping[str, Any],
        request: SignalRequest,
        data_status: str,
        signal_status: str,
    ) -> list[str]:
        reasons: list[str] = []
        if data_status == "MISSING":
            reasons.append("Data Gate chặn: Không có dữ liệu của mã cổ phiếu này")
        if signal_status == "NO SIGNAL":
            quality_reasons = _text(snapshot, "quality_reasons")
            if quality_reasons and ("Thiếu dữ liệu giá" in quality_reasons or "phiên giá tối thiểu" in quality_reasons):
                reasons.append(f"Lỗi chất lượng dữ liệu: {quality_reasons}")

        required_price_fields = {
            "latest_close": _number(snapshot, "latest_close", "realtime_price"),
            "sma_20": _number(snapshot, "sma_20"),
            "sma_50": _number(snapshot, "sma_50"),
        }
        missing = [name for name, value in required_price_fields.items() if value is None]
        if missing:
            reasons.append(f"Thiếu dữ liệu giá bắt buộc: {', '.join(missing)}")
        return reasons

    def _position_price_gate_reasons(
            self, snapshot: Mapping[str, Any]
    ) -> list[str]:
        """Chỉ yêu cầu giá realtime đáng tin cậy cho cơ chế bảo vệ vị thế."""

        reasons: list[str] = []
        realtime_price = _number(snapshot, "realtime_price")
        if realtime_price is None or realtime_price <= 0:
            reasons.append("Risk/Exit Gate chặn: thiếu giá realtime hợp lệ")

        realtime_age = _number(snapshot, "realtime_age_minutes")
        if realtime_age is None:
            reasons.append(
                "Risk/Exit Gate chặn: không xác định được độ mới của giá realtime"
            )
        elif realtime_age < -5:
            reasons.append("Risk/Exit Gate chặn: thời gian realtime nằm trong tương lai")
        elif realtime_age > self.config.max_position_realtime_age_minutes:
            reasons.append(
                "Risk/Exit Gate chặn: giá realtime quá cũ "
                f"({realtime_age:.0f}/{self.config.max_position_realtime_age_minutes:.0f} phút)"
            )
        return reasons

    def _market_gate_failed(self, market: MarketContext | None, regime: str) -> list[str]:
        if market is None:
            return []
        m_c = market.close
        m_s20 = market.sma_20
        m_s50 = getattr(market, "sma_50", None)
        m_s200 = getattr(market, "sma_200", None)
        regime = (regime or "SMA20").strip().upper()

        if regime == "SMA200":
            if m_s200 is not None and not (m_c > m_s200):
                return [f"Thị trường chung (VNINDEX) chưa thuận lợi dài hạn: Giá {m_c:.2f} <= SMA200 {m_s200:.2f}"]
            if m_s200 is None and not (m_c > m_s20):
                return [f"Thị trường chung (VNINDEX) chưa thuận lợi: Giá {m_c:.2f} <= SMA20 {m_s20:.2f}"]
        elif regime == "DUAL":
            dual_ok = True
            if m_s50 is not None and not (m_c > m_s50):
                dual_ok = False
            if m_s200 is not None and not (m_c > m_s200):
                dual_ok = False
            if not dual_ok:
                s50_s = f"{m_s50:.2f}" if m_s50 else "N/A"
                s200_s = f"{m_s200:.2f}" if m_s200 else "N/A"
                return [f"Thị trường chung (VNINDEX) chưa thuận lợi: Giá {m_c:.2f} không vượt cả SMA50 ({s50_s}) và SMA200 ({s200_s})"]
        elif regime == "SMA50":
            if m_s50 is not None and not (m_c > m_s50):
                return [f"Thị trường chung (VNINDEX) chưa thuận lợi trung hạn: Giá {m_c:.2f} <= SMA50 {m_s50:.2f}"]
            if m_s50 is None and not (m_c > m_s20):
                return [f"Thị trường chung (VNINDEX) chưa thuận lợi: Giá {m_c:.2f} <= SMA20 {m_s20:.2f}"]
        else:
            if not (m_c > m_s20):
                return [f"Thị trường chung (VNINDEX) chưa thuận lợi: Giá {m_c:.2f} <= SMA20 {m_s20:.2f}"]
        return []

    def _evaluate_entry(
        self,
        *,
        request: SignalRequest,
        symbol: str,
        sector: str | None,
        data_status: str,
        data_as_of: str,
        close: float,
        sma_20: float | None,
        sma_50: float | None,
        foreign: ForeignFlowSummary,
    ) -> SignalEvent:
        snapshot = request.snapshot
        roe_ttm = _number(snapshot, "roe_ttm", "roe_quarter", "roe_year")
        debt_to_equity = _number(
            snapshot, "debt_to_equity_quarter", "debt_to_equity_year", "debt_to_equity"
        )
        rsi_14 = _number(snapshot, "rsi_14")
        macd = _number(snapshot, "macd")
        macd_signal = _number(snapshot, "macd_signal")
        stochastic_k = _number(snapshot, "stochastic_k_14")
        stochastic_d = _number(snapshot, "stochastic_d_3")
        bollinger_upper = _number(snapshot, "bollinger_upper_20")
        bollinger_lower = _number(snapshot, "bollinger_lower_20")
        bollinger_bandwidth = _number(snapshot, "bollinger_bandwidth_20")
        volume_ratio = _number(snapshot, "volume_ratio_20d")
        ob_support = _number(snapshot, "ob_support")
        ob_resistance = _number(snapshot, "ob_resistance")
        pe_val = _number(snapshot, "pe_quarter", "pe_year", "pe")
        pb_val = _number(snapshot, "pb_quarter", "pb_year", "pb")
        investment_mode = str(snapshot.get("investment_mode") or "SHORT_TERM").strip().upper()

        missing = [
            name
            for name, value in {
                "rsi_14": rsi_14,
                "macd": macd,
                "macd_signal": macd_signal,
            }.items()
            if value is None
        ]
        if missing:
            return self._event(
                symbol=symbol,
                action="NO SIGNAL",
                confidence=None,
                reference_price=close,
                stop_loss=None,
                take_profit=None,
                reasons=[f"Thiếu dữ liệu kỹ thuật bắt buộc: {', '.join(missing)}"],
                data_as_of=data_as_of,
                data_status="DATA WARNING",
                signal_status="NO SIGNAL",
                sector=sector,
            )

        assert rsi_14 is not None and macd is not None and macd_signal is not None
        is_financial = self._is_financial_sector(sector)

        # ── 1. ĐÁNH GIÁ ĐIỀU KIỆN MUA (BUY) ──────────────────────────
        # Đọc cấu hình chế độ SL/TP và mức % cá nhân
        sl_tp_mode = str(snapshot.get("sl_tp_mode") or "FIXED").strip().upper()
        user_sl_pct = _number(snapshot, "fixed_sl_pct") or self.config.stop_loss_pct
        user_tp_pct = _number(snapshot, "fixed_tp_pct") or self.config.take_profit_pct

        # Phân nhánh BUY cho DÀI HẠN (Cơ bản / Tích sản)
        if investment_mode == "LONG_TERM":
            lt_failed = []
            if roe_ttm is not None and roe_ttm < self.config.roe_ttm_min:
                lt_failed.append(f"Cơ bản yếu: ROE TTM = {roe_ttm*100:.1f}% dưới mức tối thiểu {self.config.roe_ttm_min*100:.1f}%")
            if not is_financial and debt_to_equity is not None and debt_to_equity > 1.8:
                lt_failed.append(f"Đòn bẩy cao: D/E = {debt_to_equity:.2f} vượt ngưỡng an toàn dài hạn (1.8)")
            if pe_val is not None and pe_val > 16.0:
                lt_failed.append(f"Định giá chưa đủ hấp dẫn để tích sản dài hạn: P/E = {pe_val:.1f} > 16.0")
            if pb_val is not None and pb_val > 2.2:
                lt_failed.append(f"Định giá P/B = {pb_val:.1f} > 2.2")
            if rsi_14 is not None and rsi_14 < 30 and foreign.net_volume < 0 and foreign.net_sell_sessions >= 4:
                lt_failed.append(f"Cổ phiếu đang bị bán tháo rủi ro cao (RSI={rsi_14:.1f} kèm khối ngoại xả mạnh)")

            if not lt_failed:
                lt_reasons = [
                    f"🏛 [DÀI HẠN] Đạt tiêu chuẩn đầu tư giá trị & tích sản:",
                    f"• Doanh nghiệp hiệu quả cao: ROE TTM = {(roe_ttm or 0.15)*100:.1f}%",
                ]
                if pe_val:
                    lt_reasons.append(f"• Định giá hấp dẫn: P/E = {pe_val:.1f}")
                if pb_val:
                    lt_reasons.append(f"• Định giá P/B = {pb_val:.1f}")
                lt_reasons.append("• Khuyến nghị TÍCH SẢN DÀI HẠN với biên an toàn lớn, bỏ qua rung lắc nến ngày.")

                return self._event(
                    symbol=symbol,
                    action="BUY",
                    confidence=85.0,
                    reference_price=close,
                    stop_loss=round(close * 0.85, 6),
                    take_profit=round(close * 1.35, 6),
                    reasons=lt_reasons,
                    data_as_of=data_as_of,
                    data_status=data_status,
                    signal_status="ELIGIBLE",
                    sector=sector,
                    metadata={
                        "investment_mode": "LONG_TERM",
                        "foreign_net_volume_5d": foreign.net_volume,
                        "foreign_net_buy_sessions_5d": foreign.net_buy_sessions,
                        "pe": pe_val,
                        "pb": pb_val,
                        "roe_ttm": roe_ttm,
                    },
                )

        # Phân nhánh BUY cho NGẮN HẠN (Kỹ thuật / Momentum) hoặc CẢ HAI
        failed = []
        is_short_term = (investment_mode == "SHORT_TERM")
        max_de = _number(snapshot, "debt_to_equity_max", "de_max") or self.config.debt_to_equity_max
        confluence_k_val = _number(snapshot, "confluence_k")
        if confluence_k_val is None:
            confluence_k_val = getattr(self.config, "confluence_k", None)
        confluence_k = int(confluence_k_val) if confluence_k_val is not None else None
        criteria_details: list[str] = []
        criteria_passed: int = 0
        total_confluence_n = 6 if is_short_term else 8

        if confluence_k is not None:
            # ── HYBRID CONFLUENCE MODE (Tầng 1: 4 Hard Gates + Tầng 2: Confluence K/N) ──
            hard_failed = []
            market_regime = str(snapshot.get("market_regime") or getattr(self.config, "market_regime", "SMA20")).strip().upper()
            m_failed = self._market_gate_failed(request.market, market_regime)
            if m_failed:
                hard_failed.extend(m_failed)

            if sma_20 is not None and close <= sma_20:
                hard_failed.append(f"Xu hướng yếu: Giá {close:.2f} nằm dưới SMA20 {sma_20:.2f}")

            if sma_20 is not None and sma_50 is not None and sma_20 <= sma_50:
                hard_failed.append(f"Cấu trúc xu hướng gãy: SMA20 ({sma_20:.2f}) nằm dưới hoặc bằng SMA50 ({sma_50:.2f})")

            if foreign.session_count >= self.config.foreign_window and foreign.net_volume < 0 and foreign.net_sell_sessions >= 4:
                hard_failed.append(f"Khối ngoại xả thảm họa: Bán ròng mạnh 4/5 phiên ({foreign.net_volume:,.0f})")

            if hard_failed:
                failed = hard_failed
            else:
                # Tầng 2: 6 Tiêu chí Kỹ thuật & Dòng tiền (N=6)
                c1 = bool(self.config.rsi_min <= rsi_14 <= self.config.rsi_max)
                if c1:
                    criteria_passed += 1
                    criteria_details.append("RSI 50-70")

                c2 = bool(macd > macd_signal)
                if c2:
                    criteria_passed += 1
                    criteria_details.append("MACD>Signal")

                c3 = bool(volume_ratio is None or volume_ratio >= 0.8)
                if c3:
                    criteria_passed += 1
                    criteria_details.append("Volume>=0.8x")

                c4 = bool(ob_resistance is None or close >= ob_resistance or ((ob_resistance - close) / close >= 0.035))
                if c4:
                    criteria_passed += 1
                    criteria_details.append("OB dư địa>=3.5%")

                c5 = bool(foreign.session_count > 0 and (foreign.net_volume > 0 or foreign.net_buy_sessions >= self.config.foreign_min_positive_sessions))
                if c5:
                    criteria_passed += 1
                    criteria_details.append("Khối ngoại gom ròng")

                c6 = not bool(bollinger_upper is not None and close > bollinger_upper * 1.01 and (bollinger_bandwidth is None or bollinger_bandwidth < 0.08))
                if c6:
                    criteria_passed += 1
                    criteria_details.append("Bollinger an toàn")

                if not is_short_term:
                    # Chế độ BOTH: Bổ sung 2 tiêu chí cơ bản vào Confluence (N=8)
                    c7 = bool(is_financial or debt_to_equity is None or debt_to_equity <= max_de)
                    if c7:
                        criteria_passed += 1
                        criteria_details.append(f"D/E<={max_de:.1f}")

                    c8 = bool(roe_ttm is None or roe_ttm >= self.config.roe_ttm_min)
                    if c8:
                        criteria_passed += 1
                        criteria_details.append(f"ROE>={self.config.roe_ttm_min*100:.0f}%")

                if criteria_passed >= confluence_k:
                    failed = []
                else:
                    failed = [f"Chưa đạt ngưỡng Confluence: {criteria_passed}/{total_confluence_n} tiêu chí đạt (yêu cầu >={confluence_k})"]
        else:
            # ── BASELINE MODE (Toán tử AND cứng) ──
            market_regime = str(snapshot.get("market_regime") or getattr(self.config, "market_regime", "SMA20")).strip().upper()
            m_failed = self._market_gate_failed(request.market, market_regime)
            if m_failed:
                failed.extend(m_failed)

            if not is_short_term:
                if roe_ttm is not None and roe_ttm < self.config.roe_ttm_min:
                    failed.append(f"Cơ bản yếu: ROE TTM = {roe_ttm*100:.1f}% dưới mức tối thiểu {self.config.roe_ttm_min*100:.1f}%")

                if not is_financial and debt_to_equity is not None and debt_to_equity > max_de:
                    failed.append(f"Cơ bản yếu: D/E = {debt_to_equity:.2f} vượt ngưỡng an toàn {max_de:.2f}")

            if sma_20 is not None and close <= sma_20:
                failed.append(f"Xu hướng yếu: Giá {close:.2f} nằm dưới SMA20 {sma_20:.2f}")

            if sma_20 is not None and sma_50 is not None and sma_20 <= sma_50:
                failed.append(f"Xu hướng yếu: SMA20 ({sma_20:.2f}) nằm dưới SMA50 ({sma_50:.2f})")

            if not (self.config.rsi_min <= rsi_14 <= self.config.rsi_max):
                failed.append(f"Động lượng yếu: RSI(14) = {rsi_14:.2f} không nằm trong khoảng yêu cầu ({self.config.rsi_min:.0f}-{self.config.rsi_max:.0f})")

            if macd <= macd_signal:
                failed.append(f"Động lượng yếu: MACD ({macd:.3f}) nằm dưới đường tín hiệu ({macd_signal:.3f})")

            if foreign.session_count >= self.config.foreign_window and foreign.net_volume < 0 and foreign.net_sell_sessions >= 4:
                failed.append(f"Dòng tiền yếu: Khối ngoại bán ròng mạnh trong 5 phiên ({foreign.net_volume:,.0f})")

            # ── BỘ LỌC TINH CHỈNH MỚI CHO BUY NGẮN HẠN ──
            # 1. Chặn bẫy mua rướn đỉnh dải trên Bollinger Bands:
            if bollinger_upper is not None and close > bollinger_upper * 1.01:
                if bollinger_bandwidth is None or bollinger_bandwidth < 0.08:
                    failed.append(f"Giá tiệm cận/vượt dải trên Bollinger ({close:,.0f} > {bollinger_upper:,.0f}) trong dải nén — rủi ro mua đỉnh ngắn hạn")

            # 2. Xác nhận khối lượng (Volume Confirmation - Chống Bull Trap):
            if volume_ratio is not None and volume_ratio < 0.8:
                failed.append(f"Thanh khoản yếu ({volume_ratio:.2f}x bình quân 20 phiên) — thiếu xác nhận dòng tiền lớn")

            # 3. Dư địa tăng tới kháng cự Order Block (Risk/Reward):
            if ob_resistance is not None and close < ob_resistance:
                upside_pct = (ob_resistance - close) / close
                if upside_pct < 0.035:
                    failed.append(f"Giá ({close:,.0f}) nằm quá sát cản Order Block ({ob_resistance:,.0f}) — biên lãi < 3.5% không tối ưu Risk/Reward")

        # ── 2. NẾU THỎA MÃN BUY -> XUẤT TÍN HIỆU BUY ────────────────
        if not failed:
            m_close = request.market.close if request.market else close
            m_sma20 = request.market.sma_20 if request.market else (sma_20 or close)
            scores = self._confidence_scores(
                close=close,
                sma_20=sma_20 or close,
                sma_50=sma_50 or close,
                rsi_14=rsi_14,
                macd=macd,
                macd_signal=macd_signal,
                roe_ttm=roe_ttm or 0.15,
                debt_to_equity=debt_to_equity,
                is_financial=is_financial,
                market_close=m_close,
                market_sma_20=m_sma20,
                foreign=foreign,
            )
            confidence = round(
                100
                * (
                    self.config.market_weight * scores["market"]
                    + self.config.fundamental_weight * scores["fundamental"]
                    + self.config.technical_weight * scores["technical"]
                    + self.config.foreign_flow_weight * scores["foreign_flow"]
                ),
                2,
            )
            if confluence_k is not None:
                reasons = [
                    f"Đạt mô hình Hybrid Confluence ({criteria_passed}/{total_confluence_n} tiêu chí: {', '.join(criteria_details)})",
                    f"Chiến lược Xu hướng & MA: Giá ({close:,.0f}) trên SMA20 ({sma_20:,.0f}) — Cấu trúc tăng giữ vững.",
                    f"Chiến lược Động lượng (RSI & MACD): RSI(14) = {rsi_14:.1f} | MACD ({macd:.3f}) so với Signal ({macd_signal:.3f}).",
                ]
            else:
                reasons = [
                    "Tất cả các điều kiện BUY kỹ thuật & xu hướng đều thỏa mãn!",
                    f"Chiến lược Xu hướng & MA: Giá ({close:,.0f}) > SMA20 ({sma_20:,.0f}) > SMA50 ({sma_50:,.0f}) — Xác nhận sóng tăng.",
                    f"Chiến lược Động lượng (RSI & MACD): RSI(14) = {rsi_14:.1f} chuẩn đà tăng | MACD ({macd:.3f}) cắt lên trên Signal ({macd_signal:.3f}).",
                ]
            if foreign.session_count > 0:
                reasons.append(
                    f"Chiến lược Dòng tiền Khối ngoại: Mua ròng {foreign.net_volume:+,.0f} cp ({foreign.net_buy_sessions}/{foreign.session_count} phiên) hỗ trợ lực cầu."
                )
            if is_financial:
                reasons.append("Áp dụng ngoại lệ D/E cho ngành tài chính")
            if investment_mode == "BOTH" and roe_ttm and roe_ttm >= 0.15:
                reasons.append(f"🏛 [Góc nhìn Dài hạn]: Nền tảng cơ bản tốt (ROE={roe_ttm*100:.1f}%), thuận lợi nắm giữ dài hạn")

            # Tính SL & TP
            if sl_tp_mode == "STRUCTURE":
                if ob_support is not None and 0 < ob_support < close:
                    stop_loss = round(ob_support, 6)
                    sl_source = "ORDER_BLOCK"
                else:
                    stop_loss = round(close * (1 - user_sl_pct), 6)
                    sl_source = "FALLBACK_FIXED"

                if ob_resistance is not None and ob_resistance > close:
                    take_profit = round(ob_resistance, 6)
                    tp_source = "ORDER_BLOCK"
                elif bollinger_upper is not None and bollinger_upper > close:
                    take_profit = round(bollinger_upper, 6)
                    tp_source = "BOLLINGER_UPPER"
                else:
                    take_profit = round(close * (1 + user_tp_pct), 6)
                    tp_source = "FALLBACK_FIXED"
            else:
                stop_loss = round(close * (1 - user_sl_pct), 6)
                sl_source = "FIXED"
                take_profit = round(close * (1 + user_tp_pct), 6)
                tp_source = "FIXED"

            return self._event(
                symbol=symbol,
                action="BUY",
                confidence=confidence,
                reference_price=close,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reasons=reasons,
                data_as_of=data_as_of,
                data_status=data_status,
                signal_status="ELIGIBLE",
                sector=sector,
                component_scores={key: round(value, 4) for key, value in scores.items()},
                metadata={
                    "investment_mode": investment_mode,
                    "foreign_net_volume_5d": foreign.net_volume,
                    "foreign_net_buy_sessions_5d": foreign.net_buy_sessions,
                    "sl_tp_mode": sl_tp_mode,
                    "sl_source": sl_source,
                    "tp_source": tp_source,
                    "ob_support": ob_support,
                    "ob_resistance": ob_resistance,
                    "user_sl_pct": user_sl_pct,
                    "user_tp_pct": user_tp_pct,
                    **({"confluence_k": confluence_k, "confluence_score": criteria_passed, "confluence_criteria": criteria_details} if confluence_k is not None else {}),
                },
            )

        # ── 3. KIỂM TRA ĐIỀU KIỆN BÁN (SELL) ──────────────────────────
        sell_reasons = []

        if investment_mode == "LONG_TERM":
            # Tiêu chuẩn BÁN cho Dài hạn: sự kiện trọng yếu, định giá bong bóng hoặc cơ bản suy thoái
            if pe_val is not None and pe_val >= 28.0:
                sell_reasons.append(f"🏛 [DÀI HẠN] Định giá P/E = {pe_val:.1f} quá đắt (vùng bong bóng / khuyến nghị chốt lời dài hạn)")
            if not is_financial and debt_to_equity is not None and debt_to_equity > 2.5:
                sell_reasons.append(f"🏛 [DÀI HẠN] Đòn bẩy tài chính tăng vọt nguy hiểm (D/E = {debt_to_equity:.2f} > 2.5)")
            if roe_ttm is not None and roe_ttm < 0:
                sell_reasons.append(f"🏛 [DÀI HẠN] Doanh nghiệp kinh doanh thua lỗ (ROE TTM = {roe_ttm*100:.1f}% < 0)")
            if foreign.session_count >= 5 and foreign.net_volume < 0 and foreign.net_sell_sessions >= 5 and sma_20 is not None and close < sma_20:
                sell_reasons.append(f"🏛 [DÀI HẠN] Khối ngoại xả tháo chạy 5/5 phiên liên tiếp khi giá thủng mốc xu hướng trung hạn")
        else:
            # Tiêu chuẩn BÁN Ngắn hạn / Kỹ thuật có bộ lọc hội tụ (Confluence Filters)
            # Kịch bản 1: Gãy xu hướng trung hạn
            if sma_20 is not None and sma_50 is not None and close < sma_20 and sma_20 < sma_50:
                if macd < macd_signal:
                    sell_reasons.append(f"Gãy xu hướng: Giá ({close:,.0f}) < SMA20 ({sma_20:,.0f}) < SMA50 ({sma_50:,.0f}) kèm MACD dốc xuống")

            # Kịch bản 2: Gãy dải dưới Bollinger Bands CÓ XÁC NHẬN
            bb_broken = bollinger_lower is not None and close < bollinger_lower
            foreign_dump = (
                foreign.session_count >= 5
                and foreign.net_volume < 0
                and foreign.net_sell_sessions >= 4
                and sma_20 is not None
                and close < sma_20
            )
            bb_expanding = bollinger_bandwidth is None or bollinger_bandwidth >= 0.08
            ob_broken = ob_support is not None and close < ob_support

            if bb_broken:
                if foreign_dump:
                    sell_reasons.append(
                        f"Gãy dải dưới Bollinger ({bollinger_lower:,.0f}) kèm áp lực bán ròng khối ngoại ({foreign.net_sell_sessions}/5 phiên) khi giá dưới SMA20"
                    )
                elif bb_expanding and rsi_14 < 45 and macd < macd_signal:
                    bw_str = f"{bollinger_bandwidth*100:.1f}%" if bollinger_bandwidth is not None else "đang mở"
                    sell_reasons.append(
                        f"Gãy dải dưới Bollinger ({bollinger_lower:,.0f}) trong pha mở rộng dải ({bw_str}) kèm RSI={rsi_14:.1f} và MACD suy yếu"
                    )
                elif ob_broken:
                    sell_reasons.append(
                        f"Gãy đồng thời dải dưới Bollinger ({bollinger_lower:,.0f}) và thủng đáy hỗ trợ Order Block ({ob_support:,.0f})"
                    )

            # Kịch bản 3: Thủng hỗ trợ cấu trúc Order Block độc lập
            if ob_broken and not bb_broken:
                if close < (sma_20 or close * 1.01) or macd < macd_signal:
                    sell_reasons.append(f"Thủng đáy vùng hỗ trợ Bullish Order Block ({ob_support:,.0f})")

            # Kịch bản 4: Đảo chiều tại vùng cản kháng cự quá mua
            if rsi_14 > 70 or (stochastic_k is not None and stochastic_k > 80):
                res_target = ob_resistance or bollinger_upper
                if res_target is not None and close >= res_target * 0.98:
                    if macd < macd_signal:
                        sell_reasons.append(f"Chạm vùng cản kháng cự ({res_target:,.0f}) trong trạng thái quá mua (RSI={rsi_14:.1f}) và MACD đảo chiều giảm")

            # Kịch bản 5: Khối ngoại phân phối áp đảo độc lập
            if foreign_dump and not bb_broken:
                sell_reasons.append(f"Áp lực bán ròng khối ngoại áp đảo ({foreign.net_sell_sessions}/5 phiên) khi giá nằm dưới SMA20")

        if sell_reasons:
            return self._event(
                symbol=symbol,
                action="SELL",
                confidence=None,
                reference_price=close,
                stop_loss=round(close * 1.05, 6) if investment_mode != "LONG_TERM" else round(close * 1.15, 6),
                take_profit=round(close * 0.90, 6) if investment_mode != "LONG_TERM" else round(close * 0.80, 6),
                reasons=sell_reasons,
                data_as_of=data_as_of,
                data_status=data_status,
                signal_status="ELIGIBLE",
                sector=sector,
                metadata={
                    "investment_mode": investment_mode,
                    "foreign_net_volume_5d": foreign.net_volume,
                    "foreign_net_buy_sessions_5d": foreign.net_buy_sessions,
                    "sell_trigger": "TECHNICAL_SELL" if investment_mode != "LONG_TERM" else "FUNDAMENTAL_SELL",
                    "ob_support": ob_support,
                    "ob_resistance": ob_resistance,
                },
            )

        # ── 4. NẾU KHÔNG PHẢI BUY VÀ SELL -> TÍN HIỆU LÀ HOLD (QUAN SÁT THÊM) ─
        hold_reasons = []
        if investment_mode == "LONG_TERM":
            hold_reasons.append("🏛 [DÀI HẠN] Định giá doanh nghiệp đang ở mức hợp lý.")
            if pe_val:
                pb_str = f"{pb_val:.1f}" if pb_val else "—"
                hold_reasons.append(f"• Định giá: P/E = {pe_val:.1f} | P/B = {pb_str}")
            if roe_ttm:
                hold_reasons.append(f"• Hiệu quả hoạt động: ROE TTM = {roe_ttm*100:.1f}%")
            hold_reasons.append("Chưa xuất hiện sự kiện trọng yếu hoặc định giá chiết khấu đủ sâu để tích sản thêm.")
            hold_reasons.append("Khuyến nghị: DUY TRÌ VỊ THẾ / QUAN SÁT THÊM kết quả kinh doanh quý tới.")
        else:
            # 1. Đánh giá Chiến lược Xu hướng & MA
            ma_trend = (
                f"Giá ({close:,.0f}) trên SMA20 ({sma_20:,.0f}) & SMA50 ({sma_50:,.0f}) — Cấu trúc tăng ngắn hạn giữ vững"
                if sma_20 and sma_50 and close > sma_20 and sma_20 > sma_50
                else f"Giá ({close:,.0f}) nằm dưới SMA20 ({sma_20:,.0f}) — Đang trong nhịp điều chỉnh ngắn hạn"
                if sma_20 and close < sma_20
                else f"Giá ({close:,.0f}) bám sát hỗ trợ SMA20 ({sma_20:,.0f})"
                if sma_20
                else "Đang tích lũy quanh vùng hỗ trợ"
            )
            hold_reasons.append(f"Chiến lược Xu hướng & MA: {ma_trend}.")

            # 2. Đánh giá Chiến lược Động lượng (RSI & MACD)
            rsi_desc = "Quá mua (>70)" if rsi_14 >= 70 else "Quá bán (<30)" if rsi_14 <= 30 else "Vùng tích lũy trung tính"
            macd_desc = "trên Signal (Động lượng tích cực)" if macd > macd_signal else "dưới Signal (Chờ điểm giao cắt bứt phá)"
            hold_reasons.append(
                f"Chiến lược Động lượng: RSI(14) = {rsi_14:.1f} ({rsi_desc}) | MACD ({macd:.2f}) {macd_desc}."
            )

            # 3. Đánh giá Chiến lược Dòng tiền Khối ngoại
            if foreign.session_count > 0:
                f_action = "Mua ròng" if foreign.net_volume > 0 else "Bán ròng" if foreign.net_volume < 0 else "Cân bằng"
                f_eval = "dòng tiền ngoại gom mua hỗ trợ lực cầu" if foreign.net_volume > 0 else "khối ngoại đang bán ròng nhẹ/điều chỉnh" if foreign.net_volume < 0 else "giao dịch cân bằng"
                hold_reasons.append(
                    f"Chiến lược Dòng tiền Khối ngoại: {f_action} {foreign.net_volume:+,.0f} cp ({foreign.net_buy_sessions}/{foreign.session_count} phiên mua) — {f_eval}."
                )

            # 4. Điểm nghẽn cần theo dõi thêm trước khi mở vị thế MUA (từ mảng failed)
            if failed:
                key_obstacles = [f.split(":")[-1].strip() for f in failed[:2]]
                hold_reasons.append(f"Điểm nghẽn cần theo dõi: {'; '.join(key_obstacles)}.")

            hold_reasons.append("Khuyến nghị: QUAN SÁT THÊM — Kiên nhẫn chờ điểm bứt phá xác nhận dòng tiền.")

        # Ghi chú nhận định chuyên sâu về Bollinger Bands & Quá bán (tránh false breakdown)
        if bollinger_lower is not None and close <= bollinger_lower:
            bw_pct = (bollinger_bandwidth * 100) if bollinger_bandwidth is not None else 0.0
            hold_reasons.append(
                f"⚠️ Giá đang chạm/thủng dải dưới Bollinger ({bollinger_lower:,.0f} đ) trong trạng thái dải nén ({bw_pct:.1f}%) hoặc thiếu xác nhận bán tháo: Tiềm ẩn nhịp hồi phục kỹ thuật (Mean Reversion), khuyến nghị QUAN SÁT THÊM, tránh bán tháo hoảng loạn."
            )
        elif rsi_14 <= 32:
            hold_reasons.append(
                f"⚠️ RSI(14) = {rsi_14:.1f} rơi vào vùng quá bán sâu: Khuyến nghị theo dõi phản ứng lực cầu rút chân thay vì bán tháo."
            )
        elif bollinger_upper is not None and close >= bollinger_upper * 0.99:
            hold_reasons.append(
                f"⚠️ Giá tiệm cận dải trên Bollinger ({bollinger_upper:,.0f} đ): Đang trong vùng rung lắc đỉnh ngắn hạn, theo dõi lực chốt lời."
            )

        if ob_support or ob_resistance:
            sup_str = f"{ob_support:,.0f} đ" if ob_support else "Đang cập nhật"
            res_str = f"{ob_resistance:,.0f} đ" if ob_resistance else "Đang cập nhật"
            hold_reasons.append(f"Biên độ quan sát: Hỗ trợ [{sup_str}] — Kháng cự [{res_str}]")

        return self._event(
            symbol=symbol,
            action="HOLD",
            confidence=None,
            reference_price=close,
            stop_loss=round(ob_support, 6) if ob_support else round(close * (1 - user_sl_pct), 6),
            take_profit=round(ob_resistance, 6) if ob_resistance else round(close * (1 + user_tp_pct), 6),
            reasons=hold_reasons,
            data_as_of=data_as_of,
            data_status=data_status,
            signal_status="ELIGIBLE",
            sector=sector,
            metadata={
                "investment_mode": investment_mode,
                "foreign_net_volume_5d": foreign.net_volume,
                "foreign_net_buy_sessions_5d": foreign.net_buy_sessions,
                "ob_support": ob_support,
                "ob_resistance": ob_resistance,
            },
        )

    def _evaluate_position(
        self,
        *,
        request: SignalRequest,
        symbol: str,
        sector: str | None,
        data_status: str,
        data_as_of: str,
        close: float,
        sma_20: float,
        sma_50: float,
        foreign: ForeignFlowSummary,
    ) -> SignalEvent:
        assert request.position.entry_price is not None
        entry_price = request.position.entry_price
        stop_loss = request.position.stop_loss or (entry_price * (1 - self.config.stop_loss_pct))
        take_profit = request.position.take_profit or (entry_price * (1 + self.config.take_profit_pct))
        session_low = request.session_low if request.session_low is not None else close
        session_high = request.session_high if request.session_high is not None else close

        snapshot = request.snapshot
        pure_asset_exit = _bool_value(snapshot.get("pure_asset_exit"))
        if pure_asset_exit is None:
            pure_asset_exit = getattr(self.config, "pure_asset_exit", True)

        sell_reason: str | None = None
        sell_trigger: str | None = None
        if session_low <= stop_loss:
            sell_trigger = "STOP_LOSS"
            sl_pct = (stop_loss - entry_price) / entry_price
            sell_reason = (
                f"Stop Loss: giá thấp nhất {session_low:,.2f} chạm ngưỡng "
                f"{stop_loss:,.2f} ({sl_pct:+.1%})"
            )
        elif session_high >= take_profit:
            sell_trigger = "TAKE_PROFIT"
            tp_pct = (take_profit - entry_price) / entry_price
            sell_reason = (
                f"Take Profit: giá cao nhất {session_high:,.2f} chạm ngưỡng "
                f"{take_profit:,.2f} ({tp_pct:+.1%})"
            )
        elif sma_20 is not None and sma_50 is not None and sma_20 < sma_50:
            sell_trigger = "TREND_EXIT"
            sell_reason = "Trend Exit: SMA20 đã nằm dưới SMA50"
        elif not pure_asset_exit and (
            request.market is not None
            and request.market.close <= request.market.sma_20
        ):
            sell_trigger = "MARKET_EXIT"
            sell_reason = "Market Exit: chỉ số thị trường đã đóng cửa dưới hoặc bằng SMA20"
        elif not pure_asset_exit and (
            foreign.session_count >= self.config.foreign_window
            and foreign.net_volume < 0
            and foreign.net_sell_sessions >= self.config.foreign_min_negative_sessions
        ):
            sell_trigger = "FOREIGN_NET_SELL"
            sell_reason = (
                "Foreign Net Sell: khối ngoại bán ròng 5 phiên và bán ròng "
                f"{foreign.net_sell_sessions}/5 phiên"
            )

        metadata: dict[str, Any] = {
            "entry_price": entry_price,
            "session_low": session_low,
            "session_high": session_high,
            "exit_context_complete": (
                sma_20 is not None
                and sma_50 is not None
                and request.market is not None
                and foreign.session_count >= self.config.foreign_window
            ),
            "foreign_session_count": foreign.session_count,
            "foreign_net_volume_5d": foreign.net_volume,
            "foreign_net_sell_sessions_5d": foreign.net_sell_sessions,
        }
        if sell_reason and sell_trigger:
            metadata["sell_trigger"] = sell_trigger
            return self._event(
                symbol=symbol,
                action="SELL",
                confidence=None,
                reference_price=close,
                stop_loss=round(stop_loss, 6),
                take_profit=round(take_profit, 6),
                reasons=[sell_reason],
                data_as_of=data_as_of,
                data_status=data_status,
                signal_status="ELIGIBLE",
                sector=sector,
                metadata=metadata,
            )

        reasons = ["Tiếp tục nắm giữ: chưa xuất hiện điều kiện SELL"]
        if (
            sma_20 is not None
            and sma_50 is not None
            and close < sma_20
            and sma_20 > sma_50
        ):
            reasons.append("Cảnh báo xu hướng yếu: Close dưới SMA20 nhưng SMA20 vẫn trên SMA50")
        if (
            foreign.session_count >= self.config.foreign_window
            and foreign.net_volume < 0
        ):
            reasons.append("Cảnh báo dòng tiền ngoại suy yếu: khối ngoại đang bán ròng nhẹ")
        if sma_20 is None or sma_50 is None:
            reasons.append("Thiếu SMA20/SMA50: bỏ qua kiểm tra Trend Exit")
        if request.market is None:
            reasons.append("Thiếu VNINDEX/VN30: bỏ qua kiểm tra Market Exit")
        if foreign.session_count < self.config.foreign_window:
            reasons.append(
                "Thiếu chuỗi khối ngoại: bỏ qua kiểm tra Foreign Net Sell "
                f"({foreign.session_count}/{self.config.foreign_window} phiên)"
            )
        return self._event(
            symbol=symbol,
            action="HOLD",
            confidence=None,
            reference_price=close,
            stop_loss=round(stop_loss, 6),
            take_profit=round(take_profit, 6),
            reasons=reasons,
            data_as_of=data_as_of,
            data_status=data_status,
            signal_status="ELIGIBLE",
            sector=sector,
            metadata=metadata,
        )

    def _confidence_scores(
        self,
        *,
        close: float,
        sma_20: float,
        sma_50: float,
        rsi_14: float,
        macd: float,
        macd_signal: float,
        roe_ttm: float,
        debt_to_equity: float | None,
        is_financial: bool,
        market_close: float,
        market_sma_20: float,
        foreign: ForeignFlowSummary,
    ) -> dict[str, float]:
        market_score = self._margin_score(market_close, market_sma_20, 0.05)
        roe_score = _clamp(roe_ttm / 0.30)
        debt_score = (
            1.0
            if is_financial
            else _clamp(1.0 - float(debt_to_equity or 0.0) / 2.0)
        )
        fundamental_score = (roe_score + debt_score) / 2

        price_score = self._margin_score(close, sma_20, 0.10)
        trend_score = self._margin_score(sma_20, sma_50, 0.10)
        rsi_score = _clamp(1.0 - abs(rsi_14 - 60.0) / 20.0)
        macd_scale = max(abs(close) * 0.02, 1e-9)
        macd_score = _clamp(0.5 + 0.5 * (macd - macd_signal) / macd_scale)
        technical_score = (price_score + trend_score + rsi_score + macd_score) / 4

        # Khi chỉ có net volume từng phiên, tổng trị tuyệt đối được suy ra từ summary
        # bằng số phiên mua/bán và net tổng. Điểm phiên vẫn giữ trọng số riêng.
        session_score = foreign.net_buy_sessions / self.config.foreign_window
        if foreign.total_buy_volume is not None and foreign.total_sell_volume is not None:
            gross = foreign.total_buy_volume + foreign.total_sell_volume
            strength_score = (
                _clamp(0.5 + 0.5 * foreign.net_volume / gross) if gross > 0 else 0.5
            )
        else:
            strength_score = 1.0 if foreign.net_volume > 0 else 0.0
        foreign_score = (session_score + strength_score) / 2

        return {
            "market": _clamp(market_score),
            "fundamental": _clamp(fundamental_score),
            "technical": _clamp(technical_score),
            "foreign_flow": _clamp(foreign_score),
        }

    @staticmethod
    def _margin_score(value: float, baseline: float, full_margin: float) -> float:
        relative_margin = value / baseline - 1.0
        return _clamp(0.5 + 0.5 * relative_margin / full_margin)

    @classmethod
    def _is_financial_sector(cls, sector: str | None) -> bool:
        if not sector:
            return False
        normalized = _normalize_text(sector)
        return any(keyword in normalized for keyword in cls.FINANCIAL_SECTOR_KEYWORDS)

    def _event(
        self,
        *,
        symbol: str,
        action: str,
        confidence: float | None,
        reference_price: float | None,
        stop_loss: float | None,
        take_profit: float | None,
        reasons: list[str],
        data_as_of: str,
        data_status: str,
        signal_status: str,
        sector: str | None,
        component_scores: dict[str, float] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SignalEvent:
        return SignalEvent(
            signal_id=str(uuid.uuid4()),
            symbol=symbol,
            strategy=self.name,
            strategy_version=self.version,
            action=action,  # type: ignore[arg-type]
            confidence=confidence,
            reference_price=reference_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reasons=reasons,
            data_as_of=data_as_of,
            generated_at=datetime.now(timezone.utc).isoformat(),
            signal_status=signal_status,
            data_status=data_status,
            sector=sector,
            component_scores=component_scores or {},
            metadata=metadata or {},
        )
