"""Thu thap du lieu lich su va snapshot moi nhat tu DNSE OpenAPI.

Thong tin xac thuc duoc doc tu bien moi truong hoac file `.env`:

    DNSE_API_KEY=...
    DNSE_API_SECRET=...

SDK `dnse` chiu trach nhiem ky HMAC cho moi HTTP request.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import logging
import math
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from dnse import DnseClient
    from dnse.exceptions import DnseAuthError, DnseError, DnseRateLimitError
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Thieu SDK DNSE. Hay cai trong interpreter PyCharm: pip install dnse"
    ) from exc

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - .env la tuy chon
    load_dotenv = None

if load_dotenv is not None:
    # Uu tien file .env nam cung crawler, sau do den .env cua thu muc dang chay.
    load_dotenv(Path(__file__).with_name(".env"))
    load_dotenv()

LOGGER = logging.getLogger("dnse_api_crawl")
DEFAULT_DNSE_BASE_URL = "https://openapi.dnse.com.vn"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
VIETNAM_TIMEZONE = timezone(timedelta(hours=7))
PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def _resolve_project_path(value: str | Path) -> Path:
    """Chuan hoa duong dan theo thu muc dnse va loai tien to dnse bi lap."""

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


def _env_flag(name: str, default: bool = False) -> bool:
    """Doc bien moi truong dang boolean."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


@contextmanager
def _without_proxy_environment() -> Iterable[None]:
    """Tam bo proxy khi khoi tao HTTP client, sau do khoi phuc lai."""
    old_values = {name: os.environ.get(name) for name in PROXY_ENV_NAMES}
    try:
        for name in PROXY_ENV_NAMES:
            os.environ.pop(name, None)
        yield
    finally:
        for name, value in old_values.items():
            if value is not None:
                os.environ[name] = value


@dataclass(slots=True)
class DNSEConfig:
    """Cau hinh ket noi REST API cua DNSE."""

    base_url: str = field(
        default_factory=lambda: os.getenv(
            "DNSE_BASE_URL", DEFAULT_DNSE_BASE_URL
        ).rstrip("/")
    )
    api_key: str = field(default_factory=lambda: os.getenv("DNSE_API_KEY", "").strip())
    api_secret: str = field(
        default_factory=lambda: os.getenv("DNSE_API_SECRET", "").strip()
    )
    request_timeout: int = int(os.getenv("DNSE_TIMEOUT", "20"))
    max_retries: int = int(os.getenv("DNSE_MAX_RETRIES", "3"))
    retry_sleep: float = float(os.getenv("DNSE_RETRY_SLEEP", "1.5"))
    use_system_proxy: bool = field(
        default_factory=lambda: _env_flag("DNSE_USE_SYSTEM_PROXY", False)
    )

    symbols_endpoint: str = os.getenv(
        "DNSE_SYMBOLS_ENDPOINT", "/market/instruments"
    )
    history_endpoint: str = os.getenv("DNSE_HISTORY_ENDPOINT", "/price/ohlc")
    foreign_endpoint: str = os.getenv(
        "DNSE_FOREIGN_ENDPOINT", "/price/{symbol}/foreign-trading"
    )
    realtime_endpoint: str = os.getenv(
        "DNSE_REALTIME_ENDPOINT", "/price/{symbol}/trades/latest"
    )

    def validate(self) -> None:
        """Kiem tra cau hinh bat buoc truoc khi goi API."""
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError(
                "DNSE_BASE_URL cua REST API phai bat dau bang http:// hoac https://; "
                "khong dung wss:// tai day."
            )
        if not self.api_key or not self.api_secret:
            raise ValueError(
                "Can khai bao ca DNSE_API_KEY va DNSE_API_SECRET de ky HMAC."
            )


