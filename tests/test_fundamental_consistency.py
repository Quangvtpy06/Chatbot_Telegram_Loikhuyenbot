"""Kiểm tra cơ bản từ nguồn tới snapshot và hai lệnh của bot cho mọi mã."""

from pathlib import Path
import sqlite3
from contextlib import closing
from uuid import uuid4
from unittest.mock import Mock, AsyncMock

import pandas as pd
import pytest

from collectors_processing.analytics import AnalyticsPipeline, PipelineConfig
from collectors_processing.fundamental_schema import normalize_fundamental_fields
from collectors_processing.dnse_api_crawl import _canonical_financial_row
from telegram_bot.analytics_repository import AnalyticsRepository
from telegram_bot.formatter import format_check, format_signal_single


@pytest.mark.parametrize("symbol", ["MSB", "FPT", "PC1", "A32", "NEW"])
def test_alias_di_xuyen_suot_den_check_va_signal(symbol):
    """Không dựa vào danh sách mã cố định hoặc cấu trúc riêng của một doanh nghiệp."""
    directory = Path("work") / f"fundamental_consistency_{uuid4().hex}"
    directory.mkdir(parents=True)
    try:
        pipeline = AnalyticsPipeline(PipelineConfig(input_dir=directory, output_dir=directory,
                                                    database_path=directory / "test.sqlite"))
        raw = pd.DataFrame([{"symbol": symbol, "period": "2026-Q2", "period_type": "quarter",
                             "source": "KBS", "pe": None, "pe_ratio": 10., "pb_ratio": 1.2,
                             "roe": .18, "debt_to_equity": .5}])
        pipeline.stats["fundamentals"] = {}
        cleaned = pipeline._clean_fundamentals(raw, "fundamentals")
        pipeline.cleaned["fundamentals"] = cleaned
        # Screening thiếu ô không được che dữ liệu đầy đủ cùng nguồn trong báo cáo.
        pipeline.cleaned["screening"] = cleaned.assign(pe=float("nan"), pb=float("nan"))
        pipeline.reconcile_sources()
        snapshot = pipeline._latest_fundamentals()
        snapshot["sector"] = "Sản xuất thực phẩm"
        snapshot["latest_close"] = 14.
        snapshot["realtime_price"] = 14.
        snapshot["price_as_of"] = "2026-09-29"
        with closing(sqlite3.connect(directory / "test.sqlite")) as connection:
            snapshot.to_sql("stock_snapshot", connection, index=False)
        repository = AnalyticsRepository(directory / "test.sqlite", directory / "quality.json")
        view, event = repository.get_signal_view(symbol, user_settings={"investment_mode": "LONG_TERM"})
        assert event.action == "BUY"
        assert "missing_fundamental_fields" not in event.metadata
        assert "P/E: <code>10.0</code>" in format_check(view)
        assert "P/B: <code>1.2</code>" in format_check(view)
        assert event.metadata["pe"] == view.metrics["pe_quarter"]
        assert event.metadata["pb"] == view.metrics["pb_quarter"]
        assert "ĐANG ĐỒNG BỘ" not in format_signal_single(view)
    finally:
        for file in directory.iterdir():
            file.unlink()
        directory.rmdir()


def test_alias_bu_tung_o_va_khong_ghi_de_so_lieu_chuan():
    frame = pd.DataFrame({"pe": [8., None, float("inf")], "pe_ratio": [9., 10., 11.]})
    assert normalize_fundamental_fields(frame).pe.tolist() == [8., 10., 11.]
    row = _canonical_financial_row({"pe_ratio": 10., "pb_ratio": 1.2}, "2026-09-29")
    assert row["pe"] == 10. and row["pb"] == 1.2


def test_du_phong_theo_nam_va_thong_bao_thieu_chinh_xac():
    from telegram_bot.models import SignalView
    view = SignalView("ABC", "NO SIGNAL", "DATA WARNING", "NO SIGNAL",
                      metrics={"pe_quarter": None, "pe_year": 7., "pb_year": 1.1},
                      reasons=("Chưa đủ dữ liệu cơ bản: ROE",))
    text = format_check(view)
    assert "P/E: <code>7.0</code>" in text
    assert "P/B: <code>1.1</code>" in text
    assert "ROE: chưa có dữ liệu" in text


def test_signal_dai_han_thu_dong_bo_khi_thieu_co_ban():
    """Có giá rồi vẫn phải thử nạp phần cơ bản thiếu trước khi trả kết quả."""
    import asyncio
    from types import SimpleNamespace
    from telegram_bot.handlers import BotHandlers
    from telegram_bot.models import SignalView
    handler = object.__new__(BotHandlers)
    handler._authorize = AsyncMock(return_value=True)
    handler._save_user = Mock()
    handler._validated_symbol = AsyncMock(return_value="ABC")
    handler.subscribers = Mock()
    handler.subscribers.get_user_settings.return_value = {"investment_mode": "LONG_TERM"}
    missing = SignalView("ABC", "NO SIGNAL", "DATA WARNING", "NO SIGNAL", price=10.,
                         metadata={"missing_fundamental_fields": ["P/E"]})
    ready = SignalView("ABC", "HOLD", "OK", "ELIGIBLE", price=10.,
                       metadata={"investment_mode": "LONG_TERM"})
    handler.analytics = Mock()
    handler.analytics.get_signal_view.return_value = (missing, None)
    handler._on_demand_fetch_and_analyze = AsyncMock(return_value=(ready, None))
    handler._reply = AsyncMock()
    handler.risk_gate = None
    handler.config = SimpleNamespace()
    asyncio.run(handler.signal(SimpleNamespace(effective_chat=SimpleNamespace(id=1)),
                               SimpleNamespace(args=["ABC"])))
    handler._on_demand_fetch_and_analyze.assert_awaited_once()
    assert handler._on_demand_fetch_and_analyze.call_args.kwargs["user_settings"]["investment_mode"] == "LONG_TERM"
    assert "Dài hạn" in handler._reply.call_args.args[1]
