"""Các command handler của Telegram Bot."""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

try:
    from signals.risk_manager import RiskGate
    from storage.repositories import PositionRepository, RiskDecision
    from collectors_processing.dnse_api_crawl import (
        DNSEClient,
        DNSEConfig,
        crawl_fundamentals,
        crawl_history,
        crawl_market_indices,
        crawl_dnse_foreign_sessions,
        save_latest_realtime_rows,
    )
    from collectors_processing.analytics import AnalyticsPipeline, PipelineConfig
except ImportError:
    from dnse.signals.risk_manager import RiskGate
    from dnse.storage.repositories import PositionRepository, RiskDecision
    from dnse.collectors_processing.dnse_api_crawl import (
        DNSEClient,
        DNSEConfig,
        crawl_fundamentals,
        crawl_history,
        crawl_market_indices,
        crawl_dnse_foreign_sessions,
        save_latest_realtime_rows,
    )
    from dnse.collectors_processing.analytics import AnalyticsPipeline, PipelineConfig

from datetime import datetime, timedelta, timezone
import asyncio
from zoneinfo import ZoneInfo

VIETNAM_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")

from .analytics_repository import AnalyticsRepository
from .config import BotConfig, INVALID_EQUITY_SYMBOLS
from .formatter import (
    format_access_denied,
    format_block,
    format_check,
    format_help,
    format_settings,
    format_setup,
    format_signal,
    format_signal_scan,
    format_signal_single,
    format_start,
    format_system_status,
    format_watchlist,
    format_market,
    format_portfolio,
)
from .subscriber_repository import SubscriberRepository

LOGGER = logging.getLogger(__name__)
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")


def _fetch_symbol_data(symbol: str, config: BotConfig) -> None:
    """Thu thập dữ liệu on-demand cho một mã.

    - DNSE (giá realtime, lịch sử, khối ngoại): LUÔN fetch mới nhất.
    - BCTC vnstock: chỉ fetch lần đầu, lưu cache, lần sau dùng lại.
    """

    client = DNSEClient(DNSEConfig())
    try:
        # 1. Realtime snapshot — luôn lấy mới
        rows = client.fetch_realtime([symbol])
        if rows:
            save_latest_realtime_rows(
                rows, config.data_dir / "realtime" / "trades_latest.jsonl"
            )

        # 2. Lịch sử giá 1 năm — luôn lấy mới
        end_date = datetime.now(VIETNAM_TIMEZONE).strftime("%Y-%m-%d")
        start_date = (datetime.now(VIETNAM_TIMEZONE) - timedelta(days=365)).strftime("%Y-%m-%d")
        crawl_history(client, [symbol], start_date, end_date, "1D", config.data_dir)

        # 3. Khối ngoại 5 phiên gần nhất — luôn lấy mới
        crawl_dnse_foreign_sessions(
            client=client,
            symbols=[symbol],
            output_dir=config.data_dir,
            end=end_date,
            sessions=5,
            lookback_days=10,
            board_id="",
        )

        # 4. Lịch sử VNINDEX & VN30 — tự động nạp nếu thiếu để Analytics Pipeline không bị chặn
        vnindex_file = config.data_dir / "market" / "history" / "VNINDEX_1D.csv"
        vn30_file = config.data_dir / "market" / "history" / "VN30_1D.csv"
        if not vnindex_file.is_file() or not vn30_file.is_file():
            LOGGER.info("Chưa có lịch sử VNINDEX/VN30, đang tự động nạp từ DNSE...")
            crawl_market_indices(client, ["VNINDEX", "VN30"], start_date, end_date, "1D", config.data_dir)
    except Exception as exc:
        LOGGER.error("Lỗi khi thu thập dữ liệu DNSE on-demand cho %s: %s", symbol, exc)
    finally:
        client.close()

    # 4. Báo cáo tài chính (Mô hình Hybrid: vnfinancialdata cho lịch sử + vnstock cho quý gần nhất)
    try:
        from collectors_processing.hybrid_fundamental import sync_symbol_fundamentals_hybrid
    except ImportError:
        from dnse.collectors_processing.hybrid_fundamental import sync_symbol_fundamentals_hybrid

    try:
        sync_symbol_fundamentals_hybrid(
            symbol=symbol,
            output_dir=config.data_dir,
            years_limit=3,
            period_limit_quarter=max(5, min(config.fundamental_period_limit, 8)),
            delay_seconds=0.2,
            use_system_proxy=False,
        )
    except Exception as exc:
        LOGGER.error("Lỗi khi đồng bộ BCTC Hybrid cho %s: %s", symbol, exc)


