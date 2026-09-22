"""Dịch vụ nền refresh analytics và cập nhật BCTC theo chu kỳ."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

try:
    from collectors_processing.analytics import AnalyticsPipeline, PipelineConfig
    from collectors_processing.dnse_api_crawl import (
        DNSEClient,
        DNSEConfig,
        crawl_fundamentals,
        save_latest_realtime_rows,
    )
except ImportError:
    from dnse.collectors_processing.analytics import AnalyticsPipeline, PipelineConfig
    from dnse.collectors_processing.dnse_api_crawl import (
        DNSEClient,
        DNSEConfig,
        crawl_fundamentals,
        save_latest_realtime_rows,
    )

from .config import BotConfig
from .subscriber_repository import SubscriberRepository

if TYPE_CHECKING:
    from .signal_delivery import ProactiveSignalService

LOGGER = logging.getLogger(__name__)
VIETNAM_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")


class RealtimeRefreshService:
    """Refresh realtime, analytics và BCTC theo chu kỳ."""

    def __init__(
            self,
            config: BotConfig,
            subscribers: SubscriberRepository,
            signal_delivery: "ProactiveSignalService | None" = None,
    ) -> None:
        self.config = config
        self.subscribers = subscribers
        self.signal_delivery = signal_delivery
        self._task: asyncio.Task[None] | None = None
        self._last_realtime_at = 0.0
        self._last_analytics_at = 0.0
        self._fundamental_state_path = (
                self.config.data_dir / "fundamental" / "auto_refresh_state.json"
        )
        self._last_fundamental_at = self._load_fundamental_refresh_state()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name="telegram-analytics-refresh"
        )
        LOGGER.info(
            "Đã bật realtime %.0fs, analytics %.0fs và BCTC tự động mỗi %.1f giờ.",
            self.config.realtime_refresh_seconds,
            self.config.analytics_refresh_seconds,
            self.config.fundamental_refresh_hours,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        """Cập nhật BCTC đến hạn, sau đó chạy analytics và phát tín hiệu."""

        while True:
            try:
                symbols = self._symbols()
                if symbols:
                    wall_now = datetime.now(timezone.utc)
                    monotonic_now = time.monotonic()
                    market_open = self._is_market_open(wall_now)
                    if (
                            market_open
                            and (
                            self._last_realtime_at == 0
                            or monotonic_now - self._last_realtime_at
                            >= self.config.realtime_refresh_seconds
                    )
                    ):
                        await asyncio.to_thread(self._refresh_realtime, symbols)
                        self._last_realtime_at = time.monotonic()
                    if (
                            self._fundamental_due(wall_now)
                            and not market_open
                    ):
                        await asyncio.to_thread(self._refresh_fundamentals, symbols)
                        self._last_fundamental_at = datetime.now(timezone.utc)
                        self._save_fundamental_refresh_state(
                            self._last_fundamental_at
                        )

                    if (
                            self._last_analytics_at == 0
                            or monotonic_now - self._last_analytics_at
                            >= self.config.analytics_refresh_seconds
                    ):
                        await asyncio.to_thread(self._refresh_analytics, symbols)
                        self._last_analytics_at = time.monotonic()
                        await self._deliver_signals(symbols)
                else:
                    LOGGER.debug(
                        "Analytics refresh chờ có mã trong watchlist hoặc snapshot."
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Analytics refresh thất bại; sẽ thử lại.")
            await asyncio.sleep(
                min(
                    self.config.realtime_refresh_seconds,
                    self.config.analytics_refresh_seconds,
                )
            )

    def _refresh_realtime(self, symbols: tuple[str, ...]) -> None:
        """Lấy một snapshot realtime cho toàn bộ mã đang theo dõi."""

        client = DNSEClient(DNSEConfig())
        try:
            rows = client.fetch_realtime(symbols)
            if rows:
                save_latest_realtime_rows(
                    rows,
                    self.config.data_dir / "realtime" / "trades_latest.jsonl",
                )
            LOGGER.info(
                "Đã refresh realtime cho %s/%s mã.", len(rows), len(symbols)
            )
        finally:
            client.close()

    def _load_fundamental_refresh_state(self) -> datetime | None:
        if not self._fundamental_state_path.is_file():
            return None
        try:
            payload = json.loads(
                self._fundamental_state_path.read_text(encoding="utf-8")
            )
            value = datetime.fromisoformat(
                str(payload["last_fundamental_at_utc"]).replace("Z", "+00:00")
            )
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            LOGGER.warning(
                "Không đọc được state refresh BCTC tại %s; sẽ cập nhật lại.",
                self._fundamental_state_path,
            )
            return None

    def _save_fundamental_refresh_state(self, value: datetime) -> None:
        self._fundamental_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"last_fundamental_at_utc": value.astimezone(timezone.utc).isoformat()}
        temporary = Path(f"{self._fundamental_state_path}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._fundamental_state_path)

    def _fundamental_due(self, now: datetime | None = None) -> bool:
        """Kiểm tra BCTC đã đến hạn refresh theo state được lưu hay chưa."""

        if not self.config.fundamental_auto_refresh:
            return False
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if self._last_fundamental_at is None:
            return True
        last = self._last_fundamental_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc) - last.astimezone(timezone.utc) >= timedelta(
            hours=self.config.fundamental_refresh_hours
        )

    def _refresh_fundamentals(self, symbols: tuple[str, ...]) -> None:
        """Crawl BCTC quý cho watchlist/snapshot bằng KBS, dự phòng VCI."""

        crawl_fundamentals(
            symbols=symbols,
            output_dir=self.config.data_dir,
            periods=("quarter",),
            reports=("ratio", "income", "balance"),
            source="KBS",
            fallback_source="VCI",
            period_limit=max(5, self.config.fundamental_period_limit),
            delay_seconds=self.config.fundamental_delay_seconds,
            use_system_proxy=self.config.use_system_proxy,
        )
        LOGGER.info("Đã refresh BCTC quý cho %s mã.", len(symbols))

    async def _deliver_signals(self, symbols: tuple[str, ...]) -> None:
        if self.signal_delivery is None or not self.config.proactive_notifications:
            return
        result = await self.signal_delivery.process_symbols(symbols)
        LOGGER.info(
            "Tín hiệu chủ động: evaluated=%s queued=%s sent=%s skipped=%s",
            result["evaluated"], result["queued"], result["sent"], result["skipped"],
        )

    def _symbols(self) -> tuple[str, ...]:
        configured = set(self.config.realtime_symbols)
        configured.update(self.subscribers.list_all_watchlist_symbols())
        if not configured:
            configured.update(self._snapshot_symbols())
        symbols = tuple(sorted(configured))
        if len(symbols) > self.config.realtime_max_symbols:
            return symbols[: self.config.realtime_max_symbols]
        return symbols

    def _snapshot_symbols(self) -> tuple[str, ...]:
        """Dùng các mã analytics hiện có khi chưa cấu hình/watchlist."""

        if not self.config.analytics_database.is_file():
            return ()
        try:
            with sqlite3.connect(
                    f"file:{self.config.analytics_database.as_posix()}?mode=ro",
                    uri=True,
                    timeout=5,
            ) as connection:
                rows = connection.execute(
                    "SELECT UPPER(symbol) FROM stock_snapshot ORDER BY symbol"
                ).fetchall()
        except sqlite3.Error as exc:
            LOGGER.warning("Không đọc được danh sách mã từ analytics: %s", exc)
            return ()
        return tuple(str(row[0]) for row in rows if row and row[0])

    def _refresh_analytics(self, symbols: tuple[str, ...]) -> None:
        pipeline = AnalyticsPipeline(
            PipelineConfig(
                input_dir=self.config.data_dir,
                output_dir=self.config.analytics_output_dir,
                database_path=self.config.analytics_database,
                expected_symbols=symbols,
            )
        )
        report = pipeline.run()
        LOGGER.info(
            "Đã refresh analytics; trạng thái=%s.",
            report.get("status", "UNKNOWN"),
        )

    @staticmethod
    def _is_market_open(now: datetime | None = None) -> bool:
        current = now or datetime.now(VIETNAM_TIMEZONE)
        if current.tzinfo is None:
            current = current.replace(tzinfo=VIETNAM_TIMEZONE)
        local = current.astimezone(VIETNAM_TIMEZONE)
        if local.weekday() >= 5:
            return False
        clock = local.time().replace(tzinfo=None)
        morning = datetime_time(9, 0) <= clock <= datetime_time(11, 30)
        afternoon = datetime_time(13, 0) <= clock <= datetime_time(15, 0)
        return morning or afternoon