class DNSEClient:
    """Lop boc SDK DNSE, bo sung retry va chuan hoa du lieu."""

    def __init__(self, config: DNSEConfig) -> None:
        config.validate()
        self.config = config
        if config.use_system_proxy:
            self.session = self._create_session(config)
        else:
            with _without_proxy_environment():
                self.session = self._create_session(config)

    @staticmethod
    def _create_session(config: DNSEConfig) -> DnseClient:
        """Khoi tao SDK client de SDK tu ky HMAC."""
        return DnseClient(
            api_key=config.api_key,
            api_secret=config.api_secret,
            base_url=config.base_url,
            timeout=config.request_timeout,
        )

    def close(self) -> None:
        """Dong ket noi HTTP cua SDK."""
        self.session.close()

    def request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        """Goi endpoint bang SDK DNSE va retry cac loi tam thoi."""
        last_error: Exception | None = None

        for attempt in range(1, self.config.max_retries + 1):
            try:
                response = self.session.request(method, endpoint, **kwargs)
                if not response.content:
                    return None
                return response.json()
            except DnseAuthError as exc:
                raise PermissionError(
                    "DNSE tu choi xac thuc. Kiem tra cap DNSE_API_KEY/DNSE_API_SECRET."
                ) from exc
            except (DnseRateLimitError, DnseError, ValueError) as exc:
                last_error = exc
                if attempt == self.config.max_retries:
                    break
                sleep_seconds = self.config.retry_sleep * attempt
                LOGGER.warning(
                    "Goi %s that bai lan %s/%s: %s. Thu lai sau %.1fs",
                    endpoint,
                    attempt,
                    self.config.max_retries,
                    exc,
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)

        detail = (
            f"{type(last_error).__name__}: {last_error}"
            if last_error
            else "khong ro loi"
        )
        raise RuntimeError(
            f"Goi API DNSE that bai: {endpoint}. Chi tiet: {detail}"
        ) from last_error

    def list_symbols(self, exchange: str | None = None) -> list[str]:
        """Lay co phieu niem yet tren HOSE, HNX va UPCOM."""
        market_ids = {"HOSE": "STO", "HNX": "STX", "UPCOM": "UPX"}
        selected_markets = (
            [market_ids.get(exchange.upper(), exchange.upper())]
            if exchange
            else list(market_ids.values())
        )
        symbols: list[str] = []
        for market_id in selected_markets:
            params: dict[str, Any] = {
                "marketId": market_id,
                "securityGroupId": "ST",
                "limit": 100,
                "page": 1,
            }
            while True:
                payload = self.request("GET", self.config.symbols_endpoint, params=params)
                rows = _extract_rows(payload)
                symbols.extend(_extract_symbols(rows))
                if len(rows) < params["limit"]:
                    break
                params["page"] += 1
                if params["page"] > 100:
                    raise RuntimeError(
                        f"Dung sau 100 trang cua marketId={market_id} de tranh lap vo han."
                    )

        if not symbols:
            raise RuntimeError("Khong tim thay ma co phieu trong response DNSE.")
        return sorted(set(symbols))

    def fetch_history(
            self,
            symbol: str,
            start: str,
            end: str,
            interval: str = "1D",
            asset_type: str = "STOCK",
    ) -> list[dict[str, Any]]:
        """Lay OHLCV lich su cho co phieu hoac chi so thi truong."""

        normalized_type = asset_type.strip().upper()
        if normalized_type not in {"STOCK", "INDEX"}:
            raise ValueError("asset_type chi chap nhan STOCK hoac INDEX")
        params = {
            "type": normalized_type,
            "symbol": symbol.upper(),
            "resolution": _normalize_resolution(interval),
            "from": _date_to_unix(start, end_of_day=False),
            "to": _date_to_unix(end, end_of_day=True),
        }
        payload = self.request("GET", self.config.history_endpoint, params=params)
        return [_with_symbol(row, symbol) for row in _extract_ohlc_rows(payload)]

    def fetch_foreign_sessions(
            self,
            symbol: str,
            end: str,
            sessions: int = 5,
            lookback_days: int = 15,
            board_id: str = "G1",
    ) -> list[dict[str, Any]]:
        """Lay tong mua/ban khoi ngoai cua cac phien gan nhat tu DNSE."""

        if sessions < 1:
            raise ValueError("foreign sessions phai lon hon 0")
        if lookback_days < sessions:
            raise ValueError("foreign lookback_days phai lon hon hoac bang sessions")
        try:
            end_date = datetime.fromisoformat(end).date()
        except ValueError as exc:
            raise ValueError(f"Ngay ket thuc khong hop le: {end!r}") from exc

        collected_at = datetime.now(timezone.utc).isoformat()
        output: list[dict[str, Any]] = []
        endpoint = self.config.foreign_endpoint.format(symbol=symbol.upper())
        for offset in range(lookback_days):
            trading_date = end_date - timedelta(days=offset)
            if trading_date.weekday() >= 5:
                continue
            day_text = trading_date.isoformat()
            try:
                payload = self.request(
                    "GET",
                    endpoint,
                    params={
                        "boardId": board_id,
                        "from": _date_to_unix(day_text, end_of_day=False),
                        "to": _date_to_unix(day_text, end_of_day=True),
                        "limit": 1000,
                    },
                )
            except (PermissionError, RuntimeError) as exc:
                LOGGER.warning(
                    "Khong lay duoc khoi ngoai %s ngay %s: %s",
                    symbol,
                    day_text,
                    exc,
                )
                continue

            rows = _extract_foreign_rows(payload)
            if not rows:
                continue
            latest = max(rows, key=_foreign_timestamp_sort_key)
            canonical = _canonical_foreign_flow_row(
                latest,
                collected_at=collected_at,
                source="DNSE",
                symbol=symbol,
                trading_date=day_text,
            )
            if canonical is not None:
                output.append(canonical)
            if len(output) >= sessions:
                break
        return sorted(output, key=lambda row: str(row["trading_date"]))

    def fetch_realtime(self, symbols: Iterable[str]) -> list[dict[str, Any]]:
        """Lay giao dich khop gan nhat cua tung ma qua REST API."""
        rows: list[dict[str, Any]] = []
        crawl_time = datetime.now(timezone.utc).isoformat()

        for raw_symbol in symbols:
            symbol = raw_symbol.strip().upper()
            if not symbol:
                continue
            try:
                endpoint = self.config.realtime_endpoint.format(symbol=symbol)
                payload = self.request("GET", endpoint, params={"boardId": "G1"})
                rows.extend(
                    _canonical_realtime_row(row, symbol, crawl_time)
                    for row in _extract_rows(payload)
                )
            except (PermissionError, RuntimeError) as exc:
                LOGGER.error("Bo qua realtime %s vi loi: %s", symbol, exc)
        return rows


class ForeignFlowClient:
    """Lay khoi luong mua/ban cua khoi ngoai tu bang gia KBS."""

    def __init__(self, source: str = "KBS", use_system_proxy: bool = False) -> None:
        self.source = source.upper()
        self.use_system_proxy = use_system_proxy
        os.environ.setdefault("VNSTOCK_TELEMETRY", "off")
        os.environ.setdefault("VNSTOCK_DISABLE_AGENT_SETUP", "1")
        os.environ.setdefault("VNSTOCK_AGENT_TARGETS", "none")

    @contextmanager
    def _network_context(self) -> Iterable[None]:
        if self.use_system_proxy:
            yield
        else:
            with _without_proxy_environment():
                yield

    def fetch(self, symbols: Iterable[str], batch_size: int = 50) -> list[dict[str, Any]]:
        """Lay snapshot khoi ngoai; moi dong luon ghi ro nguon va thoi diem thu thap."""

        try:
            from vnstock import Trading
        except ImportError as exc:
            raise RuntimeError(
                "Thieu vnstock. Hay cai bang interpreter PyCharm: pip install vnstock"
            ) from exc

        if batch_size < 1:
            raise ValueError("foreign batch_size phai lon hon 0")
        normalized_symbols = sorted(
            {symbol.strip().upper() for symbol in symbols if symbol.strip()}
        )
        collected_at = datetime.now(timezone.utc).isoformat()
        output: list[dict[str, Any]] = []
        with self._network_context():
            trading = Trading(source=self.source, show_log=False)
            for start in range(0, len(normalized_symbols), batch_size):
                batch = normalized_symbols[start: start + batch_size]
                frame = trading.price_board(symbols_list=batch, get_all=True)
                if frame is None or getattr(frame, "empty", True):
                    LOGGER.warning("Nguon %s khong tra du lieu khoi ngoai cho %s", self.source, batch)
                    continue
                for row in frame.to_dict(orient="records"):
                    canonical = _canonical_foreign_flow_row(row, collected_at, self.source)
                    if canonical is not None:
                        output.append(canonical)
        return output


