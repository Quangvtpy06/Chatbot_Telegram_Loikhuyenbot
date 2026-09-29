"""Kiểm tra khối lượng từ DNSE tới tin nhắn và loại bỏ mã VNPT nhập nhầm."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest

from collectors_processing.analytics import AnalyticsPipeline
from telegram_bot.analytics_repository import AnalyticsRepository
from telegram_bot.config import BotConfig, _parse_symbols
from telegram_bot.data_refresh import RealtimeRefreshService
from telegram_bot.formatter import format_check
from telegram_bot.handlers import BotHandlers
from telegram_bot.models import SignalView
from telegram_bot.subscriber_repository import SubscriberRepository


@pytest.fixture(autouse=True)
def danh_muc_dnse_gia_lap(monkeypatch):
    """Kiểm thử độc lập mạng và thông tin xác thực thật."""
    valid = lambda: frozenset({"FPT", "HAG", "HPG"})
    monkeypatch.setattr("telegram_bot.handlers.equity_symbols", valid)
    monkeypatch.setattr("telegram_bot.data_refresh.equity_symbols", valid)


@pytest.fixture
def tmp_path():
    """Tạo thư mục kiểm thử trong workspace, giữ quyền kế thừa trên Windows."""
    directory = Path("work") / f"test_bot_volume_{uuid4().hex}"
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()


@pytest.mark.parametrize("symbol,raw_volume,shares", [
    ("HAG", 176_190, 1_761_900),
    ("FPT", 355_750, 3_557_500),
    ("HPG", 6_000_000, 60_000_000),
])
def test_khoi_luong_chi_quy_doi_mot_lan(tmp_path, symbol, raw_volume, shares):
    raw = pd.DataFrame([{
        "symbol": symbol,
        "_event_time": pd.Timestamp("2026-09-23 14:45:02"),
        "_crawled_time": pd.Timestamp("2026-09-23 07:45:03", tz="UTC"),
        "matchPrice": 14.0, "matchQtty": 10, "avgPrice": 14.0,
        "totalVolumeTraded": raw_volume,
        "openPrice": 14.0, "highestPrice": 14.0, "lowestPrice": 14.0,
    }])
    normalized = AnalyticsPipeline._normalize_realtime(raw)
    assert normalized.iloc[0]["total_volume"] == shares

    database = tmp_path / "analytics.sqlite"
    snapshot = pd.DataFrame([{
        "symbol": symbol,
        "total_volume": int(normalized.iloc[0]["total_volume"]),
        "latest_volume": shares,
        "latest_close": 14.0,
        "realtime_price": 14.0,
        "price_as_of": "2026-09-23",
    }])
    with closing(sqlite3.connect(database)) as connection:
        snapshot.to_sql("stock_snapshot", connection, index=False)
    view, event = AnalyticsRepository(database, tmp_path / "quality.json").get_signal_view(symbol)
    assert event is not None
    assert view.metrics["volume"] == shares
    assert f"{shares:,.0f} cp" in format_check(view, volume=view.metrics["volume"])


@pytest.mark.parametrize("metrics,volume,expected", [
    ({"total_volume": 1_761_900}, None, "1,761,900 cp"),
    ({"latest_volume": 3_557_500}, None, "3,557,500 cp"),
    ({"volume": 1_761_900}, None, "1,761,900 cp"),
    ({"total_volume": 1_761_900}, 0, "0 cp"),
])
def test_hien_thi_du_phong_giu_nguyen_don_vi_cp(metrics, volume, expected):
    view = SignalView("HAG", "NO SIGNAL", "OK", "NO SIGNAL", metrics=metrics)
    assert expected in format_check(view, volume=volume)


@pytest.mark.parametrize("realtime,expected", [(1200, 1200), (0, 0), (None, 900000)])
def test_uu_tien_khoi_luong_trong_phien(tmp_path, realtime, expected):
    """Khối lượng đầu phiên không được thay bằng số lớn hơn của phiên trước."""
    database = tmp_path / "volume.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        pd.DataFrame([{
            "symbol": "FPT", "total_volume": realtime,
            "latest_volume": 900000, "latest_close": 90.0,
            "realtime_price": 91.0, "price_as_of": "2026-09-29",
        }]).to_sql("stock_snapshot", connection, index=False)
    view, _ = AnalyticsRepository(database, tmp_path / "quality.json").get_signal_view("FPT")
    assert view.metrics["volume"] == expected
    assert f"{expected:,.0f} cp" in format_check(view)


def test_market_hien_thi_tong_gia_tri_va_thoi_gian():
    """Giữ đơn vị cp, tỷ đồng và thời điểm của snapshot chỉ số."""
    from telegram_bot.formatter import format_market
    text = format_market({
        "c": 1777.73, "prev_c": 1780.68, "v": 530476411,
        "value_billion": 12179.83027171, "as_of": "2026-09-29 14:45:05",
    }, {})
    assert "530,476,411 cp" in text
    assert "12,179.83 tỷ đồng" in text
    assert "2026-09-29 14:45:05" in text
    assert "VN30:</b> Không có dữ liệu" in text


def test_check_khong_tao_bieu_do(monkeypatch):
    """Lệnh check chỉ gửi văn bản sau khi phân tích."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from telegram_bot import chart_service
    handler = object.__new__(BotHandlers)
    handler._authorize = AsyncMock(return_value=True)
    handler._save_user = Mock()
    handler._on_demand_fetch_and_analyze = AsyncMock(return_value=(
        SignalView("FPT", "NO SIGNAL", "OK", "NO SIGNAL"), None,
    ))
    handler._reply = AsyncMock()
    chart = Mock(side_effect=AssertionError("Không được vẽ trong /check"))
    monkeypatch.setattr(chart_service, "generate_bollinger_chart", chart)
    message = SimpleNamespace(reply_photo=AsyncMock())
    asyncio.run(handler.check(SimpleNamespace(effective_message=message), SimpleNamespace(args=["FPT"])))
    handler._reply.assert_awaited_once()
    chart.assert_not_called()
    message.reply_photo.assert_not_awaited()