async def _cleanup_symbol_data(symbol: str, config: BotConfig) -> None:
    """Xóa file ảnh biểu đồ tạm sau khi gửi xong, giữ lại dữ liệu lịch sử và BCTC."""
    await asyncio.sleep(15)
    for chart_name in (f"{symbol.upper()}_bollinger.png", f"{symbol.upper()}_block.png"):
        chart_path = config.data_dir / "charts" / chart_name
        if chart_path.is_file():
            try:
                chart_path.unlink()
            except OSError:
                pass


def _run_analytics(symbol: str, config: BotConfig) -> None:
    """Chạy analytics pipeline cho một mã."""

    pipeline = AnalyticsPipeline(
        PipelineConfig(
            input_dir=config.data_dir,
            output_dir=config.analytics_output_dir,
            database_path=config.analytics_database,
            expected_symbols=[symbol],
        )
    )
    pipeline.run()


def _fetch_index_data_from_vnstock(symbol: str) -> dict[str, Any]:
    """Lấy dữ liệu chỉ số thị trường (VNINDEX, VN30) bao gồm tổng khối lượng chuẩn từ VCI/vnstock."""
    import contextlib
    import os
    try:
        with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
            from vnstock.api.quote import Quote
            end_date = datetime.now(VIETNAM_TIMEZONE).strftime("%Y-%m-%d")
            start_date = (datetime.now(VIETNAM_TIMEZONE) - timedelta(days=7)).strftime("%Y-%m-%d")
            q = Quote(symbol=symbol, source="VCI", show_log=False)
            df = q.history(start=start_date, end=end_date)
            if df is not None and len(df) >= 2:
                last_row = df.iloc[-1]
                prev_row = df.iloc[-2]
                return {
                    "c": float(last_row["close"]),
                    "prev_c": float(prev_row["close"]),
                    "v": int(last_row["volume"]),
                    "symbol": symbol,
                }
    except Exception as exc:
        LOGGER.warning("Không thể lấy dữ liệu index %s từ vnstock: %s", symbol, exc)
    return {}


