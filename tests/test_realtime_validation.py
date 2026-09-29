"""Kiểm tra xác thực mã và chỉ báo trên nến ngày đang hình thành."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pandas as pd
import pytest

from collectors_processing.analytics import AnalyticsPipeline, LOCAL_TIMEZONE
from telegram_bot.handlers import BotHandlers


@pytest.mark.parametrize("command,args", [
    ("check", ["VNPT"]), ("check", ["ZZZ"]), ("chart", ["ABCD"]),
    ("block", ["WATCHLIST"]), ("watch", ["VNPT"]),
    ("watchlist", ["add", "VNPT"]),
])
def test_ma_khong_ton_tai_khong_dong_bo(monkeypatch, command, args):
    monkeypatch.setattr("telegram_bot.handlers.equity_symbols", lambda: frozenset({"FPT", "PC1"}))
    handler = object.__new__(BotHandlers)
    handler._authorize = AsyncMock(return_value=True)
    handler._save_user = Mock()
    handler._reply = AsyncMock()
    handler._on_demand_fetch_and_analyze = AsyncMock()
    handler.subscribers = Mock()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1))
    asyncio.run(getattr(handler, command)(update, SimpleNamespace(args=args)))
    assert "Không tồn tại mã" in handler._reply.call_args.args[1]
    handler._on_demand_fetch_and_analyze.assert_not_awaited()
    handler.subscribers.add_to_watchlist.assert_not_called()


def test_loi_danh_muc_khong_bi_coi_la_ma_khong_ton_tai(monkeypatch):
    monkeypatch.setattr("telegram_bot.handlers.equity_symbols", Mock(side_effect=RuntimeError("Mất mạng")))
    handler = object.__new__(BotHandlers)
    handler._reply = AsyncMock()
    assert asyncio.run(handler._validated_symbol(None, ["FPT"])) is None
    assert "Chưa xác thực" in handler._reply.call_args.args[1]


def _du_lieu():
    now = pd.Timestamp.now(tz=LOCAL_TIMEZONE)
    days = pd.date_range(end=now.normalize() - pd.Timedelta(days=1), periods=80)
    close = 100 + np.arange(80) * .2 + np.sin(np.arange(80))
    history = pd.DataFrame({
        "symbol": "FPT", "interval": "1D", "timestamp_local": days,
        "timestamp_utc": days.tz_convert("UTC"), "date": days.strftime("%Y-%m-%d"),
        "open": close, "high": close + 1, "low": close - 1,
        "close": close, "volume": 900000,
    })
    realtime = pd.DataFrame([{
        "symbol": "FPT", "timestamp_local": now - pd.Timedelta(seconds=1),
        "match_price": 130., "total_volume": 1200,
        "open": 120., "high": 131., "low": 119.,
    }])
    return history, realtime


def test_chi_bao_dung_gia_phien_hien_tai_va_khong_lap_nen():
    history, realtime = _du_lieu()
    original = history.copy(deep=True)
    merged = AnalyticsPipeline._history_with_realtime(history, realtime)
    assert len(merged) == len(history) + 1
    assert merged.iloc[-1]["volume"] == 1200
    assert merged.iloc[-1]["close"] == 130
    pd.testing.assert_frame_equal(history, original)
    again = AnalyticsPipeline._history_with_realtime(merged, realtime)
    assert len(again) == len(merged)
    metrics = AnalyticsPipeline._analyze_price_history(merged).iloc[0]
    close = pd.concat([history.close, pd.Series([130.])], ignore_index=True)
    ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    gain = close.diff().clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    loss = (-close.diff().clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    assert metrics.sma_20 == pytest.approx(close.tail(20).mean())
    assert metrics.ema_20 == pytest.approx(close.ewm(span=20, adjust=False).mean().iloc[-1])
    assert metrics.macd == pytest.approx((ema12 - ema26).iloc[-1])
    assert metrics.rsi_14 == pytest.approx((100 - 100 / (1 + gain / loss)).iloc[-1])
    old = AnalyticsPipeline._analyze_price_history(history).iloc[0]
    for key in ("sma_20", "ema_20", "macd", "rsi_14"):
        assert metrics[key] != pytest.approx(old[key])


def test_snapshot_phien_cu_khong_ghi_de_lich_su():
    history, realtime = _du_lieu()
    realtime["timestamp_local"] -= pd.Timedelta(days=1)
    pd.testing.assert_frame_equal(AnalyticsPipeline._history_with_realtime(history, realtime), history)


def test_refresh_loc_ma_sai_truoc_khi_goi_dnse(monkeypatch):
    from telegram_bot.data_refresh import RealtimeRefreshService
    monkeypatch.setattr("telegram_bot.data_refresh.equity_symbols", lambda: frozenset({"FPT"}))
    client = Mock()
    constructor = Mock(return_value=client)
    monkeypatch.setattr("telegram_bot.data_refresh.DNSEClient", constructor)
    service = object.__new__(RealtimeRefreshService)
    service._refresh_realtime(("VNPT", "WATCHLIST", "ZZZ"))
    constructor.assert_not_called()


def test_danh_muc_cache_va_loi_khong_cho_dong_bo(monkeypatch):
    from collectors_processing import symbol_registry as registry
    monkeypatch.setattr(registry, "_symbols", frozenset())
    monkeypatch.setattr(registry, "_expires_at", 0)
    client = Mock()
    client.list_symbols.return_value = ["FPT", "PC1"]
    monkeypatch.setattr(registry, "DNSEClient", Mock(return_value=client))
    registry.require_equity("PC1")
    with pytest.raises(ValueError, match="Không tồn tại"):
        registry.require_equity("VNPT")
    assert client.list_symbols.call_count == 1
    monkeypatch.setattr(registry, "_expires_at", 0)
    client.list_symbols.side_effect = RuntimeError("Mất mạng")
    with pytest.raises(RuntimeError):
        registry.require_equity("FPT")
