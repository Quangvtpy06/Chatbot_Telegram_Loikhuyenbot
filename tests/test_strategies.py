"""Kiểm tra Entry Gate và Risk/Exit Gate của chiến lược."""

from __future__ import annotations

from signals.signal_engine import SignalEngine


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

