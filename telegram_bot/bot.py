"""Điểm khởi chạy Telegram Bot ở chế độ polling."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from telegram.request import HTTPXRequest

# Khi bấm Run trực tiếp file này trong PyCharm, Python không tự nhận package cha.
# Nhánh này bổ sung project root để hỗ trợ cả chạy file và chạy bằng ``-m``.
if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parent.parent
    # Cho phép chạy trực tiếp bot.py: các package signals/storage/notifications
    # nằm trong project root, còn telegram_bot cần thư mục cha của project.
    sys.path.insert(0, str(project_root))
    sys.path.insert(0, str(project_root.parent))
    from telegram_bot.analytics_repository import AnalyticsRepository
    from telegram_bot.config import BotConfig
    from telegram_bot.data_refresh import RealtimeRefreshService
    from telegram_bot.formatter import format_proactive_signal
    from telegram_bot.handlers import BotHandlers
    from telegram_bot.signal_delivery import ProactiveSignalService, TelegramAsyncSender
    from telegram_bot.subscriber_repository import SubscriberRepository
else:
    from .analytics_repository import AnalyticsRepository
    from .config import BotConfig
    from .data_refresh import RealtimeRefreshService
    from .formatter import format_proactive_signal
    from .handlers import BotHandlers
    from .signal_delivery import ProactiveSignalService, TelegramAsyncSender
    from .subscriber_repository import SubscriberRepository

try:
    from notifications.service import OutboxNotificationService
    from signals.risk_manager import create_risk_gate, RiskConfig
    from storage.repositories import NotificationOutboxRepository, PositionRepository
except ImportError:
    from dnse.notifications.service import OutboxNotificationService
    from dnse.signals.risk_manager import create_risk_gate, RiskConfig
    from dnse.storage.repositories import NotificationOutboxRepository, PositionRepository

LOGGER = logging.getLogger(__name__)


def _ensure_market_indices(config: BotConfig) -> None:
    """Tự động tải dữ liệu VNINDEX & VN30 nếu chưa có trong data/market/history/."""
    vnindex_file = config.data_dir / "market" / "history" / "VNINDEX_1D.csv"
    vn30_file = config.data_dir / "market" / "history" / "VN30_1D.csv"
    if not vnindex_file.is_file() or not vn30_file.is_file():
        LOGGER.info("Chưa có lịch sử VNINDEX/VN30. Đang tự động nạp từ DNSE...")
        try:
            from collectors_processing.dnse_api_crawl import (
                DNSEClient,
                DNSEConfig,
                crawl_market_indices,
            )
        except ImportError:
            from dnse.collectors_processing.dnse_api_crawl import (
                DNSEClient,
                DNSEConfig,
                crawl_market_indices,
            )
        client = DNSEClient(DNSEConfig())
        try:
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo
            vn_tz = ZoneInfo("Asia/Ho_Chi_Minh")
            end_date = datetime.now(vn_tz).strftime("%Y-%m-%d")
            start_date = (datetime.now(vn_tz) - timedelta(days=365)).strftime("%Y-%m-%d")
            crawl_market_indices(client, ["VNINDEX", "VN30"], start_date, end_date, "1D", config.data_dir)
            LOGGER.info("Đã nạp xong lịch sử VNINDEX & VN30.")
        except Exception as exc:
            LOGGER.error("Không thể nạp lịch sử VNINDEX/VN30: %s", exc)
        finally:
            client.close()


async def _post_init(application: Application) -> None:
    """Đăng ký command và khởi động tác vụ dữ liệu nền."""

    await application.bot.set_my_commands(
        [
            BotCommand("start", "Bắt đầu sử dụng bot & quy trình giao dịch"),
            BotCommand("help", "Cẩm nang hướng dẫn sử dụng (Quy trình 3 bước)"),
            BotCommand("check", "Cập nhật realtime từ DNSE & chỉ báo kỹ thuật"),
            BotCommand("signal", "Bộ lọc tín hiệu Mua/Bán & SL/TP"),
            BotCommand("block", "Bắt đỉnh/đáy (Stochastic & Order Block)"),
            BotCommand("chart", "Biểu đồ kỹ thuật Bollinger Bands"),
            BotCommand("setup", "Cài đặt khẩu vị đầu tư (Ngắn hạn / Dài hạn)"),
            BotCommand("settings", "Cài đặt quản trị rủi ro SL/TP"),
            BotCommand("watchlist", "Xem danh sách đang theo dõi"),
            BotCommand("watch", "Thêm mã vào watchlist (bật cảnh báo)"),
            BotCommand("unwatch", "Xóa mã khỏi watchlist"),
            BotCommand("market", "Xem dữ liệu VNINDEX, VN30"),
            BotCommand("status", "Xem trạng thái hệ thống và độ trễ dữ liệu"),
        ]
    )
    # Tự động bảo đảm lịch sử VNINDEX & VN30 luôn sẵn sàng
    config = application.bot_data.get("bot_config")
    if isinstance(config, BotConfig):
        import asyncio
        await asyncio.to_thread(_ensure_market_indices, config)

    refresher = application.bot_data.get("realtime_refresher")
    if isinstance(refresher, RealtimeRefreshService):
        await refresher.start()


async def _post_shutdown(application: Application) -> None:
    """Dừng gọn tác vụ dữ liệu nền trước khi đóng event loop."""

    refresher = application.bot_data.get("realtime_refresher")
    if isinstance(refresher, RealtimeRefreshService):
        await refresher.stop()


def create_application(config: BotConfig) -> Application:
    """Tạo Application và đăng ký toàn bộ command handler."""

    subscribers = SubscriberRepository(
        config.bot_database, watchlist_limit=config.watchlist_limit
    )
    positions = PositionRepository(
        config.bot_database, initial_nav=config.portfolio_initial_nav
    )
    analytics = AnalyticsRepository(
        config.analytics_database,
        config.quality_report,
        position_repository=positions,
    )
    risk_config = RiskConfig(
        max_daily_loss_pct=config.risk_max_daily_loss_pct,
        max_drawdown_pct=config.risk_max_drawdown_pct,
        min_average_trading_value_vnd=(
            config.risk_min_average_trading_value_vnd
        ),
        max_order_average_volume_pct=(
            config.risk_max_order_average_volume_pct
        ),
        default_board_lot_size=config.board_lot_size,
    )
    risk_gate = create_risk_gate(
        db_path=config.bot_database,
        config=risk_config,
        initial_nav=config.portfolio_initial_nav,
    )
    handlers = BotHandlers(config, subscribers, analytics, risk_gate, positions)

    # Mặc định bỏ qua HTTP_PROXY/HTTPS_PROXY của hệ điều hành vì proxy cũ hoặc
    # proxy không hoạt động là nguyên nhân phổ biến khiến Telegram không kết nối.
    trust_environment = config.use_system_proxy and config.proxy_url is None
    api_request = HTTPXRequest(
        connect_timeout=10,
        read_timeout=20,
        write_timeout=10,
        proxy=config.proxy_url,
        httpx_kwargs={"trust_env": trust_environment},
    )
    polling_request = HTTPXRequest(
        connection_pool_size=1,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=10,
        proxy=config.proxy_url,
        httpx_kwargs={"trust_env": trust_environment},
    )
    application = (
        Application.builder()
        .token(config.token)
        .request(api_request)
        .get_updates_request(polling_request)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    outbox_notifications = OutboxNotificationService(
        repository=NotificationOutboxRepository(config.bot_database),
        sender=TelegramAsyncSender(application.bot),
        renderer=format_proactive_signal,
    )
    proactive_signals = ProactiveSignalService(
        analytics=analytics,
        risk_gate=risk_gate,
        positions=positions,
        subscribers=subscribers,
        notifications=outbox_notifications,
        board_lot_size=config.board_lot_size,
        outbox_batch_size=config.outbox_batch_size,
    )
    application.bot_data["proactive_signal_service"] = proactive_signals
    application.bot_data["bot_config"] = config
    if config.auto_refresh:
        application.bot_data["realtime_refresher"] = RealtimeRefreshService(
            config, subscribers, proactive_signals
        )
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help))
    application.add_handler(CommandHandler("status", handlers.status))
    application.add_handler(CommandHandler("market", handlers.market))
    application.add_handler(CommandHandler("signal", handlers.signal))
    application.add_handler(CommandHandler("check", handlers.check))
    application.add_handler(CommandHandler("block", handlers.block))
    application.add_handler(CommandHandler("chart", handlers.chart))
    application.add_handler(CommandHandler("watchlist", handlers.watchlist))
    application.add_handler(CommandHandler("watch", handlers.watch))
    application.add_handler(CommandHandler("unwatch", handlers.unwatch))
    application.add_handler(CommandHandler("settings", handlers.settings))
    application.add_handler(
        CallbackQueryHandler(handlers.settings_callback, pattern=r"^set_mode:")
    )
    application.add_handler(CommandHandler("setup", handlers.setup))
    application.add_handler(
        CallbackQueryHandler(handlers.setup_callback, pattern=r"^set_inv:")
    )
    application.add_error_handler(handlers.error)
    return application


def main() -> None:
    """Nạp cấu hình và chạy polling hoặc webhook tới khi dừng."""

    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = BotConfig.from_env()
    if not config.allowlist_enabled:
        LOGGER.warning(
            "TELEGRAM_ALLOWED_CHAT_IDS đang trống; mọi chat đều có thể dùng bot."
        )
    LOGGER.info("Khởi động Telegram Bot ở chế độ %s.", config.mode)
    application = create_application(config)
    if config.mode == "webhook":
        application.run_webhook(
            listen=config.webhook_listen,
            port=config.webhook_port,
            url_path=config.webhook_path,
            webhook_url=config.webhook_url,
            secret_token=config.webhook_secret_token,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        application.run_polling(
            poll_interval=config.poll_interval,
            timeout=20,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )


if __name__ == "__main__":
    main()
