"""Pipeline xử lý dữ liệu chứng khoán sau khi thu thập từ DNSE/Vnstock.

Luồng xử lý:
    Làm sạch -> Chuẩn hóa -> Kiểm tra -> Lưu trữ -> Phân tích

Ví dụ chạy:
    python analytics.py
    python analytics.py --input-dir data_test_pipeline --output-dir data_test_analytics
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

LOGGER = logging.getLogger("dnse.analytics")
LOCAL_TIMEZONE = "Asia/Ho_Chi_Minh"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_ANALYTICS_DIR = DEFAULT_DATA_DIR / "analytics"


def _resolve_project_path(value: str | Path) -> Path:
    """Chuẩn hóa đường dẫn theo thư mục dnse và loại tiền tố dnse bị lặp."""

    path = Path(value).expanduser()
    if not path.is_absolute():
        parts = list(path.parts)
        if parts and parts[0].casefold() == PROJECT_ROOT.name.casefold():
            parts = parts[1:]
        path = PROJECT_ROOT.joinpath(*parts)
    resolved = path.resolve()
    duplicated_root = PROJECT_ROOT / PROJECT_ROOT.name
    if resolved.is_relative_to(duplicated_root):
        resolved = PROJECT_ROOT / resolved.relative_to(duplicated_root)
    return resolved


HISTORY_COLUMNS = [
    "symbol",
    "interval",
    "timestamp_utc",
    "timestamp_local",
    "date",
    "open",
    "high",
    "low",
    "ceiling_price",
    "floor_price",
    "close",
    "volume",
]

REALTIME_COLUMNS = [
    "symbol",
    "market_id",
    "board_id",
    "timestamp_local",
    "crawled_at_utc",
    "match_price",
    "match_quantity",
    "side",
    "average_price",
    "total_volume",
    "open",
    "high",
    "low",
]

FOREIGN_FLOW_COLUMNS = [
    "symbol",
    "trading_date",
    "timestamp",
    "foreign_buy_volume",
    "foreign_sell_volume",
    "foreign_net_volume",
    "foreign_net_buy_volume",
    "foreign_net_sell_volume",
    "foreign_buy_value",
    "foreign_sell_value",
    "foreign_net_value",
    "source",
    "collected_at",
]

FUNDAMENTAL_NUMERIC_COLUMNS = [
    "asset_turnover",
    "current_ratio",
    "debt_to_equity",
    "eps",
    "eps_diluted",
    "eps_ttm",
    "ev_to_ebitda",
    "financial_leverage",
    "gross_margin",
    "market_cap",
    "net_margin",
    "net_profit",
    "net_sales",
    "revenue",
    "owners_equity",
    "pb",
    "pe",
    "ps",
    "quick_ratio",
    "roa",
    "roe",
    "roic",
]

CORE_FUNDAMENTAL_COLUMNS = ["pe", "pb", "roe", "roa", "debt_to_equity"]
SOURCE_COMPARISON_COLUMNS = ["pe", "pb", "roe", "roa", "debt_to_equity", "eps", "market_cap"]
SOURCE_PRIORITY = {"VNFINANCIALDATA": 0, "KBS": 1, "VCI": 2}


@dataclass(frozen=True)
class PipelineConfig:
    """Cấu hình đường dẫn cho pipeline phân tích."""

    input_dir: Path
    output_dir: Path
    database_path: Path
    fail_on_quality_errors: bool = False
    expected_symbols: tuple[str, ...] = ()
    max_price_age_days: float = 7.0
    max_realtime_age_minutes: float = 1_440.0
    max_quarter_age_days: int = 200
    max_year_age_days: int = 550
    source_tolerance: float = 0.20
    minimum_history_rows: int = 50
    minimum_fundamental_coverage: float = 0.60
    require_realtime_for_signal: bool = True

    def __post_init__(self) -> None:
        if self.max_price_age_days < 0 or self.max_realtime_age_minutes < 0:
            raise ValueError("Ngưỡng độ cũ của dữ liệu không được âm.")
        if self.max_quarter_age_days < 0 or self.max_year_age_days < 0:
            raise ValueError("Ngưỡng tuổi báo cáo tài chính không được âm.")
        if not 0 <= self.source_tolerance <= 1:
            raise ValueError("source_tolerance phải nằm trong khoảng 0..1.")
        if not 0 <= self.minimum_fundamental_coverage <= 1:
            raise ValueError("minimum_fundamental_coverage phải nằm trong khoảng 0..1.")
        if self.minimum_history_rows < 1:
            raise ValueError("minimum_history_rows phải lớn hơn 0.")


class AnalyticsPipeline:
    """Xử lý và phân tích toàn bộ dữ liệu do ``dnse_api_crawl.py`` tạo ra."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.raw: dict[str, pd.DataFrame] = {}
        self.cleaned: dict[str, pd.DataFrame] = {}
        self.rejected: dict[str, pd.DataFrame] = {}
        self.analysis: dict[str, pd.DataFrame] = {}
        self.stats: dict[str, dict[str, Any]] = {}
        self.quality_report: dict[str, Any] = {}
        self.precheck_report: dict[str, Any] = {}
        self.ingestion_errors: list[dict[str, str]] = []
        self.crawl_status: dict[str, Any] = {}
        self.reconciled_fundamentals = pd.DataFrame()
        self.source_audit = pd.DataFrame()

    def run(self) -> dict[str, Any]:
        """Chạy tuần tự năm bước và trả về báo cáo chất lượng."""

        LOGGER.info("Bắt đầu pipeline với dữ liệu tại %s", self.config.input_dir)
        self.load()
        self.precheck()
        self.clean()
        self.normalize()
        self.reconcile_sources()
        self.validate()
        self.analyze()
        self.store()

        status = self.quality_report.get("status", "UNKNOWN")
        if self.config.fail_on_quality_errors and status == "FAIL":
            raise RuntimeError("Dữ liệu không đạt kiểm tra chất lượng. Xem quality_report.json.")
        LOGGER.info("Hoàn tất pipeline. Trạng thái chất lượng: %s", status)
        return self.quality_report

    # 1. Nạp dữ liệu -------------------------------------------------------
    def load(self) -> None:
        """Nạp lịch sử, realtime và dữ liệu cơ bản từ thư mục crawler."""

        if not self.config.input_dir.exists():
            raise FileNotFoundError(
                f"Không tìm thấy thư mục dữ liệu: {self.config.input_dir}. "
                "Hãy chạy dnse_api_crawl.py trước hoặc truyền --input-dir."
            )

        history_files = sorted((self.config.input_dir / "history").glob("*.csv"))
        market_history_files = sorted(
            (self.config.input_dir / "market" / "history").glob("*.csv")
        )
        realtime_files = sorted((self.config.input_dir / "realtime").glob("*.jsonl"))
        foreign_files = sorted((self.config.input_dir / "foreign").glob("*.csv"))
        screening_files = sorted((self.config.input_dir / "fundamental").glob("screening_*.csv"))
        fundamental_files = sorted(
            path
            for path in (self.config.input_dir / "fundamental").glob("*/*_fundamentals.csv")
            if path.is_file()
        )

        self.raw["history"] = self._read_history_files(history_files)
        self.raw["market_history"] = self._read_history_files(market_history_files)
        self.raw["realtime"] = self._read_jsonl_files(realtime_files)
        self.raw["foreign_flow"] = self._read_csv_files(
            foreign_files, include_source_file=True
        )
        self.raw["screening"] = self._read_csv_files(screening_files, include_source_file=True)
        self.raw["fundamentals"] = self._read_csv_files(fundamental_files, include_source_file=True)
        self._load_crawl_status()

        for name, frame in self.raw.items():
            self.stats[name] = {
                "input_rows": int(len(frame)),
                "input_files": int(frame["_source_file"].nunique())
                if "_source_file" in frame.columns
                else 0,
            }
            LOGGER.info("Đã nạp %-12s: %s dòng", name, len(frame))

    def _read_csv_files(self, files: list[Path], include_source_file: bool = False) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for path in files:
            try:
                frame = pd.read_csv(path, low_memory=False)
                if include_source_file:
                    frame["_source_file"] = str(path)
                frames.append(frame)
            except (OSError, UnicodeError, pd.errors.ParserError) as exc:
                LOGGER.warning("Bỏ qua file CSV không đọc được %s: %s", path, exc)
                self.ingestion_errors.append(
                    {"dataset": "csv", "file": str(path), "error": f"{type(exc).__name__}: {exc}"}
                )
        return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()

    def _read_history_files(self, files: list[Path]) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for path in files:
            try:
                frame = pd.read_csv(path, low_memory=False)
                frame["_source_file"] = str(path)
                frame["_interval"] = self._interval_from_filename(path)
                frames.append(frame)
            except (OSError, UnicodeError, pd.errors.ParserError) as exc:
                LOGGER.warning("Bỏ qua file lịch sử không đọc được %s: %s", path, exc)
                self.ingestion_errors.append(
                    {"dataset": "history", "file": str(path), "error": f"{type(exc).__name__}: {exc}"}
                )
        return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()

    def _read_jsonl_files(self, files: list[Path]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for path in files:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                            row["_source_file"] = str(path)
                            rows.append(row)
                        except json.JSONDecodeError as exc:
                            LOGGER.warning("JSON lỗi tại %s:%s: %s", path, line_number, exc)
                            self.ingestion_errors.append(
                                {
                                    "dataset": "realtime",
                                    "file": f"{path}:{line_number}",
                                    "error": f"JSONDecodeError: {exc}",
                                }
                            )
            except OSError as exc:
                LOGGER.warning("Bỏ qua file realtime không đọc được %s: %s", path, exc)
                self.ingestion_errors.append(
                    {
                        "dataset": "realtime",
                        "file": str(path),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return pd.DataFrame(rows)

    def _load_crawl_status(self) -> None:
        """Đọc trạng thái crawler nếu crawler hoặc bộ lập lịch có ghi lại."""

        for filename in ["crawl_status.json", "api_status.json"]:
            path = self.config.input_dir / filename
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    self.crawl_status = payload
                    crawl_state = str(payload.get("status", "")).strip().lower()
                    if crawl_state in {"failed", "error", "partial", "disconnected"}:
                        self.ingestion_errors.append(
                            {
                                "dataset": "api",
                                "file": str(path),
                                "error": f"Crawler báo trạng thái {crawl_state}",
                            }
                        )
                    errors = payload.get("errors", [])
                    if isinstance(errors, list):
                        for error in errors:
                            if isinstance(error, dict):
                                self.ingestion_errors.append(
                                    {
                                        "dataset": str(error.get("dataset", "api")),
                                        "file": str(path),
                                        "error": str(error.get("error", error.get("message", "API lỗi"))),
                                    }
                                )
                return
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                self.ingestion_errors.append(
                    {"dataset": "api_status", "file": str(path), "error": f"{type(exc).__name__}: {exc}"}
                )

    @staticmethod
    def _interval_from_filename(path: Path) -> str:
        match = re.search(r"_([^_]+)$", path.stem)
        return match.group(1).upper() if match else "UNKNOWN"

    # Kiểm tra ban đầu -----------------------------------------------------
    def precheck(self) -> None:
        """Ghi nhận lỗi đầu vào trước khi sửa hoặc loại bỏ dữ liệu."""

        duplicate_keys = {
            "history": ["symbol", "t"],
            "market_history": ["symbol", "t"],
            "realtime": ["symbol", "time", "matchPrice", "matchQtty", "side"],
            "foreign_flow": ["symbol", "trading_date", "source"],
            "screening": ["symbol", "period_type", "period", "source"],
            "fundamentals": ["symbol", "period_type", "period", "source"],
        }
        datasets: dict[str, Any] = {}
        for name, frame in self.raw.items():
            keys = [column for column in duplicate_keys[name] if column in frame.columns]
            duplicate_rows = int(frame.duplicated(keys, keep=False).sum()) if keys else 0
            null_counts = {
                str(column): int(count)
                for column, count in frame.isna().sum().items()
                if int(count) > 0 and not str(column).startswith("_")
            }
            datasets[name] = {
                "rows": int(len(frame)),
                "duplicate_rows_detected": duplicate_rows,
                "null_counts": null_counts,
            }

        self.precheck_report = {
            "datasets": datasets,
            "ingestion_errors": list(self.ingestion_errors),
            "crawl_status": self.crawl_status,
        }

    # 2. Làm sạch ----------------------------------------------------------
    def clean(self) -> None:
        """Xóa trùng, chuẩn hóa kiểu cơ bản và tách các dòng không hợp lệ."""

        self.cleaned["history"] = self._clean_history(self.raw["history"], "history")
        self.cleaned["market_history"] = self._clean_history(
            self.raw["market_history"], "market_history"
        )
        self.cleaned["realtime"] = self._clean_realtime(self.raw["realtime"])
        self.cleaned["foreign_flow"] = self._clean_foreign_flow(
            self.raw["foreign_flow"]
        )
        self.cleaned["screening"] = self._clean_fundamentals(self.raw["screening"], "screening")
        self.cleaned["fundamentals"] = self._clean_fundamentals(
            self.raw["fundamentals"], "fundamentals"
        )

    def _clean_history(self, frame: pd.DataFrame, dataset: str) -> pd.DataFrame:
        if frame.empty:
            self.rejected[dataset] = pd.DataFrame()
            return pd.DataFrame(columns=HISTORY_COLUMNS)

        data = frame.copy()
        rename = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
        data = data.rename(columns={key: value for key, value in rename.items() if key in data.columns})
        data["symbol"] = self._normalize_symbol(data.get("symbol"))
        data["interval"] = data.get("_interval", "UNKNOWN")

        for column in ["t", "open", "high", "low", "close", "volume"]:
            values = data[column] if column in data.columns else pd.Series(np.nan, index=data.index)
            data[column] = pd.to_numeric(values, errors="coerce")

        timestamp = pd.to_datetime(data["t"], unit="s", utc=True, errors="coerce")
        millisecond_mask = timestamp.isna() & data["t"].notna()
        if millisecond_mask.any():
            timestamp.loc[millisecond_mask] = pd.to_datetime(
                data.loc[millisecond_mask, "t"], unit="ms", utc=True, errors="coerce"
            )
        data["timestamp_utc"] = timestamp

        invalid = (
                data["symbol"].isna()
                | data["timestamp_utc"].isna()
                | data[["open", "high", "low", "close"]].isna().any(axis=1)
                | (data[["open", "high", "low", "close"]] <= 0).any(axis=1)
                | data["volume"].lt(0)
                | data["high"].lt(data["low"])
                | data["open"].lt(data["low"])
                | data["open"].gt(data["high"])
                | data["close"].lt(data["low"])
                | data["close"].gt(data["high"])
        )
        rejected = data.loc[invalid].copy()
        rejected["reject_reason"] = "OHLCV hoặc thời gian/mã cổ phiếu không hợp lệ"
        self.rejected[dataset] = rejected

        valid = data.loc[~invalid].copy()
        before = len(valid)
        valid = valid.drop_duplicates(["symbol", "interval", "timestamp_utc"], keep="last")
        self.stats[dataset]["duplicate_rows_removed"] = before - len(valid)
        return valid

    def _clean_realtime(self, frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            self.rejected["realtime"] = pd.DataFrame()
            return pd.DataFrame(columns=REALTIME_COLUMNS)

        data = frame.copy()
        data["symbol"] = self._normalize_symbol(data.get("symbol"))
        realtime_aliases = {
            "matchPrice": "close",
            "matchQtty": "match_quantity",
            "avgPrice": "average_price",
            "totalVolumeTraded": "volume",
            "openPrice": "open",
            "highestPrice": "high",
            "lowestPrice": "low",
            "time": "timestamp",
            "crawled_at_utc": "collected_at",
        }
        for target, fallback in realtime_aliases.items():
            if target not in data.columns and fallback in data.columns:
                data[target] = data[fallback]
        numeric_columns = [
            "matchPrice",
            "matchQtty",
            "avgPrice",
            "totalVolumeTraded",
            "openPrice",
            "highestPrice",
            "lowestPrice",
        ]
        for column in numeric_columns:
            values = data[column] if column in data.columns else pd.Series(np.nan, index=data.index)
            data[column] = pd.to_numeric(values, errors="coerce")

        event_values = data["time"] if "time" in data.columns else pd.Series(pd.NaT, index=data.index)
        crawled_values = (
            data["crawled_at_utc"]
            if "crawled_at_utc" in data.columns
            else pd.Series(pd.NaT, index=data.index)
        )
        data["_event_time"] = pd.to_datetime(event_values, errors="coerce")
        data["_crawled_time"] = pd.to_datetime(crawled_values, utc=True, errors="coerce")
        invalid = (
                data["symbol"].isna()
                | data["matchPrice"].isna()
                | data["matchPrice"].le(0)
                | data["matchQtty"].lt(0)
                | data["_event_time"].isna()
                | (data["highestPrice"].notna() & data["lowestPrice"].notna() & data["highestPrice"].lt(
            data["lowestPrice"]))
                | (data["avgPrice"].notna() & data["avgPrice"].le(0))
                | (
                        data["totalVolumeTraded"].notna()
                        & data["matchQtty"].notna()
                        & data["totalVolumeTraded"].lt(data["matchQtty"])
                )
        )
        rejected = data.loc[invalid].copy()
        rejected["reject_reason"] = "Giá khớp, thời gian hoặc mã cổ phiếu không hợp lệ"
        self.rejected["realtime"] = rejected

        valid = data.loc[~invalid].copy()
        dedupe_columns = ["symbol", "_event_time", "matchPrice", "matchQtty", "side"]
        before = len(valid)
        valid = valid.drop_duplicates(dedupe_columns, keep="last")
        self.stats["realtime"]["duplicate_rows_removed"] = before - len(valid)
        return valid

    def _clean_foreign_flow(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Kiem tra khoi luong mua/ban khoi ngoai va phep tinh rong."""

        if frame.empty:
            self.rejected["foreign_flow"] = pd.DataFrame()
            return pd.DataFrame(columns=FOREIGN_FLOW_COLUMNS)

        data = frame.copy()
        data["symbol"] = self._normalize_symbol(data.get("symbol"))
        data["source"] = (
            data.get("source", pd.Series("UNKNOWN", index=data.index))
            .astype("string")
            .str.strip()
            .str.upper()
        )
        for column in [
            "foreign_buy_volume",
            "foreign_sell_volume",
            "foreign_net_volume",
            "foreign_net_buy_volume",
            "foreign_net_sell_volume",
            "foreign_buy_value",
            "foreign_sell_value",
            "foreign_net_value",
        ]:
            values = data[column] if column in data.columns else pd.Series(np.nan, index=data.index)
            data[column] = pd.to_numeric(values, errors="coerce")
        data["collected_at"] = pd.to_datetime(
            data.get("collected_at"), utc=True, errors="coerce"
        )
        raw_timestamp = data.get("timestamp", pd.Series(pd.NaT, index=data.index))
        parsed_timestamp = pd.to_datetime(raw_timestamp, utc=True, errors="coerce")
        raw_trading_date = data.get(
            "trading_date", pd.Series(pd.NA, index=data.index, dtype="string")
        )
        parsed_trading_date = pd.to_datetime(raw_trading_date, errors="coerce")
        fallback_date = parsed_timestamp.dt.tz_convert(LOCAL_TIMEZONE).dt.tz_localize(None)
        collected_date = data["collected_at"].dt.tz_convert(LOCAL_TIMEZONE).dt.tz_localize(None)
        parsed_trading_date = parsed_trading_date.fillna(fallback_date).fillna(collected_date)
        data["trading_date"] = parsed_trading_date.dt.date.astype("string")
        expected_net = data["foreign_buy_volume"] - data["foreign_sell_volume"]
        net_mismatch = (
                data["foreign_net_volume"].notna()
                & ~np.isclose(
            data["foreign_net_volume"], expected_net, rtol=1e-9, atol=1e-6
        )
        )
        invalid = (
                data["symbol"].isna()
                | parsed_trading_date.isna()
                | data["collected_at"].isna()
                | data["foreign_buy_volume"].isna()
                | data["foreign_sell_volume"].isna()
                | data["foreign_buy_volume"].lt(0)
                | data["foreign_sell_volume"].lt(0)
                | net_mismatch
        )
        rejected = data.loc[invalid].copy()
        rejected["reject_reason"] = "Du lieu khoi ngoai thieu, am hoac sai phep tinh rong"
        self.rejected["foreign_flow"] = rejected

        valid = data.loc[~invalid].copy()
        valid["foreign_net_volume"] = expected_net.loc[~invalid]
        valid["foreign_net_buy_volume"] = valid["foreign_net_volume"].clip(lower=0)
        valid["foreign_net_sell_volume"] = (-valid["foreign_net_volume"]).clip(lower=0)
        if "timestamp" not in valid.columns:
            valid["timestamp"] = pd.NaT
        valid["timestamp"] = parsed_timestamp.loc[valid.index]
        before = len(valid)
        valid = valid.sort_values(["collected_at"]).drop_duplicates(
            ["symbol", "trading_date", "source"], keep="last"
        )
        self.stats["foreign_flow"]["duplicate_rows_removed"] = before - len(valid)
        return valid[FOREIGN_FLOW_COLUMNS].sort_values(
            ["symbol", "trading_date", "source"]
        )

    def _clean_fundamentals(self, frame: pd.DataFrame, name: str) -> pd.DataFrame:
        if frame.empty:
            self.rejected[name] = pd.DataFrame()
            return pd.DataFrame(
                columns=["symbol", "period_type", "period", "source", *FUNDAMENTAL_NUMERIC_COLUMNS]
            )

        data = frame.copy()
        if "period" not in data.columns and "report_period" in data.columns:
            data["period"] = data["report_period"]
        if "net_sales" not in data.columns and "revenue" in data.columns:
            data["net_sales"] = data["revenue"]
        data["symbol"] = self._normalize_symbol(data.get("symbol"))
        data["period"] = data.get("period", pd.Series(index=data.index, dtype="object")).astype("string").str.strip()
        data["period_type"] = (
            data.get("period_type", pd.Series(index=data.index, dtype="object"))
            .astype("string")
            .str.strip()
            .str.lower()
        )
        data["source"] = (
            data.get("source", pd.Series("UNKNOWN", index=data.index))
            .astype("string")
            .str.strip()
            .str.upper()
        )
        raw_published_date = (
            data.get(
                "published_date",
                pd.Series(pd.NA, index=data.index, dtype="string"),
            )
            .astype("string")
            .str.strip()
        )
        missing_published_date = raw_published_date.isna() | raw_published_date.eq("")
        estimated_published_date = data["period"].map(
            self._estimated_published_date
        )
        data["published_date"] = raw_published_date.mask(
            missing_published_date,
            estimated_published_date,
        )

        for column in FUNDAMENTAL_NUMERIC_COLUMNS:
            if column in data.columns:
                data[column] = pd.to_numeric(data[column], errors="coerce").replace([np.inf, -np.inf], np.nan)

        # Nguồn có thể trả 0 cho chỉ tiêu thanh khoản không áp dụng (thường gặp ở ngân hàng).
        # Xem đây là thiếu một phần thay vì loại cả dòng BCTC và làm mất chỉ tiêu hợp lệ.
        for optional_ratio in ["current_ratio", "quick_ratio"]:
            if optional_ratio in data.columns:
                data[optional_ratio] = data[optional_ratio].mask(
                    data[optional_ratio].eq(0)
                )

        quarter_ok = data["period"].str.fullmatch(r"\d{4}-Q[1-4]", na=False)
        year_ok = data["period"].str.fullmatch(r"\d{4}", na=False)
        period_ok = ((data["period_type"] == "quarter") & quarter_ok) | (
                (data["period_type"] == "year") & year_ok
        )
        impossible = pd.Series(False, index=data.index)
        range_rules = {
            "market_cap": (0.0, np.inf),
            "current_ratio": (0.0, np.inf),
            "quick_ratio": (0.0, np.inf),
            "gross_margin": (-5.0, 5.0),
            "net_margin": (-10.0, 10.0),
            "roe": (-10.0, 10.0),
            "roa": (-5.0, 5.0),
            "pe": (-10_000.0, 10_000.0),
            "pb": (-10_000.0, 10_000.0),
        }
        for column, (minimum, maximum) in range_rules.items():
            if column not in data.columns:
                continue
            values = data[column]
            impossible |= values.notna() & ((values <= minimum) if minimum == 0 else (values < minimum))
            if np.isfinite(maximum):
                impossible |= values.notna() & values.gt(maximum)

        invalid = data["symbol"].isna() | ~period_ok | impossible
        rejected = data.loc[invalid].copy()
        rejected["reject_reason"] = np.where(
            impossible.loc[invalid],
            "Chỉ tiêu tài chính nằm ngoài giới hạn hợp lý",
            "Mã cổ phiếu hoặc kỳ báo cáo không hợp lệ",
        )
        self.rejected[name] = rejected

        valid = data.loc[~invalid].copy()
        before = len(valid)
        valid = valid.drop_duplicates(["symbol", "period_type", "period", "source"], keep="last")
        self.stats[name]["duplicate_rows_removed"] = before - len(valid)
        return valid

    @staticmethod
    def _normalize_symbol(series: pd.Series | None) -> pd.Series:
        if series is None:
            return pd.Series(dtype="string")
        result = series.astype("string").str.strip().str.upper()
        valid = result.str.fullmatch(r"[A-Z0-9]{2,10}", na=False)
        return result.where(valid, pd.NA)

    # 3. Chuẩn hóa ---------------------------------------------------------
    def normalize(self) -> None:
        """Đưa dữ liệu sạch về tên cột và kiểu dữ liệu thống nhất."""

        self.cleaned["history"] = self._normalize_history(self.cleaned["history"])
        self.cleaned["market_history"] = self._normalize_history(
            self.cleaned["market_history"]
        )
        self.cleaned["realtime"] = self._normalize_realtime(self.cleaned["realtime"])
        self.cleaned["screening"] = self._normalize_fundamental_frame(self.cleaned["screening"])
        self.cleaned["fundamentals"] = self._normalize_fundamental_frame(
            self.cleaned["fundamentals"]
        )

        for name, frame in self.cleaned.items():
            self.stats[name]["clean_rows"] = int(len(frame))
            self.stats[name]["rejected_rows"] = int(len(self.rejected.get(name, pd.DataFrame())))

    @staticmethod
    def _normalize_history(data: pd.DataFrame) -> pd.DataFrame:
        if data.empty:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        result = data.copy()
        result["timestamp_local"] = result["timestamp_utc"].dt.tz_convert(LOCAL_TIMEZONE)
        result["date"] = result["timestamp_local"].dt.date.astype("string")
        result["volume"] = result["volume"].round().astype("Int64")
        return result.reindex(columns=HISTORY_COLUMNS).sort_values(["symbol", "interval", "timestamp_utc"])

    @staticmethod
    def _normalize_realtime(data: pd.DataFrame) -> pd.DataFrame:
        if data.empty:
            return pd.DataFrame(columns=REALTIME_COLUMNS)
        result = pd.DataFrame(index=data.index)
        result["symbol"] = data["symbol"]
        result["market_id"] = data.get("marketId")
        result["board_id"] = data.get("boardId")
        event_time = data["_event_time"]
        result["timestamp_local"] = event_time.dt.tz_localize(
            LOCAL_TIMEZONE, ambiguous="NaT", nonexistent="shift_forward"
        )
        result["crawled_at_utc"] = data["_crawled_time"]
        result["match_price"] = data["matchPrice"]
        result["match_quantity"] = data["matchQtty"].round().astype("Int64")
        result["side"] = data.get("side", pd.Series(index=data.index, dtype="object")).astype("string").str.upper()
        result["average_price"] = data["avgPrice"]
        result["total_volume"] = data["totalVolumeTraded"].round().astype("Int64")
        result["open"] = data["openPrice"]
        result["high"] = data["highestPrice"]
        result["low"] = data["lowestPrice"]
        result["ceiling_price"] = pd.to_numeric(
            data.get("ceilingPrice", data.get("ceiling_price")), errors="coerce"
        )
        result["floor_price"] = pd.to_numeric(
            data.get("floorPrice", data.get("floor_price")), errors="coerce"
        )
        return result[REALTIME_COLUMNS].sort_values(["symbol", "timestamp_local"])

    @staticmethod
    def _normalize_fundamental_frame(data: pd.DataFrame) -> pd.DataFrame:
        if data.empty:
            return data.copy()
        metadata = ["symbol", "period_type", "period", "source"]
        metrics = [column for column in FUNDAMENTAL_NUMERIC_COLUMNS if column in data.columns]
        other = [
            column
            for column in data.columns
            if column not in metadata + metrics and not column.startswith("_")
        ]
        return data[metadata + metrics + other].sort_values(
            ["symbol", "period_type", "period", "source"]
        )

    # Đối chiếu và dùng nguồn dự phòng ------------------------------------
    def reconcile_sources(self) -> None:
        """Chọn bản ghi hợp lệ, điền phần thiếu từ nguồn thật và phát hiện mâu thuẫn."""

        frames = [
            frame.assign(_dataset_priority=priority)
            for priority, frame in enumerate(
                [self.cleaned["screening"], self.cleaned["fundamentals"]]
            )
            if not frame.empty
        ]
        if not frames:
            columns = [
                "symbol",
                "period_type",
                "period",
                "source",
                *FUNDAMENTAL_NUMERIC_COLUMNS,
                "report_period",
                "published_date",
                "collected_at",
                "fallback_sources",
                "fallback_fields",
                "source_consistent",
                "source_conflicts",
            ]
            self.reconciled_fundamentals = pd.DataFrame(columns=columns)
            self.source_audit = pd.DataFrame(
                columns=[
                    "symbol",
                    "period_type",
                    "period",
                    "selected_source",
                    "available_sources",
                    "selected_core_coverage",
                    "fallback_sources",
                    "fallback_fields",
                    "source_consistent",
                    "source_conflicts",
                ]
            )
            return

        data = pd.concat(frames, ignore_index=True, sort=False)
        data = data.sort_values("_dataset_priority").drop_duplicates(
            ["symbol", "period_type", "period", "source"], keep="first"
        )
        selected_rows: list[dict[str, Any]] = []
        audit_rows: list[dict[str, Any]] = []

        for (symbol, period_type, period), candidates in data.groupby(
                ["symbol", "period_type", "period"], sort=True
        ):
            candidates = candidates.copy()
            candidates["_source_priority"] = candidates["source"].map(SOURCE_PRIORITY).fillna(99)
            available_core = [column for column in CORE_FUNDAMENTAL_COLUMNS if column in candidates]
            if available_core:
                candidates["_coverage"] = candidates[available_core].notna().mean(axis=1)
            else:
                candidates["_coverage"] = 0.0
            candidates["_coverage_ok"] = (
                    candidates["_coverage"] >= self.config.minimum_fundamental_coverage
            )
            candidates = candidates.sort_values(
                ["_coverage_ok", "_source_priority", "_coverage"],
                ascending=[False, True, False],
            )

            selected: dict[str, Any] = dict(candidates.iloc[0].to_dict())
            selected_source = str(selected.get("source", "UNKNOWN"))
            fallback_sources: list[str] = []
            fallback_fields: list[str] = []
            conflicts: list[str] = []

            for metric in SOURCE_COMPARISON_COLUMNS:
                if metric not in candidates.columns:
                    continue
                values = candidates[["source", metric]].dropna(subset=[metric])
                if len(values) >= 2:
                    numeric = pd.to_numeric(values[metric], errors="coerce").dropna()
                    if len(numeric) >= 2:
                        scale = max(float(numeric.abs().max()), 1e-9)
                        if float(numeric.max() - numeric.min()) / scale > self.config.source_tolerance:
                            conflicts.append(metric)

            for _, fallback in candidates.iloc[1:].iterrows():
                fallback_source = str(fallback.get("source", "UNKNOWN"))
                used = False
                for metric in FUNDAMENTAL_NUMERIC_COLUMNS:
                    if metric not in candidates.columns or metric in conflicts:
                        continue
                    current_value = selected.get(metric)
                    fallback_value = fallback.at[metric]
                    if pd.isna(current_value) and pd.notna(fallback_value):
                        selected[metric] = fallback_value
                        fallback_fields.append(metric)
                        used = True
                if used:
                    fallback_sources.append(fallback_source)

            selected["fallback_sources"] = ",".join(dict.fromkeys(fallback_sources))
            selected["fallback_fields"] = ",".join(dict.fromkeys(fallback_fields))
            selected["source_consistent"] = not conflicts
            selected["source_conflicts"] = ",".join(sorted(set(conflicts)))
            selected_rows.append(selected)
            audit_rows.append(
                {
                    "symbol": symbol,
                    "period_type": period_type,
                    "period": period,
                    "selected_source": selected_source,
                    "available_sources": ",".join(candidates["source"].astype(str).drop_duplicates()),
                    "selected_core_coverage": float(
                        pd.to_numeric(selected.get("_coverage", 0.0), errors="coerce")
                    ),
                    "fallback_sources": selected["fallback_sources"],
                    "fallback_fields": selected["fallback_fields"],
                    "source_consistent": selected["source_consistent"],
                    "source_conflicts": selected["source_conflicts"],
                }
            )

        reconciled = pd.DataFrame(selected_rows)
        keep = [
            column
            for column in [
                "symbol",
                "period_type",
                "period",
                "source",
                *FUNDAMENTAL_NUMERIC_COLUMNS,
                "report_period",
                "published_date",
                "collected_at",
                "fallback_sources",
                "fallback_fields",
                "source_consistent",
                "source_conflicts",
            ]
            if column in reconciled.columns
        ]
        self.reconciled_fundamentals = reconciled[keep].sort_values(
            ["symbol", "period_type", "period"]
        )
        self.source_audit = pd.DataFrame(audit_rows)

    # 4. Kiểm tra ----------------------------------------------------------
    def validate(self) -> None:
        """Đánh giá schema, tỷ lệ loại bỏ, độ đầy đủ và độ mới của dữ liệu."""

        issues: list[dict[str, str]] = []
        required = {
            "history": {"symbol", "timestamp_utc", "open", "high", "low", "close", "volume"},
            "market_history": {
                "symbol",
                "timestamp_utc",
                "open",
                "high",
                "low",
                "close",
                "volume",
            },
            "realtime": {"symbol", "timestamp_local", "match_price", "match_quantity"},
            "foreign_flow": {
                "symbol",
                "trading_date",
                "foreign_buy_volume",
                "foreign_sell_volume",
                "foreign_net_volume",
                "collected_at",
            },
            "screening": {"symbol", "period_type", "period"},
            "fundamentals": {"symbol", "period_type", "period"},
        }

        datasets: dict[str, Any] = {}
        for name, frame in self.cleaned.items():
            missing = sorted(required[name] - set(frame.columns))
            if missing and not frame.empty:
                issues.append({"severity": "ERROR", "dataset": name, "message": f"Thiếu cột: {missing}"})
            if frame.empty:
                issues.append({"severity": "WARNING", "dataset": name, "message": "Không có dữ liệu"})

            input_rows = int(self.stats[name].get("input_rows", 0))
            rejected_rows = int(self.stats[name].get("rejected_rows", 0))
            rejection_rate = rejected_rows / input_rows if input_rows else 0.0
            if rejection_rate > 0.05:
                issues.append(
                    {
                        "severity": "ERROR" if rejection_rate > 0.20 else "WARNING",
                        "dataset": name,
                        "message": f"Tỷ lệ bản ghi bị loại cao: {rejection_rate:.2%}",
                    }
                )
            datasets[name] = {
                **self.stats[name],
                "rejection_rate": round(rejection_rate, 6),
                "null_rate": {
                    column: round(float(frame[column].isna().mean()), 6)
                    for column in frame.columns
                    if frame[column].isna().any()
                },
            }

        history = self.cleaned["history"]
        if not history.empty:
            datasets["history"]["latest_timestamp_utc"] = self._iso_value(history["timestamp_utc"].max())
        market_history = self.cleaned["market_history"]
        if not market_history.empty:
            datasets["market_history"]["latest_timestamp_utc"] = self._iso_value(
                market_history["timestamp_utc"].max()
            )
        realtime = self.cleaned["realtime"]
        if not realtime.empty:
            datasets["realtime"]["latest_crawled_at_utc"] = self._iso_value(
                realtime["crawled_at_utc"].max()
            )
        foreign_flow = self.cleaned["foreign_flow"]
        if not foreign_flow.empty:
            datasets["foreign_flow"]["latest_collected_at"] = self._iso_value(
                foreign_flow["collected_at"].max()
            )
        screening = self.cleaned["screening"]
        if not screening.empty:
            datasets["screening"]["latest_period_by_type"] = {
                str(key): str(value)
                for key, value in screening.groupby("period_type")["period"].max().items()
            }

        for error in self.ingestion_errors:
            issues.append(
                {
                    "severity": "WARNING",
                    "dataset": error.get("dataset", "ingestion"),
                    "message": f"Lỗi nạp/API: {error.get('error', 'không rõ')}",
                }
            )

        if not self.source_audit.empty:
            for _, row in self.source_audit.loc[~self.source_audit["source_consistent"]].iterrows():
                issues.append(
                    {
                        "severity": "ERROR",
                        "dataset": "fundamentals",
                        "message": (
                            f"{row['symbol']} {row['period']}: nguồn mâu thuẫn tại "
                            f"{row['source_conflicts']}"
                        ),
                    }
                )

        data_quality = self._build_symbol_quality()
        self.analysis["data_quality"] = data_quality
        blocked = int((data_quality["signal_status"] == "NO SIGNAL").sum()) if not data_quality.empty else 0
        eligible = int((data_quality["signal_status"] == "ELIGIBLE").sum()) if not data_quality.empty else 0
        if blocked:
            issues.append(
                {
                    "severity": "WARNING",
                    "dataset": "signal_gate",
                    "message": f"{blocked} mã bị chặn tín hiệu do dữ liệu chưa đủ tin cậy",
                }
            )

        has_error = any(issue["severity"] == "ERROR" for issue in issues)
        has_warning = any(issue["severity"] == "WARNING" for issue in issues)
        self.quality_report = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "FAIL" if has_error else ("WARN" if has_warning else "PASS"),
            "input_dir": str(self.config.input_dir.resolve()),
            "precheck": self.precheck_report,
            "datasets": datasets,
            "source_reconciliation": {
                "rows_checked": int(len(self.source_audit)),
                "fallback_rows": int(
                    self.source_audit["fallback_sources"].fillna("").ne("").sum()
                )
                if not self.source_audit.empty
                else 0,
                "conflict_rows": int((~self.source_audit["source_consistent"]).sum())
                if not self.source_audit.empty
                else 0,
            },
            "signal_gate": {
                "eligible_symbols": eligible,
                "blocked_symbols": blocked,
                "rule": "Chỉ mã ELIGIBLE mới được phép đi tiếp tới bộ phát BUY/SELL.",
            },
            "issues": issues,
        }

    def _build_symbol_quality(self) -> pd.DataFrame:
        """Kiểm tra độ tin cậy theo từng mã và quyết định có được phát tín hiệu hay không."""

        symbols = {
            str(symbol).strip().upper()
            for symbol in self.config.expected_symbols
            if str(symbol).strip()
        }
        if not symbols:
            for frame in [
                self.cleaned["history"],
                self.cleaned["realtime"],
                self.cleaned["foreign_flow"],
                self.reconciled_fundamentals,
            ]:
                if "symbol" in frame.columns:
                    symbols.update(frame["symbol"].dropna().astype(str))
        columns = [
            "symbol",
            "data_status",
            "signal_status",
            "history_rows",
            "price_age_days",
            "realtime_age_minutes",
            "realtime_collection_age_minutes",
            "foreign_sessions_available",
            "foreign_source",
            "market_context_symbol",
            "market_price_age_days",
            "roe_ttm_available",
            "fundamental_period",
            "fundamental_age_days",
            "fundamental_coverage",
            "published_date",
            "financial_collected_at",
            "selected_source",
            "fallback_sources",
            "source_consistent",
            "quality_reasons",
        ]
        if not symbols:
            return pd.DataFrame(columns=columns)

        now = pd.Timestamp.now(tz="UTC")
        roe_ttm_frame = self._calculate_roe_ttm()
        roe_ttm_by_symbol = (
            roe_ttm_frame.set_index("symbol")["roe_ttm"]
            if not roe_ttm_frame.empty
            else pd.Series(dtype="float64")
        )
        market_history = self.cleaned["market_history"]
        market_context_symbol: str | None = None
        market_price_age_days = np.nan
        market_blocking: list[str] = []
        market_warnings: list[str] = []
        if market_history.empty:
            market_warnings.append("Thiếu lịch sử VNINDEX/VN30")
        else:
            market_symbols = set(market_history["symbol"].dropna().astype(str))
            market_context_symbol = (
                "VNINDEX"
                if "VNINDEX" in market_symbols
                else ("VN30" if "VN30" in market_symbols else sorted(market_symbols)[0])
            )
            selected_market = market_history[
                market_history["symbol"] == market_context_symbol
                ]
            market_price_age_days = float(
                (now - selected_market["timestamp_utc"].max()) / pd.Timedelta(days=1)
            )
            if len(selected_market) < 20:
                market_warnings.append(
                    f"{market_context_symbol} chỉ có {len(selected_market)}/20 phiên để tính SMA20"
                )
            if market_price_age_days > self.config.max_price_age_days:
                market_warnings.append(
                    f"Dữ liệu {market_context_symbol} quá cũ ({market_price_age_days:.1f} ngày)"
                )

        rows: list[dict[str, Any]] = []
        for symbol in sorted(symbols):
            blocking: list[str] = list(market_blocking)
            warnings: list[str] = list(market_warnings)

            history = self.cleaned["history"]
            symbol_history = history[history["symbol"] == symbol] if not history.empty else history
            history_rows = int(len(symbol_history))
            price_age_days = np.nan
            if symbol_history.empty:
                blocking.append("Thiếu dữ liệu giá lịch sử/API giá không trả dữ liệu")
            else:
                latest_price = symbol_history["timestamp_utc"].max()
                price_age_days = float((now - latest_price) / pd.Timedelta(days=1))
                if price_age_days < -1:
                    blocking.append("Thời gian giá nằm trong tương lai")
                elif price_age_days > self.config.max_price_age_days:
                    blocking.append(f"Dữ liệu giá quá cũ ({price_age_days:.1f} ngày)")
                if history_rows < self.config.minimum_history_rows:
                    blocking.append(
                        f"Chỉ có {history_rows}/{self.config.minimum_history_rows} phiên giá tối thiểu"
                    )

            realtime = self.cleaned["realtime"]
            symbol_realtime = realtime[realtime["symbol"] == symbol] if not realtime.empty else realtime
            realtime_age_minutes = np.nan
            realtime_collection_age_minutes = np.nan
            if symbol_realtime.empty:
                message = "Thiếu realtime/API realtime không trả dữ liệu"
                (blocking if self.config.require_realtime_for_signal else warnings).append(message)
            else:
                latest_event = symbol_realtime["timestamp_local"].max()
                latest_collection = symbol_realtime["crawled_at_utc"].max()
                realtime_age_minutes = float(
                    (now - latest_event.tz_convert("UTC")) / pd.Timedelta(minutes=1)
                )
                realtime_collection_age_minutes = float(
                    (now - latest_collection) / pd.Timedelta(minutes=1)
                )
                if realtime_age_minutes < -5:
                    blocking.append("Thời gian realtime nằm trong tương lai")
                elif realtime_age_minutes > self.config.max_realtime_age_minutes:
                    local_time = now.tz_convert("Asia/Ho_Chi_Minh")
                    is_market_closed = local_time.hour >= 15 or local_time.hour < 9 or local_time.weekday() >= 5
                    if not is_market_closed:
                        message = f"Realtime quá cũ ({realtime_age_minutes:.0f} phút)"
                        (blocking if self.config.require_realtime_for_signal else warnings).append(message)

                if realtime_collection_age_minutes > self.config.max_realtime_age_minutes:
                    local_time = now.tz_convert("Asia/Ho_Chi_Minh")
                    is_market_closed = local_time.hour >= 15 or local_time.hour < 9 or local_time.weekday() >= 5
                    if not is_market_closed:
                        message = (
                            "Crawler realtime không cập nhật "
                            f"({realtime_collection_age_minutes:.0f} phút)"
                        )
                        (blocking if self.config.require_realtime_for_signal else warnings).append(message)

            foreign = self._preferred_foreign_rows(
                self.cleaned["foreign_flow"], symbol
            )
            foreign_sessions_available = int(foreign["trading_date"].nunique())
            foreign_source = (
                str(foreign["source"].iloc[0]) if not foreign.empty else None
            )
            if foreign_sessions_available < 5:
                warnings.append(
                    f"Thiếu chuỗi khối ngoại: có {foreign_sessions_available}/5 phiên"
                )

            fundamental = self.reconciled_fundamentals
            symbol_fundamental = (
                fundamental[fundamental["symbol"] == symbol] if not fundamental.empty else fundamental
            )
            fundamental_period: str | None = None
            fundamental_age_days = np.nan
            coverage = 0.0
            published_date: str | None = None
            financial_collected_at: str | None = None
            selected_source: str | None = None
            fallback_sources = ""
            source_consistent = True
            roe_ttm = pd.to_numeric(
                roe_ttm_by_symbol.get(symbol, np.nan), errors="coerce"
            )
            roe_ttm_available = bool(pd.notna(roe_ttm))
            if symbol_fundamental.empty:
                warnings.append("Thiếu báo cáo tài chính/API cơ bản không trả dữ liệu")
            else:
                quarter = symbol_fundamental[symbol_fundamental["period_type"] == "quarter"]
                selected = quarter.iloc[-1] if not quarter.empty else symbol_fundamental.iloc[-1]
                fundamental_period = str(selected["period"])
                selected_source = str(selected.get("source", "UNKNOWN"))
                raw_published_date = selected.get("published_date")
                raw_collected_at = selected.get("collected_at")
                published_date = (
                    str(raw_published_date) if pd.notna(raw_published_date) else None
                )
                financial_collected_at = (
                    str(raw_collected_at) if pd.notna(raw_collected_at) else None
                )
                fallback_sources = str(selected.get("fallback_sources", "") or "")
                source_consistent = bool(selected.get("source_consistent", True))
                available_core = [
                    column for column in CORE_FUNDAMENTAL_COLUMNS if column in selected.index
                ]
                coverage = (
                    float(selected[available_core].notna().mean()) if available_core else 0.0
                )
                if coverage < self.config.minimum_fundamental_coverage:
                    warnings.append(f"Chỉ tiêu cơ bản chỉ đủ {coverage:.0%}")
                if not source_consistent:
                    warnings.append(
                        f"Nguồn tài chính mâu thuẫn: {selected.get('source_conflicts', '')}"
                    )

                if not published_date and fundamental_period:
                    p_end = self._period_end(str(fundamental_period))
                    if p_end is not None:
                        published_date = (p_end + pd.Timedelta(days=35)).strftime("%Y-%m-%d")
                        warnings.append("Ước lượng ngày công bố BCTC theo kỳ báo cáo")
                if not published_date:
                    warnings.append("Thiếu ngày công bố BCTC")
                else:
                    parsed_published = pd.to_datetime(
                        published_date, utc=True, errors="coerce"
                    )
                    if pd.isna(parsed_published):
                        warnings.append("Ngày công bố BCTC sai định dạng")
                    elif parsed_published > now:
                        warnings.append("Ngày công bố BCTC nằm trong tương lai")
                if not financial_collected_at:
                    warnings.append("Thiếu thời điểm thu thập báo cáo tài chính")

                period_end = self._period_end(str(fundamental_period))
                if period_end is None:
                    warnings.append("Không xác định được ngày kết thúc kỳ tài chính")
                else:
                    fundamental_age_days = float((now.normalize() - period_end).days)
                    maximum_age = (
                        self.config.max_quarter_age_days
                        if fundamental_period and "-Q" in fundamental_period
                        else self.config.max_year_age_days
                    )
                    if fundamental_age_days > maximum_age:
                        warnings.append(
                            f"Báo cáo tài chính quá cũ ({fundamental_age_days:.0f} ngày)"
                        )
                if not roe_ttm_available:
                    warnings.append(
                        "Chưa tính được ROE TTM từ 4 quý liên tiếp (dùng ROE quý/năm thay thế)"
                    )

            reasons = blocking + warnings
            rows.append(
                {
                    "symbol": symbol,
                    "data_status": "DATA WARNING" if reasons else "OK",
                    "signal_status": "NO SIGNAL" if blocking else "ELIGIBLE",
                    "history_rows": history_rows,
                    "price_age_days": price_age_days,
                    "realtime_age_minutes": realtime_age_minutes,
                    "realtime_collection_age_minutes": realtime_collection_age_minutes,
                    "foreign_sessions_available": foreign_sessions_available,
                    "foreign_source": foreign_source,
                    "market_context_symbol": market_context_symbol,
                    "market_price_age_days": market_price_age_days,
                    "roe_ttm_available": roe_ttm_available,
                    "fundamental_period": fundamental_period,
                    "fundamental_age_days": fundamental_age_days,
                    "fundamental_coverage": coverage,
                    "published_date": published_date,
                    "financial_collected_at": financial_collected_at,
                    "selected_source": selected_source,
                    "fallback_sources": fallback_sources,
                    "source_consistent": source_consistent,
                    "quality_reasons": " | ".join(reasons),
                }
            )
        return pd.DataFrame(rows, columns=columns)

    @staticmethod
    def _period_end(period: str) -> pd.Timestamp | None:
        """Đổi kỳ ``YYYY-Qn`` hoặc ``YYYY`` thành ngày kết thúc kỳ theo UTC."""

        quarter_match = re.fullmatch(r"(\d{4})-Q([1-4])", period)
        if quarter_match:
            year, quarter = map(int, quarter_match.groups())
            month = quarter * 3
            return pd.Timestamp(year=year, month=month, day=1, tz="UTC") + pd.offsets.MonthEnd(0)
        if re.fullmatch(r"\d{4}", period):
            return pd.Timestamp(year=int(period), month=12, day=31, tz="UTC")
        return None

    @staticmethod
    def _estimated_published_date(period: Any) -> str | None:
        """Ước lượng ngày công bố bằng ngày kết thúc kỳ cộng 30 ngày."""

        period_end = AnalyticsPipeline._period_end(str(period))
        if period_end is None:
            return None
        return (period_end + pd.Timedelta(days=30)).isoformat()

    @staticmethod
    def _iso_value(value: Any) -> str | None:
        if pd.isna(value):
            return None
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    # 5. Phân tích ---------------------------------------------------------
    def analyze(self) -> None:
        """Tính chỉ báo giá, ghép dữ liệu cơ bản và chấm điểm sàng lọc."""

        price = self._analyze_price_history(self.cleaned["history"])
        market_metrics = self._analyze_price_history(self.cleaned["market_history"])
        market_context = market_metrics.rename(
            columns={"price_as_of": "as_of", "latest_close": "close"}
        )
        latest_fundamental = self._latest_fundamentals()
        snapshot = self._merge_snapshot(
            price,
            latest_fundamental,
            self.cleaned["realtime"],
            self.cleaned["foreign_flow"],
        )
        data_quality = self.analysis.get("data_quality", pd.DataFrame())
        if not data_quality.empty:
            snapshot = snapshot.merge(data_quality, on="symbol", how="outer", validate="one_to_one")
        ranked = self._rank_stocks(snapshot)
        self.analysis["price_metrics"] = price
        self.analysis["market_context"] = market_context
        self.analysis["stock_snapshot"] = snapshot
        self.analysis["screening_ranked"] = ranked

    @staticmethod
    def _analyze_price_history(history: pd.DataFrame) -> pd.DataFrame:
        columns = [
            "symbol",
            "price_as_of",
            "latest_close",
            "previous_close",
            "latest_volume",
            "return_1d",
            "return_5d",
            "return_20d",
            "sma_10",
            "sma_20",
            "sma_50",
            "sma_200",
            "ema_12",
            "ema_20",
            "ema_26",
            "ema_50",
            "rsi_14",
            "macd",
            "macd_signal",
            "macd_histogram",
            "bollinger_middle_20",
            "bollinger_upper_20",
            "bollinger_lower_20",
            "bollinger_bandwidth_20",
            "atr_14",
            "adx_14",
            "stochastic_k_14",
            "stochastic_d_3",
            "obv",
            "volatility_20d",
            "average_volume_20d",
            "volume_ratio_20d",
            "distance_from_52w_high",
            "trend_state",
            "ob_support",
            "ob_resistance",
            "indicator_data_complete",
        ]
        if history.empty:
            return pd.DataFrame(columns=columns)

        daily = history.sort_values("timestamp_utc").drop_duplicates(
            ["symbol", "date"], keep="last"
        )
        records: list[dict[str, Any]] = []
        for symbol, group in daily.groupby("symbol", sort=True):
            group = group.sort_values("timestamp_utc")
            close = group["close"].astype(float)
            high = group["high"].astype(float)
            low = group["low"].astype(float)
            volume = group["volume"].astype(float)
            returns = close.pct_change()
            latest = group.iloc[-1]
            high_52w = high.tail(252).max()

            sma_10 = close.rolling(10, min_periods=10).mean()
            sma_20 = close.rolling(20, min_periods=20).mean()
            sma_50 = close.rolling(50, min_periods=50).mean()
            sma_200 = close.rolling(200, min_periods=200).mean()
            ema_12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
            ema_20 = close.ewm(span=20, adjust=False, min_periods=20).mean()
            ema_26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
            ema_50 = close.ewm(span=50, adjust=False, min_periods=50).mean()

            delta = close.diff()
            gain = delta.clip(lower=0)
            loss = -delta.clip(upper=0)
            average_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
            average_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
            relative_strength = average_gain / average_loss.replace(0, np.nan)
            rsi_14 = 100 - (100 / (1 + relative_strength))
            rsi_14 = rsi_14.mask((average_loss == 0) & (average_gain > 0), 100.0)
            rsi_14 = rsi_14.mask((average_loss == 0) & (average_gain == 0), 50.0)

            macd = ema_12 - ema_26
            macd_signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
            macd_histogram = macd - macd_signal

            rolling_std_20 = close.rolling(20, min_periods=20).std(ddof=0)
            bollinger_upper = sma_20 + 2 * rolling_std_20
            bollinger_lower = sma_20 - 2 * rolling_std_20
            bollinger_bandwidth = (
                    (bollinger_upper - bollinger_lower) / sma_20.replace(0, np.nan)
            )

            previous_close = close.shift(1)
            true_range = pd.concat(
                [
                    high - low,
                    (high - previous_close).abs(),
                    (low - previous_close).abs(),
                ],
                axis=1,
            ).max(axis=1)
            atr_14 = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

            up_move = high.diff()
            down_move = -low.diff()
            plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
            minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
            plus_di = (
                    100
                    * plus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
                    / atr_14.replace(0, np.nan)
            )
            minus_di = (
                    100
                    * minus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
                    / atr_14.replace(0, np.nan)
            )
            directional_sum = (plus_di + minus_di).replace(0, np.nan)
            dx = 100 * (plus_di - minus_di).abs() / directional_sum
            adx_14 = dx.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

            lowest_14 = low.rolling(14, min_periods=14).min()
            highest_14 = high.rolling(14, min_periods=14).max()
            stochastic_k = 100 * (close - lowest_14) / (highest_14 - lowest_14).replace(0, np.nan)
            stochastic_d = stochastic_k.rolling(3, min_periods=3).mean()
            obv = (np.sign(close.diff()).fillna(0) * volume.fillna(0)).cumsum()
            average_volume_20 = volume.rolling(20, min_periods=5).mean()
            volume_ratio_20 = volume / average_volume_20.replace(0, np.nan)

            latest_close = float(close.iloc[-1])
            latest_sma_20 = sma_20.iloc[-1]
            latest_sma_50 = sma_50.iloc[-1]
            latest_ema_12 = ema_12.iloc[-1]
            latest_ema_26 = ema_26.iloc[-1]
            required_indicators = [
                rsi_14.iloc[-1],
                macd_signal.iloc[-1],
                atr_14.iloc[-1],
                latest_sma_20,
                latest_sma_50,
            ]
            indicator_data_complete = all(pd.notna(value) for value in required_indicators)
            if not indicator_data_complete:
                trend_state = "INSUFFICIENT_DATA"
            elif latest_close > latest_sma_20 > latest_sma_50 and latest_ema_12 > latest_ema_26:
                trend_state = "BULLISH"
            elif latest_close < latest_sma_20 < latest_sma_50 and latest_ema_12 < latest_ema_26:
                trend_state = "BEARISH"
            else:
                trend_state = "NEUTRAL"

            open_p = (
                group["open"].astype(float)
                if "open" in group.columns
                else (group["o"].astype(float) if "o" in group.columns else close)
            )
            ob_support, ob_resistance = AnalyticsPipeline._detect_order_blocks(
                high=high, low=low, close=close, open_p=open_p
            )

            records.append(
                {
                    "symbol": symbol,
                    "price_as_of": latest["date"],
                    "latest_close": latest_close,
                    "previous_close": float(close.iloc[-2]) if len(close) > 1 else np.nan,
                    "latest_volume": float(volume.iloc[-1]),
                    "return_1d": AnalyticsPipeline._period_return(close, 1),
                    "return_5d": AnalyticsPipeline._period_return(close, 5),
                    "return_20d": AnalyticsPipeline._period_return(close, 20),
                    "sma_10": sma_10.iloc[-1],
                    "sma_20": latest_sma_20,
                    "sma_50": latest_sma_50,
                    "sma_200": sma_200.iloc[-1],
                    "ema_12": latest_ema_12,
                    "ema_20": ema_20.iloc[-1],
                    "ema_26": latest_ema_26,
                    "ema_50": ema_50.iloc[-1],
                    "rsi_14": rsi_14.iloc[-1],
                    "macd": macd.iloc[-1],
                    "macd_signal": macd_signal.iloc[-1],
                    "macd_histogram": macd_histogram.iloc[-1],
                    "bollinger_middle_20": latest_sma_20,
                    "bollinger_upper_20": bollinger_upper.iloc[-1],
                    "bollinger_lower_20": bollinger_lower.iloc[-1],
                    "bollinger_bandwidth_20": bollinger_bandwidth.iloc[-1],
                    "atr_14": atr_14.iloc[-1],
                    "adx_14": adx_14.iloc[-1],
                    "stochastic_k_14": stochastic_k.iloc[-1],
                    "stochastic_d_3": stochastic_d.iloc[-1],
                    "obv": obv.iloc[-1],
                    "volatility_20d": returns.tail(20).std(ddof=1) * np.sqrt(252)
                    if len(returns.dropna()) >= 10
                    else np.nan,
                    "average_volume_20d": average_volume_20.iloc[-1],
                    "volume_ratio_20d": volume_ratio_20.iloc[-1],
                    "distance_from_52w_high": latest_close / high_52w - 1
                    if not np.isnan(float(high_52w)) and float(high_52w) > 0
                    else np.nan,
                    "trend_state": trend_state,
                    "ob_support": ob_support,
                    "ob_resistance": ob_resistance,
                    "indicator_data_complete": indicator_data_complete,
                }
            )
        return pd.DataFrame(records, columns=columns)

    @staticmethod
    def _detect_order_blocks(
            high: pd.Series,
            low: pd.Series,
            close: pd.Series,
            open_p: pd.Series,
            window: int = 60,
    ) -> tuple[float | None, float | None]:
        """Xác định ngưỡng hỗ trợ Bullish Order Block (ob_support) và kháng cự Bearish Order Block (ob_resistance).

        - ob_support: Đáy cây nến giảm cuối cùng trước nhịp tăng tạo Swing High (hoặc Swing Low hỗ trợ gần nhất).
        - ob_resistance: Đỉnh cây nến tăng cuối cùng trước nhịp giảm tạo Swing Low (hoặc Swing High kháng cự gần nhất).
        """
        n = len(close)
        if n < 20:
            return None, None

        h = high.to_numpy(dtype=float)
        lo = low.to_numpy(dtype=float)
        c = close.to_numpy(dtype=float)
        o = (
            open_p.to_numpy(dtype=float)
            if open_p is not None and len(open_p) == n
            else c
        )
        latest_c = float(c[-1])

        start_idx = max(0, n - window)
        h_w = h[start_idx:]
        lo_w = lo[start_idx:]
        c_w = c[start_idx:]
        o_w = o[start_idx:]
        num_w = len(c_w)

        swing_highs: list[tuple[int, float]] = []
        swing_lows: list[tuple[int, float]] = []
        for i in range(2, num_w - 2):
            if (
                    h_w[i] >= h_w[i - 1]
                    and h_w[i] >= h_w[i - 2]
                    and h_w[i] >= h_w[i + 1]
                    and h_w[i] >= h_w[i + 2]
            ):
                swing_highs.append((i, float(h_w[i])))
            if (
                    lo_w[i] <= lo_w[i - 1]
                    and lo_w[i] <= lo_w[i - 2]
                    and lo_w[i] <= lo_w[i + 1]
                    and lo_w[i] <= lo_w[i + 2]
            ):
                swing_lows.append((i, float(lo_w[i])))

        bullish_ob_low: float | None = None
        for sh_idx, _ in reversed(swing_highs):
            for j in range(sh_idx - 1, max(0, sh_idx - 6), -1):
                if c_w[j] < o_w[j]:
                    cand_low = float(lo_w[j])
                    if cand_low < latest_c:
                        bullish_ob_low = cand_low
                        break
            if bullish_ob_low is not None:
                break

        ob_support = bullish_ob_low
        if ob_support is None:
            valid_lows = [sl_val for _, sl_val in swing_lows if sl_val < latest_c * 0.995]
            if valid_lows:
                ob_support = max(valid_lows)

        bearish_ob_high: float | None = None
        for sl_idx, _ in reversed(swing_lows):
            for j in range(sl_idx - 1, max(0, sl_idx - 6), -1):
                if c_w[j] > o_w[j]:
                    cand_high = float(h_w[j])
                    if cand_high > latest_c:
                        bearish_ob_high = cand_high
                        break
            if bearish_ob_high is not None:
                break

        ob_resistance = bearish_ob_high
        if ob_resistance is None:
            valid_highs = [sh_val for _, sh_val in swing_highs if sh_val > latest_c * 1.005]
            if valid_highs:
                ob_resistance = min(valid_highs)

        res_support = (
            ob_support
            if (ob_support is not None and 0 < ob_support < latest_c)
            else None
        )
        res_resistance = (
            ob_resistance
            if (ob_resistance is not None and ob_resistance > latest_c)
            else None
        )
        return res_support, res_resistance

    @staticmethod
    def _period_return(close: pd.Series, periods: int) -> float:
        if len(close) <= periods or close.iloc[-periods - 1] <= 0:
            return np.nan
        return float(close.iloc[-1] / close.iloc[-periods - 1] - 1)

    def _calculate_roe_ttm(self) -> pd.DataFrame:
        """Tính ROE TTM từ đúng bốn quý và vốn chủ bình quân đầu/cuối kỳ."""

        columns = [
            "symbol",
            "roe_ttm",
            "net_profit_ttm",
            "average_equity_ttm",
            "roe_ttm_start_period",
            "roe_ttm_end_period",
            "roe_ttm_method",
        ]
        source = self.reconciled_fundamentals
        if source.empty:
            return pd.DataFrame(columns=columns)
        quarter = source[source["period_type"] == "quarter"].copy()
        required = {"symbol", "period", "net_profit", "owners_equity"}
        if quarter.empty or not required.issubset(quarter.columns):
            return pd.DataFrame(columns=columns)

        parsed = quarter["period"].astype("string").str.extract(
            r"^(?P<year>\d{4})-Q(?P<quarter>[1-4])$"
        )
        quarter["_year"] = pd.to_numeric(parsed["year"], errors="coerce")
        quarter["_quarter"] = pd.to_numeric(parsed["quarter"], errors="coerce")
        quarter["_ordinal"] = quarter["_year"] * 4 + quarter["_quarter"] - 1
        quarter["net_profit"] = pd.to_numeric(quarter["net_profit"], errors="coerce")
        quarter["owners_equity"] = pd.to_numeric(
            quarter["owners_equity"], errors="coerce"
        )
        quarter = quarter.dropna(subset=["_ordinal"]).drop_duplicates(
            ["symbol", "_ordinal"], keep="last"
        )

        records: list[dict[str, Any]] = []
        for symbol, group in quarter.groupby("symbol", sort=True):
            group = group.sort_values("_ordinal").set_index("_ordinal", drop=False)
            latest_ordinal = int(group["_ordinal"].max())
            ttm_ordinals = list(range(latest_ordinal - 3, latest_ordinal + 1))
            beginning_ordinal = latest_ordinal - 4
            if not all(ordinal in group.index for ordinal in ttm_ordinals):
                continue
            ttm_rows = group.loc[ttm_ordinals]
            net_profit = ttm_rows["net_profit"]
            if beginning_ordinal in group.index and pd.notna(
                    group.at[beginning_ordinal, "owners_equity"]
            ):
                beginning_equity = group.at[beginning_ordinal, "owners_equity"]
                method = "SUM_4Q_NET_PROFIT/AVG_BEGIN_END_EQUITY"
            else:
                beginning_equity = ttm_rows.iloc[0]["owners_equity"]
                method = "SUM_4Q_NET_PROFIT/AVG_FIRST_END_EQUITY_APPROX"
            ending_equity = group.at[latest_ordinal, "owners_equity"]
            if (
                    net_profit.isna().any()
                    or pd.isna(beginning_equity)
                    or pd.isna(ending_equity)
            ):
                continue
            average_equity = (float(beginning_equity) + float(ending_equity)) / 2
            if average_equity <= 0:
                continue
            net_profit_ttm = float(net_profit.sum())
            records.append(
                {
                    "symbol": str(symbol),
                    "roe_ttm": net_profit_ttm / average_equity,
                    "net_profit_ttm": net_profit_ttm,
                    "average_equity_ttm": average_equity,
                    "roe_ttm_start_period": str(ttm_rows.iloc[0]["period"]),
                    "roe_ttm_end_period": str(ttm_rows.iloc[-1]["period"]),
                    "roe_ttm_method": method,
                }
            )
        return pd.DataFrame(records, columns=columns)

    @staticmethod
    def _preferred_foreign_rows(
            foreign_flow: pd.DataFrame, symbol: str
    ) -> pd.DataFrame:
        """Chọn một nguồn khối ngoại có nhiều phiên nhất, ưu tiên DNSE khi hòa."""

        if foreign_flow.empty:
            return foreign_flow.copy()
        selected = foreign_flow[foreign_flow["symbol"] == symbol].copy()
        if selected.empty:
            return selected
        counts = (
            selected.groupby("source")["trading_date"]
            .nunique()
            .rename("sessions")
            .reset_index()
        )
        counts["priority"] = counts["source"].map({"DNSE": 0, "KBS": 1}).fillna(99)
        source = counts.sort_values(
            ["sessions", "priority"], ascending=[False, True]
        ).iloc[0]["source"]
        return selected[selected["source"] == source].sort_values("trading_date")

    @classmethod
    def _summarize_foreign_flow(cls, foreign_flow: pd.DataFrame) -> pd.DataFrame:
        """Tổng hợp năm phiên gần nhất và giữ snapshot mới nhất của từng mã."""

        if foreign_flow.empty:
            return pd.DataFrame(columns=["symbol"])
        records: list[dict[str, Any]] = []
        for symbol in sorted(foreign_flow["symbol"].dropna().astype(str).unique()):
            selected = cls._preferred_foreign_rows(foreign_flow, symbol)
            if selected.empty:
                continue
            selected = selected.drop_duplicates("trading_date", keep="last").tail(5)
            latest = selected.iloc[-1]
            net = pd.to_numeric(selected["foreign_net_volume"], errors="coerce")
            record: dict[str, Any] = {
                "symbol": symbol,
                "foreign_as_of": latest["trading_date"],
                "foreign_source": latest["source"],
                "foreign_session_count_5d": int(len(selected)),
                "foreign_net_volume_5d": float(net.sum()),
                "foreign_net_buy_sessions_5d": int(net.gt(0).sum()),
                "foreign_net_sell_sessions_5d": int(net.lt(0).sum()),
            }
            for column in [
                "foreign_buy_volume",
                "foreign_sell_volume",
                "foreign_net_volume",
                "foreign_net_buy_volume",
                "foreign_net_sell_volume",
                "foreign_buy_value",
                "foreign_sell_value",
                "foreign_net_value",
            ]:
                if column in latest.index:
                    record[column] = latest[column]
            records.append(record)
        return pd.DataFrame(records)

    def _latest_fundamentals(self) -> pd.DataFrame:
        source = self.reconciled_fundamentals
        if source.empty:
            return pd.DataFrame(columns=["symbol"])

        pieces: list[pd.DataFrame] = []
        for period_type in ["quarter", "year"]:
            part = source[source["period_type"] == period_type].copy()
            if part.empty:
                continue
            part = part.sort_values("period").drop_duplicates("symbol", keep="last")
            keep = [
                column
                for column in [
                    "symbol",
                    "period",
                    "source",
                    *FUNDAMENTAL_NUMERIC_COLUMNS,
                    "report_period",
                    "published_date",
                    "collected_at",
                    "fallback_sources",
                    "fallback_fields",
                    "source_consistent",
                    "source_conflicts",
                ]
                if column in part.columns
            ]
            part = part[keep].rename(
                columns={column: f"{column}_{period_type}" for column in keep if column != "symbol"}
            )
            pieces.append(part)

        if not pieces:
            return pd.DataFrame(columns=["symbol"])
        result = pieces[0]
        for piece in pieces[1:]:
            result = result.merge(piece, on="symbol", how="outer", validate="one_to_one")
        roe_ttm = self._calculate_roe_ttm()
        if not roe_ttm.empty:
            result = result.merge(roe_ttm, on="symbol", how="left", validate="one_to_one")
        return result

    @staticmethod
    def _merge_snapshot(
            price: pd.DataFrame,
            fundamentals: pd.DataFrame,
            realtime: pd.DataFrame,
            foreign_flow: pd.DataFrame,
    ) -> pd.DataFrame:
        symbols: set[str] = set()
        for frame in [price, fundamentals, realtime, foreign_flow]:
            if "symbol" in frame.columns:
                symbols.update(frame["symbol"].dropna().astype(str))
        if not symbols:
            return pd.DataFrame(columns=["symbol"])

        result = pd.DataFrame({"symbol": sorted(symbols)})
        if not price.empty:
            result = result.merge(price, on="symbol", how="left", validate="one_to_one")
        if not fundamentals.empty:
            result = result.merge(fundamentals, on="symbol", how="left", validate="one_to_one")
        if not realtime.empty:
            latest = realtime.sort_values("timestamp_local").drop_duplicates("symbol", keep="last")
            realtime_columns = [
                column
                for column in (
                    "symbol", "timestamp_local", "match_price", "match_quantity",
                    "side", "total_volume", "market_id", "board_id",
                    "average_price", "open", "high", "low", "ceiling_price",
                    "floor_price",
                )
                if column in latest.columns
            ]
            latest = latest[realtime_columns].rename(
                columns={
                    "timestamp_local": "realtime_as_of",
                    "match_price": "realtime_price",
                }
            )
            result = result.merge(latest, on="symbol", how="left", validate="one_to_one")
        if not foreign_flow.empty:
            latest_foreign = AnalyticsPipeline._summarize_foreign_flow(foreign_flow)
            result = result.merge(
                latest_foreign, on="symbol", how="left", validate="one_to_one"
            )
        result["price_unit_vnd"] = 1000.0
        result["board_lot_size"] = 100
        return result

    @staticmethod
    def _rank_stocks(snapshot: pd.DataFrame) -> pd.DataFrame:
        if snapshot.empty:
            return snapshot.copy()
        ranked = snapshot.copy()
        eligible = (
            ranked["signal_status"].eq("ELIGIBLE")
            if "signal_status" in ranked.columns
            else pd.Series(False, index=ranked.index)
        )

        def percentile(column: str, higher_is_better: bool = True, positive_only: bool = False) -> pd.Series:
            if column not in ranked.columns:
                return pd.Series(np.nan, index=ranked.index)
            values = pd.to_numeric(ranked[column], errors="coerce").where(eligible)
            if positive_only:
                values = values.where(values > 0)
            return values.rank(pct=True, ascending=higher_is_better) * 100

        pe_column = "pe_quarter" if "pe_quarter" in ranked.columns else "pe_year"
        pb_column = "pb_quarter" if "pb_quarter" in ranked.columns else "pb_year"
        roe_column = "roe_quarter" if "roe_quarter" in ranked.columns else "roe_year"
        roa_column = "roa_quarter" if "roa_quarter" in ranked.columns else "roa_year"
        debt_column = (
            "debt_to_equity_quarter"
            if "debt_to_equity_quarter" in ranked.columns
            else "debt_to_equity_year"
        )

        ranked["valuation_score"] = pd.concat(
            [percentile(pe_column, False, True), percentile(pb_column, False, True)], axis=1
        ).mean(axis=1, skipna=True)
        ranked["quality_score"] = pd.concat(
            [percentile(roe_column), percentile(roa_column)], axis=1
        ).mean(axis=1, skipna=True)
        ranked["safety_score"] = percentile(debt_column, False)
        ranked["momentum_score"] = percentile("return_20d")

        score_columns = ["valuation_score", "quality_score", "safety_score", "momentum_score"]
        weights = pd.Series(
            {"valuation_score": 0.25, "quality_score": 0.30, "safety_score": 0.20, "momentum_score": 0.25}
        )
        available_weights = ranked[score_columns].notna().mul(weights, axis=1)
        ranked["composite_score"] = (
                ranked[score_columns].mul(weights, axis=1).sum(axis=1, skipna=True)
                / available_weights.sum(axis=1).replace(0, np.nan)
        )
        ranked.loc[~eligible, [*score_columns, "composite_score"]] = np.nan
        ranked["rank"] = ranked["composite_score"].rank(method="min", ascending=False).astype("Int64")
        return ranked.sort_values(["rank", "symbol"], na_position="last")

    # Lưu trữ --------------------------------------------------------------
    def store(self) -> None:
        """Lưu CSV sạch, báo cáo JSON và các bảng truy vấn nhanh trong SQLite."""

        clean_dir = self.config.output_dir / "clean"
        rejected_dir = self.config.output_dir / "rejected"
        analysis_dir = self.config.output_dir / "analysis"
        for directory in [clean_dir, rejected_dir, analysis_dir, self.config.database_path.parent]:
            directory.mkdir(parents=True, exist_ok=True)

        for name, frame in self.cleaned.items():
            frame.to_csv(clean_dir / f"{name}.csv", index=False, encoding="utf-8-sig")
        self.reconciled_fundamentals.to_csv(
            clean_dir / "fundamentals_reconciled.csv", index=False, encoding="utf-8-sig"
        )
        self.source_audit.to_csv(
            analysis_dir / "source_audit.csv", index=False, encoding="utf-8-sig"
        )
        for name, frame in self.rejected.items():
            if not frame.empty:
                frame.to_csv(rejected_dir / f"{name}.csv", index=False, encoding="utf-8-sig")
        for name, frame in self.analysis.items():
            frame.to_csv(analysis_dir / f"{name}.csv", index=False, encoding="utf-8-sig")

        report_path = self.config.output_dir / "quality_report.json"
        report_path.write_text(
            json.dumps(self.quality_report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        self._store_sqlite()

    def _store_sqlite(self) -> None:
        table_frames = {
            "price_history": self.cleaned["history"],
            "market_history": self.cleaned["market_history"],
            "realtime_quotes": self.cleaned["realtime"],
            "foreign_flow": self.cleaned["foreign_flow"],
            "fundamentals": self.cleaned["fundamentals"],
            "fundamentals_reconciled": self.reconciled_fundamentals,
            "screening": self.cleaned["screening"],
            "source_audit": self.source_audit,
            "data_quality": self.analysis["data_quality"],
            "market_context": self.analysis["market_context"],
            "stock_snapshot": self.analysis["stock_snapshot"],
            "screening_ranked": self.analysis["screening_ranked"],
        }
        with sqlite3.connect(self.config.database_path) as connection:
            for table, frame in table_frames.items():
                self._sqlite_safe_frame(frame).to_sql(table, connection, if_exists="replace", index=False)
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_price_symbol_time
                    ON price_history(symbol, timestamp_utc);
                CREATE INDEX IF NOT EXISTS idx_realtime_symbol_time
                    ON realtime_quotes(symbol, timestamp_local);
                CREATE INDEX IF NOT EXISTS idx_market_symbol_time
                    ON market_history(symbol, timestamp_utc);
                CREATE INDEX IF NOT EXISTS idx_foreign_symbol_time
                    ON foreign_flow(symbol, trading_date);
                CREATE INDEX IF NOT EXISTS idx_fundamental_symbol_period
                    ON fundamentals(symbol, period_type, period);
                CREATE INDEX IF NOT EXISTS idx_screening_symbol_period
                    ON screening(symbol, period_type, period);
                CREATE INDEX IF NOT EXISTS idx_reconciled_symbol_period
                    ON fundamentals_reconciled(symbol, period_type, period);
                CREATE INDEX IF NOT EXISTS idx_quality_symbol
                    ON data_quality(symbol, signal_status);
                """
            )

    @staticmethod
    def _sqlite_safe_frame(frame: pd.DataFrame) -> pd.DataFrame:
        result = frame.copy()
        for column in result.columns:
            if isinstance(result[column].dtype, pd.DatetimeTZDtype):
                result[column] = result[column].astype("string")
        return result


def parse_args() -> argparse.Namespace:
    """Đọc tham số dòng lệnh."""

    parser = argparse.ArgumentParser(
        description="Làm sạch, chuẩn hóa, kiểm tra, lưu trữ và phân tích dữ liệu DNSE."
    )
    parser.add_argument(
        "--input-dir",
        type=_resolve_project_path,
        default=DEFAULT_DATA_DIR,
        help="Thư mục dữ liệu do crawler tạo ra (mặc định: <thư mục dnse>/data).",
    )
    parser.add_argument(
        "--output-dir",
        type=_resolve_project_path,
        default=DEFAULT_ANALYTICS_DIR,
        help="Thư mục kết quả (mặc định: <thư mục dnse>/data/analytics).",
    )
    parser.add_argument(
        "--database",
        type=_resolve_project_path,
        default=None,
        help="Đường dẫn SQLite; mặc định là <output-dir>/market_analytics.sqlite.",
    )
    parser.add_argument(
        "--fail-on-quality-errors",
        action="store_true",
        help="Trả mã lỗi nếu kiểm tra chất lượng có lỗi nghiêm trọng.",
    )
    parser.add_argument(
        "--expected-symbols",
        default="",
        help="Danh sách mã bắt buộc, phân tách bằng dấu phẩy; mã thiếu dữ liệu sẽ bị NO SIGNAL.",
    )
    parser.add_argument(
        "--max-price-age-days",
        type=float,
        default=7.0,
        help="Tuổi tối đa của dữ liệu giá lịch sử, tính theo ngày (mặc định: 7).",
    )
    parser.add_argument(
        "--max-realtime-age-minutes",
        type=float,
        default=1_440.0,
        help="Tuổi tối đa của realtime, tính theo phút (mặc định: 1440).",
    )
    parser.add_argument(
        "--max-quarter-age-days",
        type=int,
        default=200,
        help="Tuổi tối đa của báo cáo quý (mặc định: 200 ngày).",
    )
    parser.add_argument(
        "--max-year-age-days",
        type=int,
        default=550,
        help="Tuổi tối đa của báo cáo năm (mặc định: 550 ngày).",
    )
    parser.add_argument(
        "--source-tolerance",
        type=float,
        default=0.20,
        help="Chênh lệch tương đối tối đa giữa các nguồn (mặc định: 0.20).",
    )
    parser.add_argument(
        "--minimum-history-rows",
        type=int,
        default=50,
        help="Số phiên tối thiểu để đủ RSI/MACD/SMA50 và cho phép tín hiệu (mặc định: 50).",
    )
    parser.add_argument(
        "--minimum-fundamental-coverage",
        type=float,
        default=0.60,
        help="Tỷ lệ tối thiểu của PE/PB/ROE/ROA/D/E (mặc định: 0.60).",
    )
    parser.add_argument(
        "--require-realtime-for-signal",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Bắt buộc realtime còn mới để cho phép phát tín hiệu.",
    )
    parser.add_argument("--verbose", action="store_true", help="Hiển thị log chi tiết.")
    return parser.parse_args()


def main() -> int:
    """Điểm vào khi chạy file trực tiếp."""

    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    output_dir = args.output_dir.resolve()
    database_path = (args.database or output_dir / "market_analytics.sqlite").resolve()
    expected_symbols = tuple(
        dict.fromkeys(
            symbol.strip().upper()
            for symbol in args.expected_symbols.split(",")
            if symbol.strip()
        )
    )
    config = PipelineConfig(
        input_dir=args.input_dir.resolve(),
        output_dir=output_dir,
        database_path=database_path,
        fail_on_quality_errors=args.fail_on_quality_errors,
        expected_symbols=expected_symbols,
        max_price_age_days=args.max_price_age_days,
        max_realtime_age_minutes=args.max_realtime_age_minutes,
        max_quarter_age_days=args.max_quarter_age_days,
        max_year_age_days=args.max_year_age_days,
        source_tolerance=args.source_tolerance,
        minimum_history_rows=args.minimum_history_rows,
        minimum_fundamental_coverage=args.minimum_fundamental_coverage,
        require_realtime_for_signal=args.require_realtime_for_signal,
    )

    try:
        report = AnalyticsPipeline(config).run()
    except Exception:
        LOGGER.exception("Pipeline thất bại")
        return 1

    print(
        json.dumps(
            {
                "status": report["status"],
                "eligible_symbols": report["signal_gate"]["eligible_symbols"],
                "blocked_symbols": report["signal_gate"]["blocked_symbols"],
                "output_dir": str(output_dir),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
