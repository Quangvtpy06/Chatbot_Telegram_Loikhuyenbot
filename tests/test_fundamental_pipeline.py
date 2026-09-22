"""Kiểm thử các fallback dữ liệu cơ bản dùng cho Data Quality Gate."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pandas as pd
import pytest

from collectors_processing.analytics import AnalyticsPipeline, PipelineConfig
from collectors_processing.dnse_api_crawl import (
    _canonical_financial_row,
    _financial_matrix_to_rows,
    save_latest_realtime_rows,
)
from collectors_processing.hybrid_fundamental import (
    PROJECT_ROOT,
    _has_minimum_quarter_periods,
    _resolve_data_path,
)


def _pipeline() -> AnalyticsPipeline:
    workspace = Path("tests")
    return AnalyticsPipeline(
        PipelineConfig(
            input_dir=workspace,
            output_dir=workspace / "analytics",
            database_path=workspace / "analytics.sqlite",
        )
    )


def test_missing_published_date_is_estimated_from_period() -> None:
    pipeline = _pipeline()
    pipeline.stats["fundamentals"] = {}
    frame = pd.DataFrame(
        [
            {
                "symbol": "BID",
                "period_type": "quarter",
                "period": "2025-Q1",
                "source": "KBS",
                "published_date": None,
                "net_profit": 10.0,
                "owners_equity": 100.0,
            }
        ]
    )

    cleaned = pipeline._clean_fundamentals(frame, "fundamentals")

    assert cleaned.iloc[0]["published_date"] == "2025-04-30T00:00:00+00:00"


def test_crawler_persists_estimated_published_date() -> None:
    row = _canonical_financial_row(
        {
            "symbol": "VCB",
            "period_type": "quarter",
            "period": "2025-Q2",
            "source": "KBS",
            "published_date": None,
        },
        collected_at="2025-08-01T00:00:00+00:00",
    )

    assert row["published_date"] == "2025-07-30T00:00:00+00:00"


def test_kbs_canonical_row_normalizes_ratios_and_balance_equity() -> None:
    row = _canonical_financial_row(
        {
            "symbol": "HPG",
            "period_type": "quarter",
            "period": "2026-Q2",
            "source": "KBS",
            "roe": 3.19,
            "roa": 1.63,
            "debt_to_equity": 75.94,
            "gross_margin": 16.72,
            "net_margin": 11.02,
            "owners_equity": 14.08,
            "owners_equity_2": 127_516_012_099_000,
        },
        collected_at="2026-09-22T00:00:00+00:00",
    )

    assert row["roe"] == 0.0319
    assert row["roa"] == 0.0163
    assert row["debt_to_equity"] == 0.7594
    assert row["gross_margin"] == 0.1672
    assert row["net_margin"] == pytest.approx(0.1102)
    assert row["owners_equity"] == 127_516_012_099_000


def test_kbs_bank_equity_uses_capital_and_reserves() -> None:
    row = _canonical_financial_row(
        {
            "symbol": "BID",
            "period_type": "quarter",
            "period": "2026-Q2",
            "source": "KBS",
            "owners_equity": 21.34,
            "capital_and_reserves": 167_986_701_000_000,
        },
        collected_at="2026-09-22T00:00:00+00:00",
    )

    assert row["owners_equity"] == 167_986_701_000_000


def test_financial_matrix_drops_duplicate_suffix_periods() -> None:
    frame = pd.DataFrame(
        {
            "item": ["ROE"],
            "item_en": ["ROE"],
            "item_id": ["roe"],
            "2026-Q2": [3.19],
            "2025-Q4_1": [14.08],
        }
    )

    rows = _financial_matrix_to_rows(frame, "HPG", "quarter", "ratio", "KBS")

    assert [row["period"] for row in rows] == ["2026-Q2"]


def test_realtime_cache_keeps_other_symbols() -> None:
    path = Path("tests") / f".realtime_cache_{uuid.uuid4().hex}.jsonl"
    try:
        save_latest_realtime_rows([{"symbol": "BID", "matchPrice": 50.0}], path)
        save_latest_realtime_rows([{"symbol": "HPG", "matchPrice": 21.1}], path)
        save_latest_realtime_rows([{"symbol": "BID", "matchPrice": 51.0}], path)

        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        assert rows == [
            {"symbol": "BID", "matchPrice": 51.0},
            {"symbol": "HPG", "matchPrice": 21.1},
        ]
    finally:
        path.unlink(missing_ok=True)


def test_roe_ttm_uses_five_consecutive_quarters() -> None:
    pipeline = _pipeline()
    pipeline.reconciled_fundamentals = pd.DataFrame(
        {
            "symbol": ["BID"] * 5,
            "period_type": ["quarter"] * 5,
            "period": ["2024-Q4", "2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4"],
            "net_profit": [8.0, 10.0, 10.0, 10.0, 10.0],
            "owners_equity": [100.0, 105.0, 110.0, 115.0, 120.0],
        }
    )

    result = pipeline._calculate_roe_ttm().iloc[0]

    assert result["net_profit_ttm"] == 40.0
    assert result["average_equity_ttm"] == 110.0
    assert result["roe_ttm"] == 40.0 / 110.0
    assert result["roe_ttm_start_period"] == "2025-Q1"
    assert result["roe_ttm_end_period"] == "2025-Q4"


def test_roe_ttm_falls_back_to_four_consecutive_quarters() -> None:
    pipeline = _pipeline()
    pipeline.reconciled_fundamentals = pd.DataFrame(
        {
            "symbol": ["HPG"] * 4,
            "period_type": ["quarter"] * 4,
            "period": ["2025-Q3", "2025-Q4", "2026-Q1", "2026-Q2"],
            "net_profit": [6.0, 3.0, 4.0, 4.0],
            "owners_equity": [110.0, 115.0, 120.0, 130.0],
        }
    )

    result = pipeline._calculate_roe_ttm().iloc[0]

    assert result["net_profit_ttm"] == 17.0
    assert result["average_equity_ttm"] == 120.0
    assert result["roe_ttm"] == 17.0 / 120.0
    assert result["roe_ttm_method"] == "SUM_4Q_NET_PROFIT/AVG_FIRST_END_EQUITY_APPROX"


def test_quarter_cache_requires_at_least_five_periods() -> None:
    path = Path("tests") / f".quarter_cache_{uuid.uuid4().hex}.csv"
    try:
        pd.DataFrame(
            {"period": ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4"]}
        ).to_csv(path, index=False)
        assert not _has_minimum_quarter_periods(path, 5)

        pd.DataFrame(
            {
                "period": [
                    "2024-Q4",
                    "2025-Q1",
                    "2025-Q2",
                    "2025-Q3",
                    "2025-Q4",
                ]
            }
        ).to_csv(path, index=False)
        assert _has_minimum_quarter_periods(path, 5)
    finally:
        path.unlink(missing_ok=True)


def test_hybrid_relative_data_paths_are_anchored_to_project() -> None:
    expected = (PROJECT_ROOT / "data").resolve()

    assert _resolve_data_path("data") == expected
    assert _resolve_data_path("dnse/data") == expected
