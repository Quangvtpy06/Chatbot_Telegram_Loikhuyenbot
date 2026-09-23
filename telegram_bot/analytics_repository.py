"""Đọc kết quả analytics đã lưu, không chạy phân tích trong Telegram handler."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

try:
    from signals.signal_engine import SignalEngine, create_signal_engine
    from signals.models import SignalEvent
    from storage.repositories import PositionRepository
except ImportError:  # Hỗ trợ chạy ``python -m dnse.telegram_bot`` từ thư mục cha.
    from dnse.signals.signal_engine import SignalEngine, create_signal_engine
    from dnse.signals.models import SignalEvent
    from dnse.storage.repositories import PositionRepository

from .models import SignalView, SystemStatus
from .config import INVALID_EQUITY_SYMBOLS

LOGGER = logging.getLogger(__name__)


class AnalyticsRepository:
    """Nguồn đọc chỉ dùng cho bot, giữ nguyên cổng an toàn dữ liệu."""

    def __init__(
            self,
            database_path: Path,
            quality_report_path: Path,
            signal_engine: SignalEngine | None = None,
            position_repository: PositionRepository | None = None,
    ) -> None:
        self.database_path = database_path
        self.quality_report_path = quality_report_path
        self.signal_engine = signal_engine or create_signal_engine()
        self.position_repository = position_repository

    def _connect_readonly(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise FileNotFoundError(self.database_path)
        connection = sqlite3.connect(
            f"file:{self.database_path.as_posix()}?mode=ro", uri=True, timeout=5
        )
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _readonly_connection(self) -> Iterator[sqlite3.Connection]:
        """Mở kết nối chỉ đọc và luôn giải phóng file database."""

        connection = self._connect_readonly()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _clean_value(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return value

    def get_system_status(self) -> SystemStatus:
        report: dict[str, Any] = {}
        issues: list[str] = []
        if self.quality_report_path.is_file():
            try:
                report = json.loads(
                    self.quality_report_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                issues.append(f"Không đọc được quality report: {type(exc).__name__}")
        else:
            issues.append("Chưa có quality_report.json")

        snapshot_symbols = 0
        latest_realtime_at: str | None = None
        latest_history_date: str | None = None
        latest_fundamental_period: str | None = None
        latest_fundamental_source: str | None = None
        if self.database_path.is_file():
            try:
                with self._readonly_connection() as connection:
                    if self._table_exists(connection, "stock_snapshot"):
                        snapshot_symbols = int(
                            connection.execute(
                                "SELECT COUNT(*) FROM stock_snapshot"
                            ).fetchone()[0]
                        )
                    if self._table_exists(connection, "realtime_quotes"):
                        latest_realtime_at = connection.execute(
                            "SELECT MAX(timestamp_local) FROM realtime_quotes"
                        ).fetchone()[0]
                    if self._table_exists(connection, "price_history"):
                        latest_history_date = connection.execute(
                            "SELECT MAX(date) FROM price_history"
                        ).fetchone()[0]
                    if self._table_exists(connection, "fundamentals"):
                        fundamental = connection.execute(
                            """
                            SELECT period, source
                            FROM fundamentals
                            WHERE period_type = 'quarter'
                            ORDER BY period DESC LIMIT 1
                            """
                        ).fetchone()
                        if fundamental is not None:
                            latest_fundamental_period = fundamental["period"]
                            latest_fundamental_source = fundamental["source"]
            except sqlite3.Error as exc:
                issues.append(f"SQLite analytics lỗi: {type(exc).__name__}")
        else:
            issues.append("Chưa có market_analytics.sqlite")

        gate = report.get("signal_gate", {})
        report_issues = report.get("issues", [])
        if isinstance(report_issues, list):
            for issue in report_issues[:5]:
                if isinstance(issue, Mapping):
                    issues.append(str(issue.get("message") or issue))
                else:
                    issues.append(str(issue))

        return SystemStatus(
            report_status=str(report.get("status", "MISSING")).upper(),
            generated_at=report.get("generated_at_utc"),
            database_available=self.database_path.is_file(),
            report_available=self.quality_report_path.is_file(),
            eligible_symbols=int(gate.get("eligible_symbols", 0) or 0),
            blocked_symbols=int(gate.get("blocked_symbols", 0) or 0),
            snapshot_symbols=snapshot_symbols,
            latest_realtime_at=latest_realtime_at,
            latest_history_date=latest_history_date,
            latest_fundamental_period=latest_fundamental_period,
            latest_fundamental_source=latest_fundamental_source,
            issues=tuple(issues),
        )

    def get_signal_view(
            self, symbol: str, user_settings: dict[str, Any] | None = None
    ) -> tuple[SignalView, SignalEvent | None]:
        """Sinh tín hiệu từ snapshot, market context và năm phiên khối ngoại mới nhất."""

        if not self.database_path.is_file():
            return SignalView(
                symbol=symbol,
                action="NO SIGNAL",
                data_status="DATA WARNING",
                signal_status="NO SIGNAL",
                reasons=("Chưa có cơ sở dữ liệu analytics.",),
            ), None

        try:
            with self._readonly_connection() as connection:
                row = connection.execute(
                    "SELECT * FROM stock_snapshot WHERE UPPER(symbol) = ? LIMIT 1",
                    (symbol,),
                ).fetchone()
                if row is not None:
                    values = {
                        key: self._clean_value(row[key]) for key in row.keys()
                    }
                    if user_settings:
                        for k in ("sl_tp_mode", "fixed_sl_pct", "fixed_tp_pct", "investment_mode"):
                            if k in user_settings:
                                values[k] = user_settings[k]
                    market = self._market_context(
                        connection, values.get("market_context_symbol")
                    )
                    foreign_sessions = self._foreign_sessions(connection, symbol)
                else:
                    values = {}
                    market = None
                    foreign_sessions = ()
        except sqlite3.Error as exc:
            return SignalView(
                symbol=symbol,
                action="NO SIGNAL",
                data_status="DATA WARNING",
                signal_status="NO SIGNAL",
                reasons=(f"Không đọc được analytics: {type(exc).__name__}.",),
            ), None

        if row is None:
            return SignalView(
                symbol=symbol,
                action="NO SIGNAL",
                data_status="DATA WARNING",
                signal_status="NO SIGNAL",
                reasons=("Không tìm thấy mã trong stock_snapshot.",),
            ), None

        try:
            position = None
            event = self.signal_engine.generate(
                values,
                market=market,
                foreign_sessions=foreign_sessions,
                position=position,
                sector=str(values.get("sector") or values.get("industry") or "")
                       or None,
            )
        except Exception as exc:  # Bot phải fail-safe: lỗi engine không được phát tín hiệu.
            LOGGER.exception("Signal Engine lỗi khi xử lý %s", symbol)
            return SignalView(
                symbol=symbol,
                action="NO SIGNAL",
                data_status="DATA WARNING",
                signal_status="NO SIGNAL",
                reasons=(f"Signal Engine lỗi: {type(exc).__name__}.",),
            ), None

        realtime_p = values.get("realtime_price")
        latest_c = values.get("latest_close")
        prev_c = values.get("previous_close")
        unit = float(values.get("price_unit_vnd") or 1000.0)

        # Xác định giá tham chiếu chuẩn (phiên liền trước):
        # Nếu nến latest_close đã là nến của ngày hôm nay (cùng ngày với realtime), giá tham chiếu là previous_close.
        # Nếu nến latest_close là nến của hôm qua (chưa cập nhật nến hôm nay), giá tham chiếu là latest_close.
        ref_price = None
        price_date = str(values.get("price_as_of") or "")[:10]
        realtime_date = str(values.get("realtime_as_of") or "")[:10]
        if price_date and realtime_date and price_date == realtime_date and prev_c is not None and prev_c > 0:
            ref_price = prev_c
        elif latest_c is not None and latest_c > 0:
            if prev_c is not None and prev_c > 0 and realtime_p is not None and math.isclose(latest_c, realtime_p, rel_tol=1e-5):
                ref_price = prev_c
            else:
                ref_price = latest_c
        elif prev_c is not None and prev_c > 0:
            ref_price = prev_c

        p_change = None
        p_change_pct = None
        if realtime_p is not None and ref_price is not None and ref_price > 0:
            p_change = (realtime_p - ref_price) * unit
            p_change_pct = (realtime_p - ref_price) / ref_price * 100.0

        metrics = {
            key: values.get(key)
            for key in (
                "rsi_14",
                "ema_20",
                "sma_20",
                "sma_50",
                "macd",
                "macd_signal",
                "trend_state",
                "bollinger_middle_20",
                "bollinger_upper_20",
                "bollinger_lower_20",
                "bollinger_bandwidth_20",
                "volume_ratio_20d",
                "ob_support",
                "ob_resistance",
                "pe_quarter",
                "pb_quarter",
                "roe_quarter",
                "roe_ttm",
                "debt_to_equity",
                "foreign_net_volume_5d",
                "total_volume",
                "latest_volume",
                "realtime_price",
                "latest_close",
                "previous_close",
                "realtime_as_of",
                "price_as_of",
            )
        }
        metrics["price_change"] = p_change
        metrics["price_change_pct"] = p_change_pct

        # Cả hai cột đã có đơn vị cổ phiếu trong pipeline; không quy đổi lần nữa.
        raw_vol = values.get("latest_volume")
        tot_v = values.get("total_volume")
        if tot_v is not None and tot_v > 0:
            if raw_vol is None or tot_v >= raw_vol:
                raw_vol = float(tot_v)
        metrics["volume"] = raw_vol

        display_price = event.reference_price or values.get("realtime_price") or values.get("latest_close")

        return SignalView(
            symbol=symbol,
            action=event.action,
            data_status=event.data_status,
            signal_status=event.signal_status,
            as_of=event.data_as_of or None,
            price=display_price,
            confidence=event.confidence,
            stop_loss=event.stop_loss,
            take_profit=event.take_profit,
            strategy=f"{event.strategy} v{event.strategy_version}",
            reasons=tuple(event.reasons),
            metrics=metrics,
            metadata=dict(event.metadata) if event else {},
        ), event

    def list_snapshot_symbols(self) -> list[str]:
        """Lấy danh sách các mã cổ phiếu đã có trong stock_snapshot."""
        if not self.database_path.is_file():
            return []
        try:
            with self._readonly_connection() as connection:
                if not self._table_exists(connection, "stock_snapshot"):
                    return []
                rows = connection.execute(
                    "SELECT DISTINCT symbol FROM stock_snapshot ORDER BY symbol"
                ).fetchall()
                return [
                    str(r[0]) for r in rows
                    if r[0] and str(r[0]).strip().upper() not in INVALID_EQUITY_SYMBOLS
                ]
        except Exception:
            return []

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                is not None
        )

    def _market_context(
            self, connection: sqlite3.Connection, preferred_symbol: Any
    ) -> dict[str, Any] | None:
        if not self._table_exists(connection, "market_context"):
            return None
        candidates: list[str] = []
        for candidate in (preferred_symbol, "VNINDEX", "VN30"):
            normalized = str(candidate or "").strip().upper()
            if normalized and normalized not in candidates:
                candidates.append(normalized)
        for candidate in candidates:
            row = connection.execute(
                "SELECT * FROM market_context WHERE UPPER(symbol) = ? LIMIT 1",
                (candidate,),
            ).fetchone()
            if row is not None:
                return {
                    key: self._clean_value(row[key]) for key in row.keys()
                }
        return None

    def _foreign_sessions(
            self, connection: sqlite3.Connection, symbol: str
    ) -> tuple[dict[str, Any], ...]:
        if not self._table_exists(connection, "foreign_flow"):
            return ()
        rows = connection.execute(
            """
            SELECT trading_date,
                   foreign_buy_volume,
                   foreign_sell_volume,
                   foreign_net_volume,
                   source,
                   collected_at
            FROM foreign_flow
            WHERE UPPER(symbol) = ?
            ORDER BY trading_date, collected_at
            """,
            (symbol,),
        ).fetchall()
        by_source: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            source = str(row["source"] or "UNKNOWN").upper()
            trading_date = str(row["trading_date"] or "")
            if not trading_date:
                continue
            by_source.setdefault(source, {})[trading_date] = {
                "as_of": trading_date,
                "foreign_buy_volume": self._clean_value(row["foreign_buy_volume"]),
                "foreign_sell_volume": self._clean_value(row["foreign_sell_volume"]),
                "foreign_net_volume": self._clean_value(row["foreign_net_volume"]),
            }
        if not by_source:
            return ()
        source_priority = {"DNSE": 3, "VCI": 2, "KBS": 1}
        preferred = max(
            by_source,
            key=lambda source: (
                len(by_source[source]),
                source_priority.get(source, 0),
                source,
            ),
        )
        sessions = sorted(
            by_source[preferred].values(), key=lambda item: str(item["as_of"])
        )
        return tuple(sessions[-5:])
