"""Thu thap du lieu lich su va snapshot moi nhat tu DNSE OpenAPI.

Thong tin xac thuc duoc doc tu bien moi truong hoac file `.env`:

    DNSE_API_KEY=...
    DNSE_API_SECRET=...

SDK `dnse` chiu trach nhiem ky HMAC cho moi HTTP request.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
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
    ) -> list[dict[str, Any]]:
        """Lay OHLCV lich su cho mot ma co phieu."""
        params = {
            "type": "STOCK",
            "symbol": symbol.upper(),
            "resolution": _normalize_resolution(interval),
            "from": _date_to_unix(start, end_of_day=False),
            "to": _date_to_unix(end, end_of_day=True),
        }
        payload = self.request("GET", self.config.history_endpoint, params=params)
        return [_with_symbol(row, symbol) for row in _extract_ohlc_rows(payload)]

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
            return mapping_method(
                report_df=raw_frame,
                lang="en",
                style="readable",
                get_all=True,
                show_log=False,
                period_type=period,
                report_type=method_name,
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
        row: dict[str, Any], collected_at: str, source: str
) -> dict[str, Any] | None:
    """Chuan hoa khoi ngoai va tinh mua rong/ban rong tu khoi luong that."""

    symbol = str(_first_present(row, "symbol", "code", "ticker") or "").strip().upper()
    buy_volume = _to_float(
        _first_present(row, "foreign_buy_volume", "foreignBuyVolume", "FB")
    )
    sell_volume = _to_float(
        _first_present(row, "foreign_sell_volume", "foreignSellVolume", "FS")
    )
    if (
            not symbol
            or buy_volume is None
            or sell_volume is None
            or buy_volume < 0
            or sell_volume < 0
    ):
        return None
    net_volume = buy_volume - sell_volume
    return {
        "symbol": symbol,
        "timestamp": _first_present(row, "timestamp", "time", "t"),
        "foreign_buy_volume": buy_volume,
        "foreign_sell_volume": sell_volume,
        "foreign_net_volume": net_volume,
        "foreign_net_buy_volume": max(net_volume, 0.0),
        "foreign_net_sell_volume": max(-net_volume, 0.0),
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
    period_columns = [column for column in frame.columns if column not in metadata_columns]
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
    """Them schema tai chinh toi thieu; khong suy dien ngay cong bo neu nguon khong co."""

    output = dict(row)
    output.update(
        {
            "symbol": row.get("symbol"),
            "report_period": _first_present(row, "report_period", "period"),
            "published_date": _first_present(
                row,
                "published_date",
                "publish_date",
                "public_date",
                "report_date",
                "release_date",
                "ReportDate",
            ),
            "revenue": _first_present(row, "revenue", "net_sales", "net_revenue"),
            "net_profit": _first_present(
                row,
                "net_profit",
                "net_profit_loss_after_tax",
                "profit_after_tax_for_shareholders_of_parent_company",
            ),
            "roe": row.get("roe"),
            "debt_to_equity": _first_present(row, "debt_to_equity", "debtPerEquity"),
            "collected_at": collected_at,
        }
    )
    return output


def _screening_row(row: dict[str, Any]) -> dict[str, Any]:
    """Chon cac chi tieu cot loi cho bo loc co ban."""

    def first_value(*names: str) -> Any:
        for name in names:
            value = row.get(name)
            if value is not None and value != "":
                return value
        return None

    def normalize_percent(value: Any) -> Any:
        if value is None or str(row.get("source", "")).upper() != "KBS":
            return value
        try:
            return float(value) / 100
        except (TypeError, ValueError):
            return value

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
        "roe": normalize_percent(row.get("roe")),
        "roa": normalize_percent(row.get("roa")),
        "roic": normalize_percent(first_value("roic", "return_on_capital_employed_roce")),
        "debt_to_equity": normalize_percent(debt_to_equity),
        "current_ratio": first_value("current_ratio", "short_term_ratio"),
        "quick_ratio": row.get("quick_ratio"),
        "gross_margin": normalize_percent(row.get("gross_margin")),
        "net_margin": normalize_percent(row.get("net_margin")),
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
        choices=("KBS",),
        default="KBS",
        help="Nguon bang gia co khoi luong mua/ban khoi ngoai.",
    )
    parser.add_argument(
        "--foreign-batch-size",
        type=int,
        default=50,
        help="So ma moi request bang gia khoi ngoai.",
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

        if args.mode in {"foreign", "all"}:
            foreign_path = crawl_foreign_flow_once(
                symbols=symbols,
                output_dir=output_dir,
                source=args.foreign_source,
                batch_size=args.foreign_batch_size,
                use_system_proxy=client.config.use_system_proxy,
            )
            if foreign_path:
                LOGGER.info("Da luu snapshot khoi ngoai: %s", foreign_path)

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
