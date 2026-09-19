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
    "timestamp",
    "foreign_buy_volume",
    "foreign_sell_volume",
    "foreign_net_volume",
    "foreign_net_buy_volume",
    "foreign_net_sell_volume",
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
SOURCE_PRIORITY = {"VCI": 0, "KBS": 1}


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
        realtime_files = sorted((self.config.input_dir / "realtime").glob("*.jsonl"))
        foreign_files = sorted((self.config.input_dir / "foreign").glob("*.csv"))
        screening_files = sorted((self.config.input_dir / "fundamental").glob("screening_*.csv"))
        fundamental_files = sorted(
            path
            for path in (self.config.input_dir / "fundamental").glob("*/*_fundamentals.csv")
            if path.is_file()
        )

        self.raw["history"] = self._read_history_files(history_files)
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
            "realtime": ["symbol", "time", "matchPrice", "matchQtty", "side"],
            "foreign_flow": ["symbol", "collected_at", "source"],
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

        self.cleaned["history"] = self._clean_history(self.raw["history"])
        self.cleaned["realtime"] = self._clean_realtime(self.raw["realtime"])
        self.cleaned["foreign_flow"] = self._clean_foreign_flow(
            self.raw["foreign_flow"]
        )
        self.cleaned["screening"] = self._clean_fundamentals(self.raw["screening"], "screening")
        self.cleaned["fundamentals"] = self._clean_fundamentals(
            self.raw["fundamentals"], "fundamentals"
        )

    def _clean_history(self, frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            self.rejected["history"] = pd.DataFrame()
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
        self.rejected["history"] = rejected

        valid = data.loc[~invalid].copy()
        before = len(valid)
        valid = valid.drop_duplicates(["symbol", "interval", "timestamp_utc"], keep="last")
        self.stats["history"]["duplicate_rows_removed"] = before - len(valid)
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
        ]:
            values = data[column] if column in data.columns else pd.Series(np.nan, index=data.index)
            data[column] = pd.to_numeric(values, errors="coerce")
        data["collected_at"] = pd.to_datetime(
            data.get("collected_at"), utc=True, errors="coerce"
        )
        expected_net = data["foreign_buy_volume"] - data["foreign_sell_volume"]
        net_mismatch = (
                data["foreign_net_volume"].notna()
                & ~np.isclose(
            data["foreign_net_volume"], expected_net, rtol=1e-9, atol=1e-6
        )
        )
        invalid = (
                data["symbol"].isna()
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
        before = len(valid)
        valid = valid.drop_duplicates(["symbol", "collected_at", "source"], keep="last")
        self.stats["foreign_flow"]["duplicate_rows_removed"] = before - len(valid)
        return valid[FOREIGN_FLOW_COLUMNS].sort_values(["symbol", "collected_at"])

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

        for column in FUNDAMENTAL_NUMERIC_COLUMNS:
            if column in data.columns:
                data[column] = pd.to_numeric(data[column], errors="coerce").replace([np.inf, -np.inf], np.nan)

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
        return result[HISTORY_COLUMNS].sort_values(["symbol", "interval", "timestamp_utc"])

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

        for (symbol, period_type), symbol_rows in data.groupby(
                ["symbol", "period_type"], sort=True
        ):
            latest_period = symbol_rows["period"].max()
            candidates = symbol_rows[symbol_rows["period"] == latest_period].copy()
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
                    "period": latest_period,
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
            "realtime": {"symbol", "timestamp_local", "match_price", "match_quantity"},
            "foreign_flow": {
                "symbol",
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

        symbols = set(self.config.expected_symbols)
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
        rows: list[dict[str, Any]] = []
        for symbol in sorted(symbols):
            blocking: list[str] = []
            warnings: list[str] = []

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
            if symbol_realtime.empty:
                message = "Thiếu realtime/API realtime không trả dữ liệu"
                (blocking if self.config.require_realtime_for_signal else warnings).append(message)
            else:
                latest_realtime = symbol_realtime["crawled_at_utc"].max()
                realtime_age_minutes = float((now - latest_realtime) / pd.Timedelta(minutes=1))
                if realtime_age_minutes < -5:
                    blocking.append("Thời gian realtime nằm trong tương lai")
                elif realtime_age_minutes > self.config.max_realtime_age_minutes:
                    message = f"Realtime quá cũ ({realtime_age_minutes:.0f} phút)"
                    (blocking if self.config.require_realtime_for_signal else warnings).append(message)

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
            if symbol_fundamental.empty:
                blocking.append("Thiếu báo cáo tài chính/API cơ bản không trả dữ liệu")
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
                    blocking.append(f"Chỉ tiêu cơ bản chỉ đủ {coverage:.0%}")
                if not source_consistent:
                    blocking.append(
                        f"Nguồn tài chính mâu thuẫn: {selected.get('source_conflicts', '')}"
                    )

                if not published_date:
                    blocking.append(
                        "Thiếu ngày công bố BCTC; không thể kiểm soát dữ liệu nhìn trước"
                    )
                else:
                    parsed_published = pd.to_datetime(
                        published_date, utc=True, errors="coerce"
                    )
                    if pd.isna(parsed_published):
                        blocking.append("Ngày công bố BCTC sai định dạng")
                    elif parsed_published > now:
                        blocking.append("Ngày công bố BCTC nằm trong tương lai")
                if not financial_collected_at:
                    blocking.append("Thiếu thời điểm thu thập báo cáo tài chính")

                period_end = self._period_end(str(fundamental_period))
                if period_end is None:
                    blocking.append("Không xác định được ngày kết thúc kỳ tài chính")
                else:
                    fundamental_age_days = float((now.normalize() - period_end).days)
                    maximum_age = (
                        self.config.max_quarter_age_days
                        if fundamental_period and "-Q" in fundamental_period
                        else self.config.max_year_age_days
                    )
                    if fundamental_age_days > maximum_age:
                        blocking.append(
                            f"Báo cáo tài chính quá cũ ({fundamental_age_days:.0f} ngày)"
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
    def _iso_value(value: Any) -> str | None:
        if pd.isna(value):
            return None
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    # 5. Phân tích ---------------------------------------------------------
    def analyze(self) -> None:
        """Tính chỉ báo giá, ghép dữ liệu cơ bản và chấm điểm sàng lọc."""

        price = self._analyze_price_history(self.cleaned["history"])
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
        self.analysis["stock_snapshot"] = snapshot
        self.analysis["screening_ranked"] = ranked

    @staticmethod
    def _analyze_price_history(history: pd.DataFrame) -> pd.DataFrame:
        columns = [
            "symbol",
            "price_as_of",
            "latest_close",
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

            records.append(
                {
                    "symbol": symbol,
                    "price_as_of": latest["date"],
                    "latest_close": latest_close,
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
                    "indicator_data_complete": indicator_data_complete,
                }
            )
        return pd.DataFrame(records, columns=columns)

    @staticmethod
    def _period_return(close: pd.Series, periods: int) -> float:
        if len(close) <= periods or close.iloc[-periods - 1] <= 0:
            return np.nan
        return float(close.iloc[-1] / close.iloc[-periods - 1] - 1)

    def _latest_fundamentals(self) -> pd.DataFrame:
        source = self.reconciled_fundamentals
        if source.empty:
            return pd.DataFrame(columns=["symbol"])

        pieces: list[pd.DataFrame] = []
        for period_type in ["quarter", "year"]:
            part = source[source["period_type"] == period_type].copy()
            if part.empty:
                continue
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
            latest = latest[
                ["symbol", "timestamp_local", "match_price", "match_quantity", "side", "total_volume"]
            ].rename(columns={"timestamp_local": "realtime_as_of", "match_price": "realtime_price"})
            result = result.merge(latest, on="symbol", how="left", validate="one_to_one")
        if not foreign_flow.empty:
            latest_foreign = foreign_flow.sort_values("collected_at").drop_duplicates(
                "symbol", keep="last"
            )
            latest_foreign = latest_foreign[
                [
                    "symbol",
                    "collected_at",
                    "foreign_buy_volume",
                    "foreign_sell_volume",
                    "foreign_net_volume",
                    "foreign_net_buy_volume",
                    "foreign_net_sell_volume",
                    "source",
                ]
            ].rename(
                columns={
                    "collected_at": "foreign_as_of",
                    "source": "foreign_source",
                }
            )
            result = result.merge(
                latest_foreign, on="symbol", how="left", validate="one_to_one"
            )
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
            "realtime_quotes": self.cleaned["realtime"],
            "foreign_flow": self.cleaned["foreign_flow"],
            "fundamentals": self.cleaned["fundamentals"],
            "fundamentals_reconciled": self.reconciled_fundamentals,
            "screening": self.cleaned["screening"],
            "source_audit": self.source_audit,
            "data_quality": self.analysis["data_quality"],
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
                CREATE INDEX IF NOT EXISTS idx_foreign_symbol_time
                    ON foreign_flow(symbol, collected_at);
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