def test_vnpt_bi_loai_khoi_watchlist_va_khong_them_lai(tmp_path):
    database = tmp_path / "bot.sqlite"
    repository = SubscriberRepository(database)
    repository.upsert_subscriber(1, None, None)
    repository.add_to_watchlist(1, "FPT")
    # Giả lập watchlist cũ trước khi có kiểm tra mã nhập nhầm.
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO watchlists (chat_id, symbol, created_at) VALUES (1, 'VNPT', '2026-09-23')"
        )
        connection.commit()
    repository = SubscriberRepository(database)
    assert repository.list_watchlist(1) == ["FPT"]
    with pytest.raises(ValueError, match="VNPT"):
        repository.add_to_watchlist(1, " vnpt ")
    assert BotHandlers._symbol(["VNPT"]) is None
    assert BotHandlers._symbol(["FPT"]) == "FPT"
    assert _parse_symbols("VNPT,FPT,HAG") == ("FPT", "HAG")


def test_vnpt_khong_duoc_quet_tu_snapshot_cu(tmp_path):
    database = tmp_path / "analytics.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE stock_snapshot (symbol TEXT)")
        connection.executemany("INSERT INTO stock_snapshot VALUES (?)", [("VNPT",), ("FPT",)])
        connection.commit()
    repository = AnalyticsRepository(database, tmp_path / "quality.json")
    assert repository.list_snapshot_symbols() == ["FPT"]
    subscribers = SubscriberRepository(tmp_path / "bot.sqlite")
    config = BotConfig(
        token="test", allowed_chat_ids=frozenset(), analytics_database=database,
        quality_report=tmp_path / "quality.json", bot_database=tmp_path / "bot.sqlite",
        data_dir=tmp_path / "data", analytics_output_dir=tmp_path / "analytics",
    )
    assert RealtimeRefreshService(config, subscribers)._symbols() == ("FPT",)
    configured = replace(config, realtime_symbols=("VNPT", "HAG"))
    assert RealtimeRefreshService(configured, subscribers)._symbols() == ("HAG",)