class FundamentalClient:
    """Lay bao cao tai chinh tu provider fundamental cua Vnstock."""

    REPORT_METHODS = {
        "ratio": "ratio",
        "income": "income_statement",
        "balance": "balance_sheet",
        "cashflow": "cash_flow",
    }

    def __init__(
            self,
            source: str = "VCI",
            period_limit: int = 20,
            use_system_proxy: bool = False,
    ) -> None:
        self.source = source.upper()
        self.period_limit = period_limit
        self.use_system_proxy = use_system_proxy
        os.environ.setdefault("VNSTOCK_TELEMETRY", "off")
        os.environ.setdefault("VNSTOCK_DISABLE_AGENT_SETUP", "1")
        os.environ.setdefault("VNSTOCK_AGENT_TARGETS", "none")

    @contextmanager
    def _network_context(self) -> Iterable[None]:
        if self.use_system_proxy:
            yield
        else:
            with _without_proxy_environment():
                yield

    def fetch_symbol(
            self,
            symbol: str,
            period: str,
            reports: Iterable[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Lay va chuan hoa cac bao cao cua mot ma theo nam hoac quy."""
        with self._network_context():
            try:
                from vnstock.api.financial import Finance
            except ImportError as exc:
                raise RuntimeError(
                    "Thieu vnstock. Hay cai bang interpreter PyCharm: pip install vnstock"
                ) from exc

            finance = Finance(
                source=self.source,
                symbol=symbol.upper(),
                period=period,
                get_all=True,
                show_log=False,
            )
            output: dict[str, list[dict[str, Any]]] = {}
            for report in reports:
                method_name = self.REPORT_METHODS[report]
                frame = self._fetch_frame(finance, method_name, period)
                output[report] = _financial_matrix_to_rows(
                    frame,
                    symbol=symbol,
                    period_type=period,
                    statement=report,
                    source=self.source,
                )
            return output

    def _fetch_frame(self, finance: Any, method_name: str, period: str) -> Any:
        """Lay DataFrame; VCI duoc phep tang gioi han so ky qua provider."""
        provider = getattr(finance, "provider", None)
        private_method = getattr(provider, "_get_financial_report", None)
        mapping_method = getattr(provider, "_ratio_mapping", None)
        if callable(private_method) and callable(mapping_method):
            raw_frame = private_method(
                report_type=method_name,
                period=period,
                mode="raw",
                dropna=False,
                show_log=False,
                limit=10_000,
            )
            if raw_frame is None or raw_frame.empty:
                return raw_frame

            if method_name == "ratio" and {"year", "quarter"}.issubset(raw_frame.columns):
                years = raw_frame["year"].astype(str).str.extract(r"(\d{4})", expand=False)
                quarters = raw_frame["quarter"].astype(float)
                raw_frame = raw_frame.assign(
                    _sort_year=years.astype(float),
                    _sort_quarter=quarters,
                )
                if period == "year":
                    raw_frame = raw_frame[raw_frame["_sort_quarter"] == 5]
                else:
                    raw_frame = raw_frame[raw_frame["_sort_quarter"].between(1, 4)]
                raw_frame = raw_frame.sort_values(["_sort_year", "_sort_quarter"])
            elif "yearReport" in raw_frame.columns:
                sort_columns = ["yearReport"]
                if "lengthReport" in raw_frame.columns:
                    sort_columns.append("lengthReport")
                raw_frame = raw_frame.sort_values(sort_columns)

            raw_frame = raw_frame.tail(self.period_limit).drop(
                columns=["_sort_year", "_sort_quarter"], errors="ignore"
            )
            mapped_frame = mapping_method(
                report_df=raw_frame,
                lang="en",
                style="readable",
                get_all=True,
                show_log=False,
                period_type=period,
                report_type=method_name,
            )
            return _append_financial_period_metadata(
                mapped_frame=mapped_frame,
                raw_frame=raw_frame,
                period_type=period,
            )
        public_method = getattr(finance, method_name)
        return public_method(period=period, lang="en", dropna=False, show_log=False)


def _date_to_unix(value: str, *, end_of_day: bool) -> int:
    """Doi ngay ISO sang Unix timestamp theo gio Viet Nam."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Ngay khong hop le: {value!r}. Can dinh dang YYYY-MM-DD.") from exc

    if "T" not in value and " " not in value:
        parsed = datetime.combine(
            parsed.date(),
            datetime_time.max if end_of_day else datetime_time.min,
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=VIETNAM_TIMEZONE)
    return int(parsed.timestamp())


def _normalize_resolution(value: str) -> str:
    """Chuan hoa cach viet khung thoi gian sang dinh dang DNSE."""
    normalized = value.strip().upper()
    aliases = {
        "1M": "1",
        "3M": "3",
        "5M": "5",
        "15M": "15",
        "30M": "30",
        "60M": "1H",
        "D": "1D",
        "W": "1W",
    }
    return aliases.get(normalized, normalized)


def _extract_symbols(payload: Any) -> list[str]:
    """Rut danh sach symbol tu response hoac danh sach ban ghi."""
    rows = payload if isinstance(payload, list) else _extract_rows(payload)
    symbols: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol") or row.get("ticker") or row.get("code")
        if symbol:
            symbols.append(str(symbol).strip().upper())
    return sorted(set(symbols))


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    """Rut list ban ghi tu cac lop boc JSON pho bien."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("data", "items", "rows", "result", "symbols", "quotes", "trades"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            nested_rows = _extract_rows(value)
            if nested_rows:
                return nested_rows
    return [payload]


def _extract_ohlc_rows(payload: Any) -> list[dict[str, Any]]:
    """Chuyen response OHLC dang mang cot t/o/h/l/c/v thanh tung dong."""
    data = payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        data = payload["data"]

    if isinstance(data, dict):
        column_names = ("t", "o", "h", "l", "c", "v")
        columns = {name: data.get(name) for name in column_names}
        if any(isinstance(value, list) for value in columns.values()):
            length = max(
                (len(value) for value in columns.values() if isinstance(value, list)),
                default=0,
            )
            return [
                {
                    name: value[index]
                    if isinstance(value, list) and index < len(value)
                    else None
                    for name, value in columns.items()
                }
                for index in range(length)
            ]
    return _extract_rows(payload)


def _extract_foreign_rows(payload: Any) -> list[dict[str, Any]]:
    """Rut danh sach snapshot khoi ngoai tu response DNSE."""

    if isinstance(payload, dict):
        foreigners = payload.get("foreigners")
        if isinstance(foreigners, list):
            return [row for row in foreigners if isinstance(row, dict)]
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("foreigners"), list):
            return [row for row in data["foreigners"] if isinstance(row, dict)]
    return []


def _foreign_timestamp_sort_key(row: dict[str, Any]) -> float:
    """Chuyen timestamp DNSE thanh khoa sap xep de chon snapshot cuoi phien."""

    value = _first_present(row, "timestamp", "time", "t")
    if value is None:
        return float("-inf")
    try:
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000
        return numeric
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def _with_symbol(row: dict[str, Any], symbol: str) -> dict[str, Any]:
    """Bao dam ban ghi luon co symbol."""
    output = dict(row)
    output.setdefault("symbol", symbol.upper())
    return output


def _first_present(row: dict[str, Any], *names: str) -> Any:
    """Lay gia tri dau tien co ton tai, khong bo qua so 0 hop le."""

    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return None


def _append_financial_period_metadata(
        mapped_frame: Any,
        raw_frame: Any,
        period_type: str,
) -> Any:
    """Giu ngay cong bo bi Vnstock loai khi chuyen BCTC sang ma tran."""

    if (
            mapped_frame is None
            or getattr(mapped_frame, "empty", True)
            or raw_frame is None
            or getattr(raw_frame, "empty", True)
            or "item_id" not in mapped_frame.columns
    ):
        return mapped_frame

    metadata_columns = {"item", "item_en", "item_id"}
    available_periods = {
        str(column): column
        for column in mapped_frame.columns
        if column not in metadata_columns
    }
    published_by_period: dict[Any, Any] = {}

    for raw_row in raw_frame.to_dict(orient="records"):
        year = _first_present(raw_row, "year", "yearReport", "YearPeriod")
        quarter = _first_present(
            raw_row,
            "quarter",
            "lengthReport",
            "quarterReport",
        )
        try:
            year_label = str(int(float(year)))
        except (TypeError, ValueError):
            continue

        period_label = year_label
        if period_type == "quarter":
            try:
                quarter_number = int(float(quarter))
            except (TypeError, ValueError):
                continue
            if not 1 <= quarter_number <= 4:
                continue
            period_label = f"{year_label}-Q{quarter_number}"

        frame_column = available_periods.get(period_label)
        if frame_column is None:
            continue
        published_date = _first_present(
            raw_row,
            "published_date",
            "publish_date",
            "public_date",
            "publicDate",
            "publishDate",
            "report_date",
            "reportDate",
            "ReportDate",
            "release_date",
        )
        if published_date is not None and published_date != "":
            published_by_period[frame_column] = published_date

    if not published_by_period:
        return mapped_frame

    existing_item_ids = set(mapped_frame["item_id"].dropna().astype(str))
    if "published_date" in existing_item_ids:
        return mapped_frame

    metadata_row = {column: None for column in mapped_frame.columns}
    metadata_row.update(
        {
            "item": "Ngay cong bo bao cao",
            "item_en": "Report publication date",
            "item_id": "published_date",
            **published_by_period,
        }
    )
    mapped_frame.loc[len(mapped_frame)] = metadata_row
    return mapped_frame


def _canonical_realtime_row(
        row: dict[str, Any], symbol: str, collected_at: str
) -> dict[str, Any]:
    """Them schema realtime on dinh, dong thoi giu lai cac truong goc DNSE."""

    output = _with_symbol(row, symbol)
    output.update(
        {
            "symbol": symbol.upper(),
            "timestamp": _first_present(row, "timestamp", "time", "t"),
            "open": _first_present(row, "open", "openPrice", "o"),
            "high": _first_present(row, "high", "highestPrice", "h"),
            "low": _first_present(row, "low", "lowestPrice", "l"),
            "close": _first_present(
                row, "close", "matchPrice", "closePrice", "price", "c"
            ),
            "volume": _first_present(
                row,
                "volume",
                "totalVolumeTraded",
                "volume_accumulated",
                "accumulatedVolume",
                "v",
            ),
            "collected_at": collected_at,
            # Giu alias cu de analytics va cac consumer cu khong bi vo.
            "crawled_at_utc": collected_at,
        }
    )
    return output


def _to_float(value: Any) -> float | None:
    """Doi gia tri so tu API; tra None neu khong hop le."""

    try:
        if value is None or value == "":
            return None
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    except (TypeError, ValueError):
        return None


def _canonical_foreign_flow_row(
        row: dict[str, Any],
        collected_at: str,
        source: str,
        symbol: str | None = None,
        trading_date: str | None = None,
) -> dict[str, Any] | None:
    """Chuan hoa khoi ngoai va tinh mua rong/ban rong tu khoi luong that."""

    normalized_symbol = str(
        symbol or _first_present(row, "symbol", "code", "ticker") or ""
    ).strip().upper()
    buy_volume = _to_float(
        _first_present(
            row,
            "foreign_buy_volume",
            "totalBuyVolume",
            "foreignBuyVolume",
            "buyVolume",
            "FB",
        )
    )
    sell_volume = _to_float(
        _first_present(
            row,
            "foreign_sell_volume",
            "totalSellVolume",
            "foreignSellVolume",
            "sellVolume",
            "FS",
        )
    )
    if (
            not normalized_symbol
            or buy_volume is None
            or sell_volume is None
            or buy_volume < 0
            or sell_volume < 0
    ):
        return None
    net_volume = buy_volume - sell_volume
    buy_value = _to_float(
        _first_present(
            row,
            "foreign_buy_value",
            "totalBuyTradedAmount",
            "buyTradedAmount",
        )
    )
    sell_value = _to_float(
        _first_present(
            row,
            "foreign_sell_value",
            "totalSellTradedAmount",
            "sellTradedAmount",
        )
    )
    return {
        "symbol": normalized_symbol,
        "trading_date": trading_date,
        "timestamp": _first_present(row, "timestamp", "time", "t"),
        "foreign_buy_volume": buy_volume,
        "foreign_sell_volume": sell_volume,
        "foreign_net_volume": net_volume,
        "foreign_net_buy_volume": max(net_volume, 0.0),
        "foreign_net_sell_volume": max(-net_volume, 0.0),
        "foreign_buy_value": buy_value,
        "foreign_sell_value": sell_value,
        "foreign_net_value": (
            buy_value - sell_value
            if buy_value is not None and sell_value is not None
            else None
        ),
        "source": source.upper(),
        "collected_at": collected_at,
    }


def _financial_matrix_to_rows(
        frame: Any,
        symbol: str,
        period_type: str,
        statement: str,
        source: str,
) -> list[dict[str, Any]]:
    """Chuyen bang chi tieu x ky cua Vnstock thanh moi ky mot dong."""
    if frame is None or getattr(frame, "empty", True):
        return []
    if "item_id" not in frame.columns:
        raise ValueError(f"Bao cao {statement} cua {symbol} khong co cot item_id.")

    metadata_columns = {"item", "item_en", "item_id"}
    period_pattern = r"\d{4}-Q[1-4]" if period_type == "quarter" else r"\d{4}"
    period_columns = [
        column
        for column in frame.columns
        if column not in metadata_columns
           and re.fullmatch(period_pattern, str(column))
    ]
    if not period_columns:
        return []

    normalized = (
        frame.drop_duplicates(subset=["item_id"], keep="first")
        .set_index("item_id")[period_columns]
        .transpose()
    )
    normalized.index.name = "period"
    normalized = normalized.reset_index()
    normalized.insert(0, "period_type", period_type)
    normalized.insert(0, "symbol", symbol.upper())
    normalized.insert(2, "source", source.upper())
    normalized.insert(3, "statement", statement)
    normalized = normalized.astype(object).where(normalized.notna(), None)
    return normalized.to_dict(orient="records")


def _merge_fundamental_reports(
        report_rows: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Gop cac bao cao theo symbol/period de chatbot truy van de dang."""
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for rows in report_rows.values():
        for row in rows:
            key = (str(row["symbol"]), str(row["period_type"]), str(row["period"]))
            target = merged.setdefault(
                key,
                {
                    "symbol": row["symbol"],
                    "period_type": row["period_type"],
                    "period": row["period"],
                    "source": row.get("source"),
                },
            )
            target.update(
                {
                    name: value
                    for name, value in row.items()
                    if name
                       not in {"symbol", "period_type", "period", "source", "statement"}
                }
            )
    return sorted(merged.values(), key=lambda item: str(item["period"]), reverse=True)


def _canonical_financial_row(
        row: dict[str, Any], collected_at: str
) -> dict[str, Any]:
    """Them schema tai chinh toi thieu va uoc luong ngay cong bo khi can."""

    output = dict(row)
    source = str(row.get("source", "")).upper()

    def normalize_percent(*names: str) -> Any:
        value = _first_present(row, *names)
        if value is None or source != "KBS":
            return value
        numeric = _to_float(value)
        return numeric / 100 if numeric is not None else value

    owners_equity = _first_present(
        row,
        "owners_equity_2",
        "owners_equity_3",
        "capital_and_reserves",
        "total_owners_equity",
        "equity",
    )
    if owners_equity is None and source != "KBS":
        owners_equity = row.get("owners_equity")
    if owners_equity is None:
        total_balance = _to_float(
            _first_present(
                row,
                "total_assets",
                "total_owners_equity_and_liabilities",
                "total_liabilities_and_owners_equity",
            )
        )
        total_liabilities = _to_float(row.get("total_liabilities"))
        if total_balance is not None and total_liabilities is not None:
            owners_equity = total_balance - total_liabilities

    report_period = _first_present(row, "report_period", "period")
    published_date = _first_present(
        row,
        "published_date",
        "publish_date",
        "public_date",
        "report_date",
        "release_date",
        "ReportDate",
    )
    if published_date is None or published_date == "":
        published_date = _estimated_financial_published_date(report_period)
    output.update(
        {
            "symbol": row.get("symbol"),
            "report_period": report_period,
            "published_date": published_date,
            "revenue": _first_present(row, "revenue", "net_sales", "net_revenue"),
            "net_profit": _first_present(
                row,
                "net_profit",
                "net_profit_loss_after_tax",
                "profit_after_tax_for_shareholders_of_parent_company",
            ),
            "owners_equity": owners_equity,
            "roe": normalize_percent("roe"),
            "roa": normalize_percent("roa"),
            "roic": normalize_percent("roic", "return_on_capital_employed_roce"),
            "gross_margin": normalize_percent("gross_margin"),
            "net_margin": normalize_percent("net_margin"),
            "debt_to_equity": normalize_percent("debt_to_equity", "debtPerEquity"),
            "collected_at": collected_at,
        }
    )
    return output


def _estimated_financial_published_date(period: Any) -> str | None:
    """Tra ngay ket thuc ky cong 30 ngay theo ISO-8601 UTC."""

    value = "" if period is None else str(period).strip()
    quarter_match = re.fullmatch(r"(\d{4})-Q([1-4])", value)
    if quarter_match:
        year, quarter = map(int, quarter_match.groups())
        month = quarter * 3
        day = calendar.monthrange(year, month)[1]
        period_end = datetime(year, month, day, tzinfo=timezone.utc)
    elif re.fullmatch(r"\d{4}", value):
        period_end = datetime(int(value), 12, 31, tzinfo=timezone.utc)
    else:
        return None
    return (period_end + timedelta(days=30)).isoformat()


def _screening_row(row: dict[str, Any]) -> dict[str, Any]:
    """Chon cac chi tieu cot loi cho bo loc co ban."""

    def first_value(*names: str) -> Any:
        for name in names:
            value = row.get(name)
            if value is not None and value != "":
                return value
        return None

    def normalize_eps(value: Any) -> Any:
        if value is None or str(row.get("source", "")).upper() != "KBS":
            return value
        try:
            numeric = float(value)
            return numeric / 1_000 if abs(numeric) >= 100_000 else numeric
        except (TypeError, ValueError):
            return value

    debt_to_equity = first_value("debt_to_equity", "debtPerEquity")
    eps = first_value("eps_basic_vnd", "earnings_per_share_vnd")
    return {
        "symbol": row.get("symbol"),
        "source": row.get("source"),
        "period_type": row.get("period_type"),
        "period": row.get("period"),
        "report_period": _first_present(row, "report_period", "period"),
        "published_date": row.get("published_date"),
        "collected_at": row.get("collected_at"),
        "eps": normalize_eps(eps),
        "eps_diluted": first_value("eps_diluted_vnd", "diluted_earnings_per_share"),
        "eps_ttm": row.get("trailing_eps"),
        "pe": row.get("pe_ratio"),
        "pb": row.get("pb_ratio"),
        "ps": row.get("ps_ratio"),
        "roe": row.get("roe"),
        "roa": row.get("roa"),
        "roic": first_value("roic", "return_on_capital_employed_roce"),
        "debt_to_equity": debt_to_equity,
        "current_ratio": first_value("current_ratio", "short_term_ratio"),
        "quick_ratio": row.get("quick_ratio"),
        "gross_margin": row.get("gross_margin"),
        "net_margin": row.get("net_margin"),
        "asset_turnover": first_value("asset_turnover", "total_asset_turnover"),
        "financial_leverage": row.get("financial_leverage"),
        "market_cap": row.get("market_cap"),
        "ev_to_ebitda": first_value("ev_to_ebitda", "ev_ebitda"),
        "net_sales": first_value("net_sales", "net_revenue", "revenue"),
        "revenue": first_value("revenue", "net_sales", "net_revenue"),
        "net_profit": first_value(
            "net_profit_loss_after_tax",
            "net_profit",
            "profit_after_tax_for_shareholders_of_parent_company",
        ),
        "owners_equity": row.get("owners_equity") or row.get("equity"),
    }


def save_rows(rows: list[dict[str, Any]], output_path: Path) -> None:
    """Luu du lieu ra CSV, JSONL hoac Parquet."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix.lower()

    if suffix == ".jsonl":
        with output_path.open("w", encoding="utf-8") as file:
            for row in rows:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
        return

    if suffix == ".parquet":
        try:
            import pandas as pd
        except ImportError as exc:
            raise SystemExit("Luu Parquet can pandas va pyarrow.") from exc
        pd.DataFrame(rows).to_parquet(output_path, index=False)
        return

    fieldnames = sorted({key for row in rows for key in row})
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def save_latest_realtime_rows(
        rows: list[dict[str, Any]], output_path: Path
) -> None:
    """Gộp snapshot realtime mới theo mã thay vì ghi đè các mã đã có."""

    merged: dict[str, dict[str, Any]] = {}
    if output_path.is_file():
        try:
            with output_path.open("r", encoding="utf-8-sig") as file:
                for line in file:
                    if not line.strip():
                        continue
                    existing = json.loads(line)
                    symbol = str(existing.get("symbol", "")).strip().upper()
                    if symbol:
                        merged[symbol] = existing
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Khong doc duoc realtime cache cu %s: %s", output_path, exc)

    for row in rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol:
            merged[symbol] = row
    if merged:
        save_rows([merged[symbol] for symbol in sorted(merged)], output_path)


def crawl_history(
        client: DNSEClient,
        symbols: Iterable[str],
        start: str,
        end: str,
        interval: str,
        output_dir: Path,
) -> None:
    """Crawl lich su va luu moi ma vao mot file CSV."""
    for symbol in symbols:
        LOGGER.info("Dang crawl lich su %s", symbol)
        try:
            rows = client.fetch_history(symbol, start, end, interval)
        except (PermissionError, RuntimeError, ValueError) as exc:
            LOGGER.error("Bo qua lich su %s vi loi: %s", symbol, exc)
            continue
        if rows:
            save_rows(rows, output_dir / "history" / f"{symbol}_{interval}.csv")
        else:
            LOGGER.warning("%s khong co du lieu lich su", symbol)


def crawl_market_indices(
        client: DNSEClient,
        symbols: Iterable[str],
        start: str,
        end: str,
        interval: str,
        output_dir: Path,
) -> None:
    """Crawl lich su VNINDEX/VN30 bang type=INDEX cua DNSE."""

    for symbol in symbols:
        normalized = symbol.strip().upper()
        if not normalized:
            continue
        LOGGER.info("Dang crawl chi so thi truong %s", normalized)
        try:
            rows = client.fetch_history(
                normalized,
                start,
                end,
                interval,
                asset_type="INDEX",
            )
        except (PermissionError, RuntimeError, ValueError) as exc:
            LOGGER.error("Bo qua chi so %s vi loi: %s", normalized, exc)
            continue
        if rows:
            save_rows(
                rows,
                output_dir / "market" / "history" / f"{normalized}_{interval}.csv",
            )
        else:
            LOGGER.warning("%s khong co du lieu lich su", normalized)


def crawl_realtime_once(
        client: DNSEClient,
        symbols: Iterable[str],
        output_dir: Path,
) -> Path | None:
    """Lay mot snapshot gia khop gan nhat va luu JSONL."""
    rows = client.fetch_realtime(symbols)
    if not rows:
        LOGGER.warning("Khong co du lieu moi nhat")
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / "realtime" / f"trades_{timestamp}.jsonl"
    save_rows(rows, output_path)
    return output_path


def crawl_foreign_flow_once(
        symbols: Iterable[str],
        output_dir: Path,
        source: str,
        batch_size: int,
        use_system_proxy: bool,
) -> Path | None:
    """Lay snapshot mua/ban khoi ngoai va luu CSV de tich luy theo thoi gian."""

    client = ForeignFlowClient(source=source, use_system_proxy=use_system_proxy)
    try:
        rows = client.fetch(symbols, batch_size=batch_size)
    except Exception as exc:
        LOGGER.error("Khong lay duoc du lieu khoi ngoai tu %s: %s", source, exc)
        return None
    if not rows:
        LOGGER.warning("Nguon %s khong tra du lieu khoi ngoai hop le", source)
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / "foreign" / f"foreign_flow_{timestamp}.csv"
    save_rows(rows, output_path)
    return output_path


def crawl_dnse_foreign_sessions(
        client: DNSEClient,
        symbols: Iterable[str],
        output_dir: Path,
        end: str,
        sessions: int,
        lookback_days: int,
        board_id: str,
) -> Path | None:
    """Lay va cap nhat lich su khoi ngoai theo phien tu DNSE."""

    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        normalized = symbol.strip().upper()
        if not normalized:
            continue
        LOGGER.info("Dang crawl %s phien khoi ngoai cua %s", sessions, normalized)
        try:
            symbol_rows = client.fetch_foreign_sessions(
                normalized,
                end=end,
                sessions=sessions,
                lookback_days=lookback_days,
                board_id=board_id,
            )
        except (PermissionError, RuntimeError, ValueError) as exc:
            LOGGER.error("Bo qua khoi ngoai %s vi loi: %s", normalized, exc)
            continue
        if len(symbol_rows) < sessions:
            LOGGER.warning(
                "%s chi co %s/%s phien khoi ngoai trong %s ngay gan nhat",
                normalized,
                len(symbol_rows),
                sessions,
                lookback_days,
            )
        rows.extend(symbol_rows)

    if not rows:
        LOGGER.warning("DNSE khong tra du lieu khoi ngoai hop le")
        return None

    output_path = output_dir / "foreign" / "foreign_flow_history.csv"
    existing: list[dict[str, Any]] = []
    if output_path.exists():
        try:
            with output_path.open("r", newline="", encoding="utf-8-sig") as file:
                existing = list(csv.DictReader(file))
        except (OSError, csv.Error) as exc:
            LOGGER.warning("Khong doc duoc file khoi ngoai cu %s: %s", output_path, exc)

    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in [*existing, *rows]:
        key = (
            str(row.get("symbol", "")).upper(),
            str(row.get("trading_date", "")),
            str(row.get("source", "")).upper(),
        )
        if all(key):
            merged[key] = row
    save_rows(
        sorted(
            merged.values(),
            key=lambda row: (
                str(row.get("symbol", "")),
                str(row.get("trading_date", "")),
            ),
        ),
        output_path,
    )
    return output_path


def crawl_fundamentals(
        symbols: Iterable[str],
        output_dir: Path,
        periods: Iterable[str],
        reports: Iterable[str],
        source: str,
        fallback_source: str | None,
        period_limit: int,
        delay_seconds: float,
        use_system_proxy: bool,
) -> None:
    """Crawl fundamental va tao bang screening moi nhat theo quy/nam."""
    fundamental_client = FundamentalClient(
        source=source,
        period_limit=period_limit,
        use_system_proxy=use_system_proxy,
    )
    fallback_client = (
        FundamentalClient(
            source=fallback_source,
            period_limit=period_limit,
            use_system_proxy=use_system_proxy,
        )
        if fallback_source and fallback_source.upper() != source.upper()
        else None
    )
    screening_rows: dict[str, list[dict[str, Any]]] = {
        period: [] for period in periods
    }

    for symbol in symbols:
        for period in periods:
            LOGGER.info("Dang crawl fundamental %s (%s)", symbol, period)
            try:
                report_rows = fundamental_client.fetch_symbol(symbol, period, reports)
                merged_rows = _merge_fundamental_reports(report_rows)
            except Exception as exc:
                LOGGER.warning(
                    "Nguon %s loi cho %s (%s): %s",
                    source,
                    symbol,
                    period,
                    exc,
                )
                report_rows = {}
                merged_rows = []

            if not merged_rows and fallback_client is not None:
                LOGGER.info(
                    "Thu nguon du phong %s cho %s (%s)",
                    fallback_source,
                    symbol,
                    period,
                )
                try:
                    report_rows = fallback_client.fetch_symbol(symbol, period, reports)
                    merged_rows = _merge_fundamental_reports(report_rows)
                except Exception as exc:
                    LOGGER.error(
                        "Nguon du phong %s cung loi cho %s (%s): %s",
                        fallback_source,
                        symbol,
                        period,
                        exc,
                    )
                    continue

            if not merged_rows:
                LOGGER.error("Khong co fundamental cho %s (%s)", symbol, period)
                continue

            collected_at = datetime.now(timezone.utc).isoformat()
            merged_rows = [
                _canonical_financial_row(row, collected_at) for row in merged_rows
            ]
            period_dir = output_dir / "fundamental" / period
            for report, rows in report_rows.items():
                if rows:
                    canonical_rows = [
                        _canonical_financial_row(row, collected_at) for row in rows
                    ]
                    save_rows(canonical_rows, period_dir / f"{symbol}_{report}.csv")
            if merged_rows:
                save_rows(merged_rows, period_dir / f"{symbol}_fundamentals.csv")
                screening_rows[period].append(_screening_row(merged_rows[0]))

            if delay_seconds > 0:
                time.sleep(delay_seconds)

    for period, rows in screening_rows.items():
        if rows:
            save_rows(rows, output_dir / "fundamental" / f"screening_{period}.csv")


def iter_realtime(
        client: DNSEClient,
        symbols: Iterable[str],
        output_dir: Path,
        poll_seconds: int,
) -> None:
    """Lap vo han de lay snapshot theo chu ky."""
    while True:
        output_path = crawl_realtime_once(client, symbols, output_dir)
        if output_path:
            LOGGER.info("Da luu snapshot: %s", output_path)
        time.sleep(poll_seconds)


def parse_symbols(
        raw_symbols: str | None,
        client: DNSEClient,
        exchange: str | None,
) -> list[str]:
    """Lay symbols tu CLI hoac tu endpoint instruments."""
    if raw_symbols:
        return [symbol.strip().upper() for symbol in raw_symbols.split(",") if symbol.strip()]
    return client.list_symbols(exchange=exchange)


def build_arg_parser() -> argparse.ArgumentParser:
    """Tao CLI cho crawler."""
    parser = argparse.ArgumentParser(description="Crawl du lieu DNSE.")
    parser.add_argument(
        "--mode",
        choices=("history", "realtime", "fundamental", "foreign", "both", "all"),
        default="all",
    )
    parser.add_argument("--symbols", help="Vi du: FPT,VCB,HPG")
    parser.add_argument("--exchange", help="HOSE, HNX hoac UPCOM")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--interval", default="1D", help="1, 5, 15, 1H, 1D, 1W")
    parser.add_argument(
        "--market-indices",
        default="VNINDEX,VN30",
        help="Chi so crawl cung lich su co phieu; de rong neu muon tat.",
    )
    parser.add_argument(
        "--output-dir",
        type=_resolve_project_path,
        default=DEFAULT_DATA_DIR,
        help="Thu muc du lieu; mac dinh la <thu muc dnse>/data.",
    )
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--realtime-once", action="store_true")
    parser.add_argument(
        "--realtime-loop",
        action="store_true",
        help="Polling realtime lien tuc; mode all mac dinh chi lay mot snapshot.",
    )
    parser.add_argument(
        "--fundamental-period",
        choices=("quarter", "year", "both"),
        default="both",
    )
    parser.add_argument(
        "--fundamental-source",
        choices=("VCI", "KBS"),
        default="VCI",
    )
    parser.add_argument(
        "--fundamental-fallback-source",
        choices=("NONE", "VCI", "KBS"),
        default="KBS",
        help="Nguon du phong khi provider chinh loi hoac khong co du lieu.",
    )
    parser.add_argument(
        "--fundamental-full",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Lay ca ratio, KQKD, CDKT va LCTT; dung --no-fundamental-full de rut gon.",
    )
    parser.add_argument(
        "--fundamental-period-limit",
        type=int,
        default=20,
        help="So quy/nam toi da moi bao cao.",
    )
    parser.add_argument(
        "--fundamental-delay",
        type=float,
        default=1.1,
        help="So giay nghi giua moi ma/ky de tranh vuot rate limit.",
    )
    parser.add_argument(
        "--foreign-source",
        choices=("DNSE", "KBS"),
        default="DNSE",
        help="DNSE co lich su theo phien; KBS chi la snapshot hien tai.",
    )
    parser.add_argument(
        "--foreign-batch-size",
        type=int,
        default=50,
        help="So ma moi request KBS; khong dung cho DNSE.",
    )
    parser.add_argument(
        "--foreign-sessions",
        type=int,
        default=5,
        help="So phien khoi ngoai gan nhat can lay tu DNSE.",
    )
    parser.add_argument(
        "--foreign-lookback-days",
        type=int,
        default=15,
        help="So ngay lich toi da de tim du phien giao dich khoi ngoai.",
    )
    parser.add_argument(
        "--foreign-board-id",
        default="G1",
        help="Board ID cua DNSE cho co phieu co so.",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        help="Gioi han so ma de chay thu, vi du --max-symbols 10.",
    )
    return parser


def main() -> None:
    """Entry point chay tu command line."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    args = build_arg_parser().parse_args()
    client = DNSEClient(DNSEConfig())

    try:
        symbols = parse_symbols(args.symbols, client, args.exchange)
        if args.max_symbols is not None:
            symbols = symbols[: args.max_symbols]
        output_dir = args.output_dir
        LOGGER.info("So ma can crawl: %s", len(symbols))

        if args.mode in {"fundamental", "all"}:
            if not args.symbols and args.max_symbols is None:
                LOGGER.warning(
                    "Dang crawl fundamental cho toan bo %s ma; tac vu co the mat nhieu gio. "
                    "Dung --max-symbols 10 de chay thu.",
                    len(symbols),
                )
            periods = (
                ("quarter", "year")
                if args.fundamental_period == "both"
                else (args.fundamental_period,)
            )
            reports = (
                ("ratio", "income", "balance", "cashflow")
                if args.fundamental_full
                else ("ratio", "income")
            )
            crawl_fundamentals(
                symbols=symbols,
                output_dir=output_dir,
                periods=periods,
                reports=reports,
                source=args.fundamental_source,
                fallback_source=(
                    None
                    if args.fundamental_fallback_source == "NONE"
                    else args.fundamental_fallback_source
                ),
                period_limit=args.fundamental_period_limit,
                delay_seconds=args.fundamental_delay,
                use_system_proxy=client.config.use_system_proxy,
            )

        if args.mode in {"history", "both", "all"}:
            crawl_history(
                client,
                symbols,
                args.start,
                args.end,
                args.interval,
                output_dir,
            )
            market_indices = [
                symbol.strip().upper()
                for symbol in args.market_indices.split(",")
                if symbol.strip()
            ]
            if market_indices:
                crawl_market_indices(
                    client,
                    market_indices,
                    args.start,
                    args.end,
                    args.interval,
                    output_dir,
                )

        if args.mode in {"foreign", "all"}:
            if args.foreign_source == "DNSE":
                foreign_path = crawl_dnse_foreign_sessions(
                    client=client,
                    symbols=symbols,
                    output_dir=output_dir,
                    end=args.end,
                    sessions=args.foreign_sessions,
                    lookback_days=args.foreign_lookback_days,
                    board_id=args.foreign_board_id,
                )
            else:
                foreign_path = crawl_foreign_flow_once(
                    symbols=symbols,
                    output_dir=output_dir,
                    source=args.foreign_source,
                    batch_size=args.foreign_batch_size,
                    use_system_proxy=client.config.use_system_proxy,
                )
            if foreign_path:
                LOGGER.info("Da luu du lieu khoi ngoai: %s", foreign_path)

        if args.mode in {"realtime", "both", "all"}:
            realtime_once = args.realtime_once or (
                    args.mode == "all" and not args.realtime_loop
            )
            if realtime_once:
                output_path = crawl_realtime_once(client, symbols, output_dir)
                if output_path:
                    LOGGER.info("Da luu snapshot: %s", output_path)
            else:
                iter_realtime(client, symbols, output_dir, args.poll_seconds)
    except KeyboardInterrupt:
        LOGGER.info("Da dung crawler.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