class BotHandlers:
    """Điều phối lệnh, phân quyền và giới hạn tần suất đơn giản."""

    def __init__(
            self,
            config: BotConfig,
            subscribers: SubscriberRepository,
            analytics: AnalyticsRepository,
            risk_gate: RiskGate | None = None,
            positions: PositionRepository | None = None,
    ) -> None:
        self.config = config
        self.subscribers = subscribers
        self.analytics = analytics
        self.risk_gate = risk_gate
        self.positions = positions
        self._last_commands: dict[tuple[int, str], float] = defaultdict(float)
        self._started_at = datetime.now(timezone.utc)

    async def _reply(self, update: Update, text: str) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    async def _authorize(self, update: Update, command: str) -> bool:
        chat = update.effective_chat
        if chat is None:
            return False
        if self.config.allowlist_enabled and chat.id not in self.config.allowed_chat_ids:
            await self._reply(update, format_access_denied(chat.id))
            return False

        key = (chat.id, command)
        now = time.monotonic()
        elapsed = now - self._last_commands[key]
        if elapsed < self.config.command_cooldown_seconds:
            await self._reply(update, "⏳ Bạn thao tác quá nhanh, vui lòng thử lại sau.")
            return False
        self._last_commands[key] = now
        return True

    @staticmethod
    def _symbol(args: list[str]) -> str | None:
        if not args:
            return None
        symbol = args[0].strip().upper()
        if symbol in INVALID_EQUITY_SYMBOLS:
            return None
        return symbol if SYMBOL_PATTERN.fullmatch(symbol) else None

    def _save_user(self, update: Update) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if chat is None:
            return
        self.subscribers.upsert_subscriber(
            chat_id=chat.id,
            username=user.username if user else None,
            first_name=user.first_name if user else None,
        )

    async def _on_demand_fetch_and_analyze(
            self, update: Update, symbol: str, user_settings: dict | None = None
    ) -> tuple:
        """Thu thập dữ liệu on-demand, thông báo chờ, rồi phân tích.

        Trả về (signal_view, signal_event).
        """
        fund_file = self.config.data_dir / "fundamental" / "quarter" / f"{symbol}_fundamentals.csv"
        if fund_file.is_file():
            msg = f"⏳ Đang cập nhật dữ liệu giá mới nhất từ DNSE cho <b>{symbol}</b> (~2-3s)..."
        else:
            msg = (
                f"⏳ Lần đầu tra cứu <b>{symbol}</b>: Đang thu thập dữ liệu BCTC & Giá...\n"
                f"Vui lòng chờ khoảng 10-15 giây."
            )
        await self._reply(update, msg)

        # Thu thập + phân tích trong thread riêng
        await asyncio.to_thread(_fetch_symbol_data, symbol, self.config)
        await asyncio.to_thread(_run_analytics, symbol, self.config)

        # Tự động thêm vào watchlist để analytics refresh cập nhật tiếp
        self.subscribers.add_to_watchlist(update.effective_chat.id, symbol)

        return self.analytics.get_signal_view(symbol, user_settings=user_settings)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update, "start"):
            return
        self._save_user(update)
        first_name = update.effective_user.first_name if update.effective_user else None
        await self._reply(update, format_start(first_name, update.effective_chat.id))

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update, "help"):
            return
        self._save_user(update)
        await self._reply(update, format_help())

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Hiển thị uptime và độ mới của dữ liệu đang phục vụ bot."""

        started = time.perf_counter()
        if not await self._authorize(update, "status"):
            return
        self._save_user(update)
        system_status = await asyncio.to_thread(self.analytics.get_system_status)
        ping_ms = (time.perf_counter() - started) * 1_000
        uptime_seconds = (
                datetime.now(timezone.utc) - self._started_at
        ).total_seconds()
        await self._reply(
            update,
            format_system_status(
                system_status,
                uptime_seconds=uptime_seconds,
                ping_ms=ping_ms,
            ),
        )

    async def portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Xem danh mục và khai báo giao dịch ngoài bot."""
        if not await self._authorize(update, "portfolio"):
            return
        self._save_user(update)
        if self.positions is None:
            await self._reply(update, "⚠️ Kho danh mục chưa được khởi tạo.")
            return
        args = [arg.strip() for arg in context.args]
        subcommand = args[0].lower() if args else "list"
        if subcommand in {"list", "show", "view"}:
            await self._reply(
                update,
                format_portfolio(
                    self.positions.list_open_positions(),
                    self.positions.get_portfolio_metrics(),
                ),
            )
            return
        if subcommand == "open":
            if len(args) != 4:
                await self._reply(
                    update,
                    "📥 Cú pháp: <code>/position open FPT 85000 100</code>\n"
                    "Giá nhập bằng đồng, khối lượng bằng cổ phiếu.",
                )
                return
            symbol = self._symbol(args[1:2])
            try:
                price_vnd = float(args[2].replace(",", ""))
                quantity = float(args[3].replace(",", ""))
            except ValueError:
                symbol = None
                price_vnd = quantity = 0
            if symbol is None or price_vnd <= 0 or quantity <= 0:
                await self._reply(update, "❌ Mã, giá và khối lượng không hợp lệ.")
                return
            execution = await asyncio.to_thread(
                self.positions.record_manual_open,
                symbol,
                price_vnd / 1000.0,
                quantity,
            )
            await self._reply(update, f"{'✅' if execution.applied else '❌'} {execution.reason}")
            return
        if subcommand == "close":
            if len(args) not in {2, 3}:
                await self._reply(
                    update,
                    "📤 Cú pháp: <code>/position close FPT [90000]</code>\n"
                    "Bỏ giá để dùng giá cuối cùng đã lưu.",
                )
                return
            symbol = self._symbol(args[1:2])
            if symbol is None:
                await self._reply(update, "❌ Mã cổ phiếu không hợp lệ.")
                return
            close_price = None
            if len(args) == 3:
                try:
                    close_price = float(args[2].replace(",", "")) / 1000.0
                except ValueError:
                    await self._reply(update, "❌ Giá đóng vị thế không hợp lệ.")
                    return
            execution = await asyncio.to_thread(
                self.positions.record_manual_close, symbol, close_price
            )
            pnl = f" | P/L: <code>{execution.realized_pnl:+,.0f} đ</code>" if execution.applied else ""
            await self._reply(update, f"{'✅' if execution.applied else '❌'} {execution.reason}{pnl}")
            return
        await self._reply(
            update,
            "💼 <b>Cú pháp danh mục</b>\n"
            "• <code>/position</code> — Xem vị thế mở\n"
            "• <code>/position open FPT 85000 100</code> — Khai báo đã mua\n"
            "• <code>/position close FPT 90000</code> — Đóng vị thế\n"
            "• <code>/portfolio</code> — Alias xem danh mục",
        )

    async def settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Cài đặt quản trị rủi ro SL/TP (2 bước chạm qua Inline Keyboard)."""
        if not await self._authorize(update, "settings"):
            return
        self._save_user(update)
        chat_id = update.effective_chat.id if update.effective_chat else 0
        user_cfg = self.subscribers.get_user_settings(chat_id)
        current_mode = user_cfg.get("sl_tp_mode", "FIXED")

        keyboard = [
            [
                InlineKeyboardButton(
                    "🛡 1. Chế độ Cố định An toàn (F0)" + (" ✅" if current_mode == "FIXED" else ""),
                    callback_data="set_mode:FIXED",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎯 2. Chế độ Cấu trúc Nến (Pro)" + (" ✅" if current_mode == "STRUCTURE" else ""),
                    callback_data="set_mode:STRUCTURE",
                )
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.effective_message:
            await update.effective_message.reply_text(
                format_settings(current_mode, chat_id),
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )

    async def settings_callback(
            self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Xử lý sự kiện bấm nút chọn chế độ SL/TP (Callback Query)."""
        query = update.callback_query
        if query is None or query.data is None:
            return

        data = query.data
        if not data.startswith("set_mode:"):
            return

        mode = data.split(":", 1)[1].strip().upper()
        chat_id = update.effective_chat.id if update.effective_chat else 0

        self.subscribers.set_sl_tp_mode(chat_id, mode)

        mode_name = (
            "Cấu trúc Nến (Order Block & BB)"
            if mode == "STRUCTURE"
            else "Cố định An toàn (-5% / +10%)"
        )
        await query.answer(f"✅ Đã chuyển sang: {mode_name}!")

        keyboard = [
            [
                InlineKeyboardButton(
                    "🛡 1. Chế độ Cố định An toàn (F0)" + (" ✅" if mode == "FIXED" else ""),
                    callback_data="set_mode:FIXED",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎯 2. Chế độ Cấu trúc Nến (Pro)" + (" ✅" if mode == "STRUCTURE" else ""),
                    callback_data="set_mode:STRUCTURE",
                )
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            format_settings(mode, chat_id),
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )

    async def setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Cài đặt khẩu vị đầu tư (/setup - 2 bước chạm qua Inline Keyboard)."""
        if not await self._authorize(update, "setup"):
            return
        self._save_user(update)
        chat_id = update.effective_chat.id if update.effective_chat else 0
        user_cfg = self.subscribers.get_user_settings(chat_id)
        current_mode = user_cfg.get("investment_mode", "SHORT_TERM")

        keyboard = [
            [
                InlineKeyboardButton(
                    "⚡️ 1. Ngắn hạn (Kỹ thuật / Lướt sóng)" + (" ✅" if current_mode == "SHORT_TERM" else ""),
                    callback_data="set_inv:SHORT_TERM",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏛 2. Dài hạn (Cơ bản / Tích sản)" + (" ✅" if current_mode == "LONG_TERM" else ""),
                    callback_data="set_inv:LONG_TERM",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔄 3. Cả hai (Đa khung thời gian)" + (" ✅" if current_mode == "BOTH" else ""),
                    callback_data="set_inv:BOTH",
                )
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.effective_message:
            await update.effective_message.reply_text(
                format_setup(current_mode, chat_id),
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )

    async def setup_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Xử lý sự kiện bấm nút chọn khẩu vị đầu tư (Callback Query)."""
        query = update.callback_query
        if query is None or query.data is None:
            return

        data = query.data
        if not data.startswith("set_inv:"):
            return

        mode = data.split(":", 1)[1].strip().upper()
        chat_id = update.effective_chat.id if update.effective_chat else 0

        self.subscribers.set_investment_mode(chat_id, mode)

        mode_name = (
            "Ngắn hạn (Kỹ thuật / Lướt sóng)"
            if mode == "SHORT_TERM"
            else "Dài hạn (Cơ bản / Tích sản)"
            if mode == "LONG_TERM"
            else "Cả hai (Đa khung thời gian)"
        )
        await query.answer(f"✅ Đã chọn: {mode_name}!")

        keyboard = [
            [
                InlineKeyboardButton(
                    "⚡️ 1. Ngắn hạn (Kỹ thuật / Lướt sóng)" + (" ✅" if mode == "SHORT_TERM" else ""),
                    callback_data="set_inv:SHORT_TERM",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏛 2. Dài hạn (Cơ bản / Tích sản)" + (" ✅" if mode == "LONG_TERM" else ""),
                    callback_data="set_inv:LONG_TERM",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔄 3. Cả hai (Đa khung thời gian)" + (" ✅" if mode == "BOTH" else ""),
                    callback_data="set_inv:BOTH",
                )
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            format_setup(mode, chat_id),
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )

    async def signal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Báo động & Quét bộ lọc tín hiệu Mua/Bán:

        - /signal: Mặc định quét Watchlist cá nhân của bạn (nếu trống sẽ nhắc thêm mã hoặc gợi ý quét thị trường).
        - /signal all (hoặc /signal market): Ép bot quét toàn bộ thị trường, không phụ thuộc vào watchlist.
        - /signal [MÃ] (Ví dụ /signal FPT): Xem báo động tín hiệu, điểm mua/bán, SL, TP của riêng 1 mã.
        """
        if not await self._authorize(update, "signal"):
            return
        self._save_user(update)
        chat_id = update.effective_chat.id if update.effective_chat else 0
        user_settings = self.subscribers.get_user_settings(chat_id)

        raw_arg = context.args[0].strip().upper() if context.args else None

        # TRƯỜNG HỢP 1: /signal (Không đối số) -> Mặc định quét Watchlist cá nhân
        if raw_arg is None:
            user_watchlist = self.subscribers.list_watchlist(chat_id)
            if not user_watchlist:
                await self._reply(
                    update,
                    "📋 <b>Danh mục theo dõi (Watchlist) của bạn đang trống!</b>\n\n"
                    "• Dùng <code>/watch [MÃ]</code> để thêm cổ phiếu theo dõi (Ví dụ: <code>/watch FPT</code>).\n"
                    "• Hoặc gõ <code>/signal all</code> để quét bộ lọc trên <b>Toàn bộ thị trường</b>.",
                )
                return

            scan_symbols = user_watchlist
            source_label = "Watchlist cá nhân của bạn"
            await self._reply(
                update,
                f"⏳ <b>Đang quét bộ lọc tín hiệu...</b>\n"
                f"Kiểm tra {len(scan_symbols)} mã trong <i>{source_label}</i>.",
            )

            results: list[tuple[SignalView, Any]] = []
            for sym in scan_symbols:
                view, event = self.analytics.get_signal_view(
                    sym, user_settings=user_settings
                )
                decision = self.risk_gate.evaluate(event) if (event and self.risk_gate and event.action == "BUY") else None
                results.append((view, decision))

            await self._reply(
                update,
                format_signal_scan(results, len(scan_symbols), source_label),
            )
            return

        # TRƯỜNG HỢP 2: /signal all hoặc /signal market -> Quét toàn bộ thị trường
        if raw_arg in ("ALL", "MARKET", "THITRUONG"):
            snapshot_syms = self.analytics.list_snapshot_symbols()
            scan_symbols = snapshot_syms if snapshot_syms else ["FPT", "HPG", "VNM", "MWG", "VCB", "SSI"]
            source_label = "Toàn bộ thị trường / Danh mục hệ thống"

            await self._reply(
                update,
                f"⏳ <b>Đang quét bộ lọc tín hiệu toàn thị trường...</b>\n"
                f"Kiểm tra {len(scan_symbols)} mã trong <i>{source_label}</i>.",
            )

            results = []
            for sym in scan_symbols:
                view, event = self.analytics.get_signal_view(
                    sym, user_settings=user_settings
                )
                decision = self.risk_gate.evaluate(event) if (event and self.risk_gate and event.action == "BUY") else None
                results.append((view, decision))

            await self._reply(
                update,
                format_signal_scan(results, len(scan_symbols), source_label),
            )
            return

        # TRƯỜNG HỢP 3: /signal [MÃ] -> Báo động tín hiệu cho 1 mã cụ thể
        symbol = self._symbol(context.args)
        if symbol is None:
            await self._reply(
                update,
                "📈 <b>Cú pháp lệnh /signal:</b>\n"
                "• <code>/signal</code> — Quét bộ lọc Mua/Bán trên <b>Watchlist cá nhân</b>\n"
                "• <code>/signal all</code> — Quét bộ lọc trên <b>Toàn bộ thị trường</b>\n"
                "• <code>/signal [MÃ]</code> — Xem tín hiệu, vùng mua, SL &amp; TP của <b>1 mã cụ thể</b> (Ví dụ: <code>/signal FPT</code>)",
            )
            return

        signal_view, signal_event = self.analytics.get_signal_view(
            symbol, user_settings=user_settings
        )
        if signal_view.price is None or signal_view.data_status == "MISSING":
            try:
                signal_view, signal_event = await self._on_demand_fetch_and_analyze(
                    update, symbol, user_settings=user_settings
                )
            except Exception:
                LOGGER.exception("On-demand fetch thất bại cho %s", symbol)
                await self._reply(update, f"❌ Có lỗi xảy ra khi quét tín hiệu cho {symbol}.")
                return

        decision = self.risk_gate.evaluate(signal_event) if (signal_event and self.risk_gate and signal_event.action == "BUY") else None
        await self._reply(update, format_signal_single(signal_view, decision))
        asyncio.create_task(_cleanup_symbol_data(symbol, self.config))

    async def check(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Tra cứu On-demand một mã cổ phiếu cụ thể (/check [MÃ]).

        Trả về: Giá hiện tại, tăng/giảm, khối lượng, trạng thái các chỉ báo (RSI, MACD, MA)
        kèm đồ thị kỹ thuật nhanh.
        """
        if not await self._authorize(update, "check"):
            return
        self._save_user(update)
        symbol = self._symbol(context.args)
        if symbol is None:
            await self._reply(
                update,
                "🔎 <b>Cú pháp tra cứu:</b> <code>/check [MÃ]</code>\n"
                "Ví dụ: <code>/check FPT</code>\n\n"
                "<i>Dùng để kiểm tra nhanh giá realtime, khối lượng, chỉ báo kỹ thuật (RSI, MACD, MA) và đồ thị nến của một cổ phiếu bất kỳ.</i>",
            )
            return

        try:
            signal_view, signal_event = await self._on_demand_fetch_and_analyze(
                update, symbol
            )
        except Exception:
            LOGGER.exception("On-demand fetch thất bại cho %s", symbol)
            await self._reply(update, f"❌ Có lỗi xảy ra khi thu thập dữ liệu cho {symbol}.")
            return

        # Gửi tin nhắn tra cứu chỉ số kỹ thuật
        metrics = signal_view.metrics or {}
        check_msg = format_check(
            view=signal_view,
            price_change=metrics.get("price_change"),
            price_change_pct=metrics.get("price_change_pct"),
            volume=metrics.get("volume"),
        )
        await self._reply(update, check_msg)

        # Vẽ và gửi đồ thị kỹ thuật nhanh Bollinger Bands
        try:
            from .chart_service import generate_bollinger_chart

            chart_path = await asyncio.to_thread(
                generate_bollinger_chart,
                symbol,
                self.config.data_dir,
            )
            if chart_path and chart_path.is_file() and update.effective_message:
                with open(chart_path, "rb") as photo_file:
                    await update.effective_message.reply_photo(
                        photo=photo_file,
                        caption=f"📈 Đồ thị kỹ thuật Bollinger Bands — {symbol}",
                    )
        except Exception:
            LOGGER.exception("Lỗi khi vẽ đồ thị nhanh cho %s", symbol)

        asyncio.create_task(_cleanup_symbol_data(symbol, self.config))

    async def market(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Kiểm tra dữ liệu thị trường VNINDEX, VN30."""
        if not await self._authorize(update, "market"):
            return
        self._save_user(update)

        await self._reply(
            update,
            "⏳ Đang lấy dữ liệu thị trường (VNINDEX, VN30)..."
        )

        try:
            # 1. Ưu tiên lấy từ vnstock để có tổng khối lượng chuẩn khớp 100% với bảng điện (khớp lệnh + thỏa thuận)
            vnindex_data = await asyncio.to_thread(_fetch_index_data_from_vnstock, "VNINDEX")
            vn30_data = await asyncio.to_thread(_fetch_index_data_from_vnstock, "VN30")

            # 2. Fallback sang DNSE client nếu vnstock thiếu dữ liệu
            if not vnindex_data or not vn30_data:
                client = DNSEClient(DNSEConfig())
                try:
                    end_date = datetime.now(VIETNAM_TIMEZONE).strftime("%Y-%m-%d")
                    start_date = (datetime.now(VIETNAM_TIMEZONE) - timedelta(days=7)).strftime("%Y-%m-%d")

                    if not vnindex_data:
                        vnindex_history = await asyncio.to_thread(
                            client.fetch_history, "VNINDEX", start_date, end_date, "1D", "INDEX"
                        )
                        if len(vnindex_history) >= 2:
                            vnindex_data = dict(vnindex_history[-1])
                            vnindex_data["prev_c"] = vnindex_history[-2].get("c")
                        else:
                            vnindex_data = dict(vnindex_history[-1]) if vnindex_history else {}

                    if not vn30_data:
                        vn30_history = await asyncio.to_thread(
                            client.fetch_history, "VN30", start_date, end_date, "1D", "INDEX"
                        )
                        if len(vn30_history) >= 2:
                            vn30_data = dict(vn30_history[-1])
                            vn30_data["prev_c"] = vn30_history[-2].get("c")
                        else:
                            vn30_data = dict(vn30_history[-1]) if vn30_history else {}
                finally:
                    client.close()

            await self._reply(update, format_market(vnindex_data, vn30_data))
        except Exception as exc:
            LOGGER.exception("Lỗi khi lấy dữ liệu market")
            await self._reply(update, "❌ Có lỗi xảy ra khi lấy dữ liệu thị trường.")

    async def chart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Vẽ biểu đồ Bollinger Bands cho một mã."""

        if not await self._authorize(update, "chart"):
            return
        self._save_user(update)
        symbol = self._symbol(context.args)
        if symbol is None:
            await self._reply(update, "Cú pháp: <code>/chart FPT</code>")
            return

        # Kiểm tra có data lịch sử chưa, nếu chưa thì fetch on-demand
        history_file = self.config.data_dir / "history" / f"{symbol}_1D.csv"
        if not history_file.is_file():
            await self._reply(
                update,
                f"⏳ Hệ thống đang thu thập dữ liệu <b>{symbol}</b> để vẽ biểu đồ...",
            )
            await asyncio.to_thread(_fetch_symbol_data, symbol, self.config)
            await asyncio.to_thread(_run_analytics, symbol, self.config)
            self.subscribers.add_to_watchlist(update.effective_chat.id, symbol)

        if not history_file.is_file():
            await self._reply(update, f"❌ Không thể lấy dữ liệu lịch sử cho {symbol}.")
            return

        try:
            from .chart_service import generate_bollinger_chart

            chart_path = await asyncio.to_thread(
                generate_bollinger_chart,
                symbol,
                self.config.data_dir,
            )
        except Exception:
            LOGGER.exception("Lỗi vẽ biểu đồ BB cho %s", symbol)
            await self._reply(update, f"❌ Lỗi khi vẽ biểu đồ cho {symbol}.")
            return

        if chart_path and chart_path.is_file():
            await update.effective_message.reply_photo(
                photo=open(chart_path, "rb"),
                caption=f"📈 Bollinger Bands — {symbol}",
            )
        else:
            await self._reply(update, f"❌ Không tạo được biểu đồ cho {symbol}.")

        # Lên lịch xóa data tạm (bao gồm ảnh chart) sau khi gửi xong
        asyncio.create_task(_cleanup_symbol_data(symbol, self.config))

    async def block(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Phân tích Order Block & Stochastic Oscillator (/block [MÃ])."""
        if not await self._authorize(update, "block"):
            return
        self._save_user(update)
        symbol = self._symbol(context.args)
        if symbol is None:
            await self._reply(
                update,
                "🧱 <b>Cú pháp tra cứu:</b> <code>/block [MÃ]</code>\n"
                "Ví dụ: <code>/block FPT</code>\n\n"
                "<i>Bắt đỉnh đáy ngắn hạn với Stochastic Oscillator (14, 3, 3), nhận diện vùng Order Block (Bullish/Bearish) và các mốc Kháng cự / Hỗ trợ chủ chốt.</i>",
            )
            return

        # Kiểm tra file lịch sử, nếu chưa có thì fetch on-demand
        history_file = self.config.data_dir / "history" / f"{symbol}_1D.csv"
        if not history_file.is_file():
            await self._reply(
                update,
                f"⏳ Hệ thống đang thu thập dữ liệu <b>{symbol}</b> để phân tích Order Block &amp; Stochastic...",
            )
            await asyncio.to_thread(_fetch_symbol_data, symbol, self.config)
            await asyncio.to_thread(_run_analytics, symbol, self.config)
            self.subscribers.add_to_watchlist(update.effective_chat.id, symbol)

        if not history_file.is_file():
            await self._reply(update, f"❌ Không thể lấy dữ liệu lịch sử cho {symbol}.")
            return

        try:
            from .chart_service import generate_order_block_chart

            chart_path, info_dict = await asyncio.to_thread(
                generate_order_block_chart,
                symbol,
                self.config.data_dir,
            )
        except Exception:
            LOGGER.exception("Lỗi phân tích Order Block cho %s", symbol)
            await self._reply(update, f"❌ Lỗi khi phân tích Order Block cho {symbol}.")
            return

        if not info_dict:
            await self._reply(update, f"❌ Không đủ dữ liệu lịch sử để phân tích cho {symbol}.")
            return

        # Gửi tin nhắn text phân tích
        block_msg = format_block(symbol, info_dict)
        await self._reply(update, block_msg)

        # Gửi ảnh biểu đồ nến kèm Order Block & Stochastic
        if chart_path and chart_path.is_file() and update.effective_message:
            try:
                with open(chart_path, "rb") as photo_file:
                    await update.effective_message.reply_photo(
                        photo=photo_file,
                        caption=f"🧱 Order Block & Stochastic (14, 3, 3) — {symbol}",
                    )
            except Exception:
                LOGGER.exception("Lỗi khi gửi ảnh biểu đồ block cho %s", symbol)

        asyncio.create_task(_cleanup_symbol_data(symbol, self.config))

    async def watch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update, "watch"):
            return
        self._save_user(update)
        symbol = self._symbol(context.args)
        if symbol is None:
            await self._reply(update, "Cú pháp: <code>/watch FPT</code>")
            return
        try:
            added = self.subscribers.add_to_watchlist(update.effective_chat.id, symbol)
        except ValueError as exc:
            await self._reply(update, f"⚠️ {_escape_error(exc)}")
            return
        message = (
            f"✅ Đã thêm <b>{symbol}</b> vào watchlist."
            if added
            else f"ℹ️ <b>{symbol}</b> đã có trong watchlist."
        )
        await self._reply(update, message)

    async def unwatch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update, "unwatch"):
            return
        self._save_user(update)
        symbol = self._symbol(context.args)
        if symbol is None:
            await self._reply(update, "Cú pháp: <code>/unwatch FPT</code>")
            return
        removed = self.subscribers.remove_from_watchlist(
            update.effective_chat.id, symbol
        )
        message = (
            f"✅ Đã xóa <b>{symbol}</b> khỏi watchlist."
            if removed
            else f"ℹ️ Không tìm thấy <b>{symbol}</b> trong watchlist."
        )
        await self._reply(update, message)

    async def watchlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update, "watchlist"):
            return
        self._save_user(update)
        args = [arg.strip() for arg in context.args]
        if args and args[0].lower() in {"add", "remove", "clear"}:
            action = args[0].lower()
            if action == "clear":
                count = self.subscribers.clear_watchlist(update.effective_chat.id)
                await self._reply(update, f"✅ Đã xóa {count} mã khỏi watchlist.")
                return
            symbol = self._symbol(args[1:])
            if symbol is None:
                await self._reply(
                    update,
                    "Cú pháp: <code>/watchlist add FPT</code> hoặc "
                    "<code>/watchlist remove FPT</code>",
                )
                return
            if action == "add":
                try:
                    added = self.subscribers.add_to_watchlist(
                        update.effective_chat.id, symbol
                    )
                except ValueError as exc:
                    await self._reply(update, f"⚠️ {_escape_error(exc)}")
                    return
                await self._reply(
                    update,
                    f"{'✅ Đã thêm' if added else 'ℹ️ Đã có'} <b>{symbol}</b>.",
                )
                return
            removed = self.subscribers.remove_from_watchlist(
                update.effective_chat.id, symbol
            )
            await self._reply(
                update,
                f"{'✅ Đã xóa' if removed else 'ℹ️ Không tìm thấy'} <b>{symbol}</b>.",
            )
            return

        symbols = self.subscribers.list_watchlist(update.effective_chat.id)
        await self._reply(update, format_watchlist(symbols))

    async def error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Ghi loại lỗi kèm chi tiết lỗi, không ghi token hay toàn bộ payload người dùng."""

        LOGGER.error("Telegram handler lỗi: %s - %s", type(context.error).__name__, context.error)


def _escape_error(exc: Exception) -> str:
    from html import escape

    return escape(str(exc), quote=False)
