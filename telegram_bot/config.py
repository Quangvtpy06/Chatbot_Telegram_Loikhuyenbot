"""Đọc cấu hình Telegram Bot từ biến môi trường."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
# Tên doanh nghiệp bị nhập nhầm thành mã cổ phiếu trên sàn.
INVALID_EQUITY_SYMBOLS = frozenset({"VNPT"})


def _project_path(value: str | Path) -> Path:
    """Chuẩn hóa đường dẫn tương đối theo thư mục gốc ``dnse``."""

    path = Path(value).expanduser()
    if not path.is_absolute():
        parts = path.parts
        if parts and parts[0].lower() == PROJECT_ROOT.name.lower():
            path = Path(*parts[1:])
        path = PROJECT_ROOT / path
    return path.resolve()


def _parse_chat_ids(raw_value: str) -> frozenset[int]:
    values = re.split(r"[\s,;]+", raw_value.strip()) if raw_value.strip() else []
    try:
        return frozenset(int(value) for value in values if value)
    except ValueError as exc:
        raise ValueError(
            "TELEGRAM_ALLOWED_CHAT_IDS chỉ được chứa số, phân tách bằng dấu phẩy."
        ) from exc


def _parse_bool(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} phải là true hoặc false.")


def _parse_symbols(raw_value: str) -> tuple[str, ...]:
    """Đọc danh sách mã phân tách bằng dấu phẩy và loại trùng."""

    values = re.split(r"[\s,;]+", raw_value.strip()) if raw_value.strip() else []
    symbols = [value.strip().upper() for value in values if value.strip()]
    invalid = [symbol for symbol in symbols if not re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", symbol)]
    if invalid:
        raise ValueError(f"TELEGRAM_REALTIME_SYMBOLS có mã không hợp lệ: {invalid}")
    return tuple(symbol for symbol in dict.fromkeys(symbols) if symbol not in INVALID_EQUITY_SYMBOLS)


@dataclass(frozen=True)
class BotConfig:
    """Cấu hình bot cho polling hoặc webhook."""

    token: str
    allowed_chat_ids: frozenset[int]
    analytics_database: Path
    quality_report: Path
    bot_database: Path
    mode: str = "polling"
    webhook_url: str | None = None
    webhook_listen: str = "127.0.0.1"
    webhook_port: int = 8080
    webhook_path: str = "telegram"
    webhook_secret_token: str | None = None
    use_system_proxy: bool = False
    proxy_url: str | None = None
    poll_interval: float = 1.0
    command_cooldown_seconds: float = 1.5
    watchlist_limit: int = 50
    auto_refresh: bool = True
    realtime_symbols: tuple[str, ...] = ()
    realtime_refresh_seconds: float = 15.0
    analytics_refresh_seconds: float = 60.0
    realtime_max_symbols: int = 20
    fundamental_auto_refresh: bool = True
    fundamental_refresh_hours: float = 24.0
    fundamental_period_limit: int = 5
    fundamental_delay_seconds: float = 1.1
    proactive_notifications: bool = True
    portfolio_initial_nav: float = 1_000_000_000.0
    risk_max_daily_loss_pct: float = 0.03
    risk_max_drawdown_pct: float = 0.10
    risk_min_average_trading_value_vnd: float = 5_000_000_000.0
    risk_max_order_average_volume_pct: float = 0.10
    board_lot_size: int = 100
    outbox_batch_size: int = 50
    data_dir: Path = PROJECT_ROOT / "data"
    analytics_output_dir: Path = PROJECT_ROOT / "data" / "analytics"

    @property
    def allowlist_enabled(self) -> bool:
        return bool(self.allowed_chat_ids)

    @classmethod
    def from_env(cls) -> "BotConfig":
        """Nạp `.env`, kiểm tra token và trả cấu hình đã chuẩn hóa."""

        load_dotenv(ENV_PATH)
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                f"Thiếu TELEGRAM_BOT_TOKEN. Hãy khai báo biến này trong {ENV_PATH}."
            )

        mode = os.getenv("TELEGRAM_MODE", "polling").strip().lower()
        if mode not in {"polling", "webhook"}:
            raise RuntimeError("TELEGRAM_MODE phải là polling hoặc webhook.")
        webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip() or None
        webhook_secret = (
            os.getenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", "").strip() or None
        )
        if mode == "webhook" and (webhook_url is None or webhook_secret is None):
            raise RuntimeError(
                "Webhook cần TELEGRAM_WEBHOOK_URL và TELEGRAM_WEBHOOK_SECRET_TOKEN."
            )

        return cls(
            token=token,
            allowed_chat_ids=_parse_chat_ids(
                os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
            ),
            analytics_database=_project_path(
                os.getenv(
                    "TELEGRAM_ANALYTICS_DB",
                    "data/analytics/market_analytics.sqlite",
                )
            ),
            quality_report=_project_path(
                os.getenv(
                    "TELEGRAM_QUALITY_REPORT",
                    "data/analytics/quality_report.json",
                )
            ),
            bot_database=_project_path(
                os.getenv("TELEGRAM_BOT_DB", "data/telegram_bot.sqlite")
            ),
            mode=mode,
            webhook_url=webhook_url,
            webhook_listen=os.getenv(
                "TELEGRAM_WEBHOOK_LISTEN", "127.0.0.1"
            ).strip(),
            webhook_port=max(
                1, int(os.getenv("TELEGRAM_WEBHOOK_PORT", "8080"))
            ),
            webhook_path=(
                os.getenv("TELEGRAM_WEBHOOK_PATH", "telegram").strip().strip("/")
                or "telegram"
            ),
            webhook_secret_token=webhook_secret,
            use_system_proxy=_parse_bool("TELEGRAM_USE_SYSTEM_PROXY", False),
            proxy_url=os.getenv("TELEGRAM_PROXY_URL", "").strip() or None,
            poll_interval=max(
                0.1, float(os.getenv("TELEGRAM_POLL_INTERVAL", "1.0"))
            ),
            command_cooldown_seconds=max(
                0.0,
                float(os.getenv("TELEGRAM_COMMAND_COOLDOWN_SECONDS", "1.5")),
            ),
            watchlist_limit=max(
                1, int(os.getenv("TELEGRAM_WATCHLIST_LIMIT", "50"))
            ),
            auto_refresh=_parse_bool("TELEGRAM_AUTO_REFRESH", True),
            realtime_symbols=_parse_symbols(
                os.getenv("TELEGRAM_REALTIME_SYMBOLS", "")
            ),
            realtime_refresh_seconds=max(
                5.0, float(os.getenv("TELEGRAM_REALTIME_REFRESH_SECONDS", "15"))
            ),
            analytics_refresh_seconds=max(
                15.0, float(os.getenv("TELEGRAM_ANALYTICS_REFRESH_SECONDS", "60"))
            ),
            realtime_max_symbols=max(
                1, int(os.getenv("TELEGRAM_REALTIME_MAX_SYMBOLS", "20"))
            ),
            fundamental_auto_refresh=_parse_bool(
                "TELEGRAM_FUNDAMENTAL_AUTO_REFRESH", True
            ),
            fundamental_refresh_hours=max(
                1.0, float(os.getenv("TELEGRAM_FUNDAMENTAL_REFRESH_HOURS", "24"))
            ),
            fundamental_period_limit=max(
                5, int(os.getenv("TELEGRAM_FUNDAMENTAL_PERIOD_LIMIT", "5"))
            ),
            fundamental_delay_seconds=max(
                0.0, float(os.getenv("TELEGRAM_FUNDAMENTAL_DELAY_SECONDS", "1.1"))
            ),
            proactive_notifications=_parse_bool(
                "TELEGRAM_PROACTIVE_NOTIFICATIONS", True
            ),
            portfolio_initial_nav=max(
                1.0, float(os.getenv("PORTFOLIO_INITIAL_NAV", "1000000000"))
            ),
            risk_max_daily_loss_pct=min(
                1.0,
                max(0.0001, float(os.getenv("RISK_MAX_DAILY_LOSS_PCT", "0.03"))),
            ),
            risk_max_drawdown_pct=min(
                1.0,
                max(0.0001, float(os.getenv("RISK_MAX_DRAWDOWN_PCT", "0.10"))),
            ),
            risk_min_average_trading_value_vnd=max(
                0.0,
                float(os.getenv("RISK_MIN_AVERAGE_TRADING_VALUE_VND", "5000000000")),
            ),
            risk_max_order_average_volume_pct=min(
                1.0,
                max(
                    0.0001,
                    float(os.getenv("RISK_MAX_ORDER_AVERAGE_VOLUME_PCT", "0.10")),
                ),
            ),
            board_lot_size=max(1, int(os.getenv("RISK_BOARD_LOT_SIZE", "100"))),
            outbox_batch_size=max(
                1, int(os.getenv("TELEGRAM_OUTBOX_BATCH_SIZE", "50"))
            ),
            data_dir=_project_path(os.getenv("TELEGRAM_DATA_DIR", "data")),
            analytics_output_dir=_project_path(
                os.getenv("TELEGRAM_ANALYTICS_OUTPUT_DIR", "data/analytics")
            ),
        )
