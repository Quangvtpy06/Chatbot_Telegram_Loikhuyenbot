"""Kiểm tra Entry Gate và Risk/Exit Gate của chiến lược."""

from __future__ import annotations

from signals.signal_engine import SignalEngine
import pytest


def _snapshot_dai_han(**overrides):
    """Dữ liệu cơ bản đủ và kỹ thuật thuận lợi để phát hiện lỗi chạy lẫn nhánh."""
    return {
        "symbol": "MSB", "investment_mode": "LONG_TERM", "sector": "bank",
        "data_status": "OK", "signal_status": "ELIGIBLE", "latest_close": 14.,
        "realtime_price": 14., "sma_20": 13., "sma_50": 12., "rsi_14": 60.,
        "macd": .3, "macd_signal": .1, "volume_ratio_20d": 1.2,
        "bollinger_upper_20": 16., "bollinger_lower_20": 11.,
        "roe_ttm": .18, "pe_quarter": 10., "pb_quarter": 1.2,
        **overrides,
    }


@pytest.mark.parametrize("changes,action", [
    ({}, "BUY"), ({"roe_ttm": .1315}, "HOLD"),
    ({"pe_quarter": 20.}, "HOLD"), ({"pe_quarter": 30.}, "SELL"),
    ({"roe_ttm": -.1}, "SELL"),
    ({"pe_quarter": None}, "NO SIGNAL"), ({"roe_ttm": None}, "NO SIGNAL"),
    ({"pb_quarter": -1.}, "NO SIGNAL"),
    ({"sector": None, "debt_to_equity_year": 8.6}, "NO SIGNAL"),
    ({"sector": "bank", "debt_to_equity_year": 8.6}, "BUY"),
    ({"sector": "manufacturing", "debt_to_equity_year": 3.}, "SELL"),
])
def test_dai_han_khong_roi_xuong_buy_ky_thuat(changes, action):
    event = SignalEngine().generate(_snapshot_dai_han(**changes))
    assert event.action == action
    assert event.metadata["investment_mode"] == "LONG_TERM"
    assert all("Confluence" not in reason for reason in event.reasons)
    if action in {"HOLD", "NO SIGNAL"}:
        assert event.stop_loss is None and event.take_profit is None


def test_dai_han_khong_phu_thuoc_rsi_macd_sma():
    snapshot = _snapshot_dai_han(rsi_14=None, macd=None, macd_signal=None, sma_20=None, sma_50=None)
    engine = SignalEngine()
    assert engine.generate(snapshot).action == "BUY"
    snapshot["investment_mode"] = "SHORT_TERM"
    assert engine.generate(snapshot).action == "NO SIGNAL"


def test_ngan_han_mua_nhung_dai_han_khong_dat_roe():
    engine = SignalEngine()
    snapshot = _snapshot_dai_han(roe_ttm=.1315)
    assert engine.generate(snapshot).action == "HOLD"
    snapshot["investment_mode"] = "SHORT_TERM"
    assert engine.generate(snapshot).action == "BUY"


def _warning_snapshot(price: float, age_minutes: float = 1.0) -> dict[str, object]:
    return {
        "symbol": "FPT",
        "data_status": "DATA WARNING",
        "signal_status": "NO SIGNAL",
        "quality_reasons": "Thiếu ROE TTM | Thiếu VNINDEX | Thiếu 5 phiên khối ngoại",
        "realtime_price": price,
        "realtime_age_minutes": age_minutes,
        "realtime_as_of": "2026-09-21T09:30:00+07:00",
        "latest_close": price,
    }


def test_entry_van_bi_chan_khi_thieu_du_lieu_bat_buoc() -> None:
    event = SignalEngine().generate(_warning_snapshot(100.0))

    assert event.action == "NO SIGNAL"
    assert event.signal_status == "NO SIGNAL"


def test_stop_loss_duoc_uu_tien_du_thieu_roe_market_va_khoi_ngoai() -> None:
    event = SignalEngine().generate(
        _warning_snapshot(94.0),
        position={"has_position": True, "entry_price": 100.0},
    )

    assert event.action == "SELL"
    assert event.signal_status == "ELIGIBLE"
    assert event.metadata["sell_trigger"] == "STOP_LOSS"


def test_take_profit_duoc_uu_tien_du_thieu_du_lieu_bo_tro() -> None:
    event = SignalEngine().generate(
        _warning_snapshot(111.0),
        position={"has_position": True, "entry_price": 100.0},
    )

    assert event.action == "SELL"
    assert event.metadata["sell_trigger"] == "TAKE_PROFIT"


def test_hold_kem_canh_bao_khi_thieu_du_lieu_bo_tro() -> None:
    event = SignalEngine().generate(
        _warning_snapshot(100.0),
        position={"has_position": True, "entry_price": 100.0},
    )

    assert event.action == "HOLD"
    assert event.signal_status == "ELIGIBLE"
    assert event.data_status == "DATA WARNING"
    assert any("bỏ qua kiểm tra Market Exit" in reason for reason in event.reasons)
    assert any("bỏ qua kiểm tra Foreign Net Sell" in reason for reason in event.reasons)


def test_gia_realtime_cu_chan_ca_tin_hieu_bao_ve_vi_the() -> None:
    event = SignalEngine().generate(
        _warning_snapshot(94.0, age_minutes=16.0),
        position={"has_position": True, "entry_price": 100.0},
    )

    assert event.action == "NO SIGNAL"
    assert event.signal_status == "NO SIGNAL"
    assert any("giá realtime quá cũ" in reason for reason in event.reasons)
