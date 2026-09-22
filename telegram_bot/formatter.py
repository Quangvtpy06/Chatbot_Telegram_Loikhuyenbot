"""Định dạng tin nhắn trả về cho Telegram."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import BotConfig
from .analytics_repository import SignalView
from .models import SystemStatus

VIETNAM_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")


def _safe(text: str | None) -> str:
    return html.escape(str(text)) if text else ""


def format_start(first_name: str | None, chat_id: int) -> str:
    name = f" {_safe(first_name)}" if first_name else ""
    return (
        f"👋 <b>Xin chào{name}!</b>\n\n"
        "FINSHIELD - Hệ thống đưa ra tín hiệu đầu tư chứng khoán tại Việt Nam.\n"
        "Hệ thống thu thập dữ liệu từ API DNSE và vnstock.\n\n"
        "⚡️ <b>QUY TRÌNH CHUẨN ĐỂ NHẬN TÍN HIỆU REALTIME TRONG PHIÊN:</b>\n"
        "1️⃣ Gõ <code>/check [MÃ]</code> (Ví dụ: <code>/check FPT</code>) để bot nạp giá realtime mới nhất từ DNSE &amp; làm mới toàn bộ chỉ báo.\n"
        "2️⃣ Gõ <code>/signal [MÃ]</code> (Ví dụ: <code>/signal FPT</code>) để xem khuyến nghị Mua/Bán, các lý do chiến lược và mốc SL/TP.\n\n"
        "🔎 <b>Tra cứu &amp; Cập nhật Realtime:</b>\n"
        "• <code>/check [MÃ]</code> — Kéo giá realtime, chỉ báo (RSI, MACD, MA) + Đồ thị nến\n"
        "• <code>/block [MÃ]</code> — Bắt đỉnh/đáy ngắn hạn (Stochastic &amp; Order Block)\n"
        "• <code>/chart [MÃ]</code> — Biểu đồ kỹ thuật Bollinger Bands\n\n"
        "📈 <b>Bộ lọc &amp; Báo động:</b>\n"
        "• <code>/signal</code> — Quét bộ lọc trên Watchlist cá nhân của bạn\n"
        "• <code>/signal all</code> — Quét bộ lọc trên Toàn bộ thị trường\n"
        "• <code>/signal [MÃ]</code> — Xem khuyến nghị Mua/Bán, điểm SL/TP &amp; phân tích 3 trụ cột\n\n"
        "📊 <b>Theo dõi &amp; Cài đặt:</b>\n"
        "• <code>/watch [MÃ]</code> — Thêm mã vào Watchlist để nhận thông báo tự động khi nổ sóng\n"
        "• <code>/unwatch [MÃ]</code> — Xóa mã khỏi Watchlist\n"
        "• <code>/watchlist</code> — Xem danh sách các mã đang theo dõi\n"
        "• <code>/setup</code> — Cài đặt khẩu vị đầu tư (Ngắn hạn kỹ thuật / Dài hạn cơ bản / Cả hai)\n"
        "• <code>/settings</code> — Cài đặt quản trị rủi ro SL/TP (Cố định F0 hoặc Cấu trúc nến Pro)\n"
        "• <code>/market</code> — Xem dữ liệu VNINDEX, VN30\n"
        "• <code>/help</code> — Cẩm nang hướng dẫn sử dụng chi tiết\n\n"
        f"Chat ID của bạn: <code>{chat_id}</code>\n\n"
        "🔔 <i>Hệ thống tự động chạy ngầm và gửi thông báo thời gian thực ngay khi phát hiện tín hiệu MUA / BÁN mới!</i>"
    )


def format_help() -> str:
    return (
        "📘 <b>CẨM NANG HƯỚNG DẪN SỬ DỤNG FINSHIELD BOT</b>\n\n"
        "🎯 <b>QUY TRÌNH 3 BƯỚC VÀNG CHO NHÀ ĐẦU TƯ:</b>\n"
        "<i>(Đặc biệt quan trọng đối với người dùng mới trong phiên giao dịch)</i>\n\n"
        "1️⃣ <b>BƯỚC 1: CÀI ĐẶT KHẨU VỊ &amp; QUẢN TRỊ RỦI RO (Làm 1 lần đầu)</b>\n"
        "• <code>/setup</code> — Chọn khẩu vị đầu tư cá nhân:\n"
        "  - ⚡️ <i>Ngắn hạn:</i> Lọc theo kỹ thuật (MA, RSI, MACD, Volume bùng nổ, Khối ngoại).\n"
        "  - 🏛 <i>Dài hạn:</i> Lọc theo định giá cơ bản (P/E, P/B, ROE, D/E, BCTC quý mới nhất).\n"
        "  - 🔄 <i>Cả hai:</i> Đa khung thời gian (Định giá tốt + Điểm vào kỹ thuật tối ưu).\n"
        "• <code>/settings</code> — Cài đặt cơ chế Cắt lỗ (SL) &amp; Chốt lời (TP):\n"
        "  - 🛡 <i>Cố định An toàn (F0):</i> Cắt lỗ -5%, Chốt lời +10% (Tỷ lệ R:R = 2.0 chuẩn mực).\n"
        "  - 🎯 <i>Cấu trúc Nến (Pro):</i> Cắt lỗ theo đáy Order Block, Chốt lời theo đỉnh cản &amp; BB.\n\n"
        "2️⃣ <b>BƯỚC 2: CẬP NHẬT REALTIME &amp; SOI SỨC KHỎE (QUAN TRỌNG NHẤT)</b>\n"
        "⚠️ <b>LƯU Ý CỰC KỲ QUAN TRỌNG:</b>\n"
        "Nhiều nhà đầu tư mới thường gõ ngay <code>/signal</code> mà không biết rằng:\n"
        "👉 <b>BẠN NÊN DÙNG <code>/check [MÃ]</code> TRƯỚC KHI DÙNG <code>/signal</code>!</b>\n"
        "• <code>/check [MÃ]</code> (Ví dụ: <code>/check FPT</code>):\n"
        "  - Kích hoạt bot <b>kết nối trực tiếp sàn DNSE</b> để nạp giá realtime mới nhất từng giây.\n"
        "  - Quét giao dịch Khối ngoại 5 phiên, đồng bộ BCTC và làm mới toàn bộ chỉ báo kỹ thuật.\n"
        "  - Trả về: Giá realtime, biến động %, RSI, MACD, MA, P/E, ROE kèm ảnh đồ thị nến.\n"
        "• <code>/block [MÃ]</code> — Soi vùng gom hàng (Bullish OB), vùng cản (Bearish OB) &amp; đỉnh/đáy Stochastic.\n"
        "• <code>/chart [MÃ]</code> — Biểu đồ nến dải Bollinger Bands trực quan chi tiết.\n\n"
        "3️⃣ <b>BƯỚC 3: QUÉT TÍN HIỆU, ĐIỂM VÀO/RA &amp; NHẬN BÁO ĐỘNG</b>\n"
        "• <code>/signal [MÃ]</code> (Ví dụ: <code>/signal FPT</code>):\n"
        "  - Xem khuyến nghị hành động: 🟢 MUA, 🔴 BÁN hoặc 🟡 QUAN SÁT THÊM.\n"
        "  - Bóc tách 3 trụ cột: <b>Chiến lược Xu hướng &amp; MA</b>, <b>Chiến lược Động lượng</b>, <b>Dòng tiền Khối ngoại</b>.\n"
        "  - Cung cấp giá vào lệnh, điểm cắt lỗ (SL), chốt lời (TP) và tỷ lệ Lời/Lỗ (R:R).\n"
        "• <code>/watch [MÃ]</code> (Ví dụ: <code>/watch FPT</code>):\n"
        "  - Thêm cổ phiếu vào Watchlist. Bot sẽ <b>tự động quét ngầm và bắn tin nhắn cảnh báo</b> ngay khi xuất hiện điểm nổ Mua/Bán trong phiên!\n"
        "• <code>/signal</code> — Quét nhanh toàn bộ cổ phiếu trong Watchlist cá nhân.\n"
        "• <code>/signal all</code> — Quét lọc cơ hội giải ngân trên <b>Toàn bộ thị trường</b>.\n"
        "• <code>/watchlist</code> | <code>/unwatch [MÃ]</code> — Xem và quản lý danh mục theo dõi.\n\n"
        "────────────────────\n"
        "ℹ️ <b>CÁC LỆNH TIỆN ÍCH KHÁC:</b>\n"
        "• <code>/market</code> — Xem nhanh dữ liệu chỉ số VNINDEX, VN30.\n"
        "• <code>/status</code> — Kiểm tra độ trễ mạng (ping) và độ mới của dữ liệu hệ thống.\n"
        "• <code>/help</code> — Xem lại cẩm nang hướng dẫn này bất cứ lúc nào."
    )


def format_system_status(
        status: SystemStatus,
        *,
        uptime_seconds: float,
        ping_ms: float,
) -> str:
    """Hiển thị sức khỏe bot và độ mới của các nguồn dữ liệu."""

    def duration(value: float) -> str:
        total = max(0, int(value))
        days, remainder = divmod(total, 86_400)
        hours, remainder = divmod(remainder, 3_600)
        minutes = remainder // 60
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    def local_datetime(value: str | None) -> str:
        if not value:
            return "Chưa có dữ liệu"
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=VIETNAM_TIMEZONE)
            return parsed.astimezone(VIETNAM_TIMEZONE).strftime("%d/%m/%Y %H:%M:%S")
        except ValueError:
            return str(value)

    data_ready = (
            status.database_available
            and status.report_available
            and status.latest_realtime_at is not None
            and status.latest_history_date is not None
    )
    system_ready = data_ready and status.report_status in {"PASS", "OK"}
    feed_icon = "🟢" if data_ready else "🟡"
    report_icon = "🟢" if status.report_status in {"PASS", "OK"} else "🟡"
    ping_label = "Rất mượt" if ping_ms < 100 else "Ổn định" if ping_ms < 500 else "Chậm"
    fundamental = status.latest_fundamental_period or "Chưa có dữ liệu"
    if status.latest_fundamental_source:
        fundamental += f" ({status.latest_fundamental_source})"

    lines = [
        "🤖 <b>TRẠNG THÁI HỆ THỐNG (SYSTEM STATUS)</b>",
        f"🟢 Bot Core: Live (Uptime: {duration(uptime_seconds)})",
        f"🟢 Ping xử lý: {ping_ms:.0f}ms ({ping_label})",
        "",
        "📦 <b>DỮ LIỆU CHỨNG KHOÁN (DATA FEED)</b>",
        "• Nguồn cung cấp: DNSE REST + KBS BCTC",
        f"• Trạng thái dữ liệu: {feed_icon} {'Sẵn sàng' if data_ready else 'Chưa đồng bộ đủ'}",
        f"• Realtime mới nhất: {local_datetime(status.latest_realtime_at)}",
        f"• Lịch sử giá: {status.latest_history_date or 'Chưa có dữ liệu'}",
        f"• BCTC quý mới nhất: {fundamental}",
        f"• Analytics: {report_icon} {status.report_status} — {status.snapshot_symbols} mã "
        f"({status.eligible_symbols} đủ điều kiện, {status.blocked_symbols} bị chặn)",
    ]
    if status.issues:
        lines.extend(["", "⚠️ " + html.escape(str(status.issues[0]))])
    lines.extend(
        [
            "",
            "⚡ Hệ thống hoạt động bình thường. Sẵn sàng nhận lệnh!"
            if system_ready
            else "⚠️ Bot đang chạy nhưng dữ liệu chưa đầy đủ; lệnh phân tích sẽ tự đồng bộ khi cần.",
        ]
    )
    return "\n".join(lines)


def format_market(vnindex_data: dict, vn30_data: dict) -> str:
    def _format_index(name: str, data: dict) -> str:
        if not data:
            return f"<b>{name}:</b> Không có dữ liệu"

        c = data.get("c", 0.0)
        # Ưu tiên so sánh với giá tham chiếu phiên trước (prev_c) thay vì giá mở cửa (o)
        ref = data.get("prev_c") or data.get("o", 0.0)
        v = data.get("v", 0.0)
        diff = c - ref
        pct = (diff / ref * 100) if ref > 0 else 0

        icon = "🟢" if diff > 0 else "🔴" if diff < 0 else "🟡"
        sign = "+" if diff > 0 else ""

        return (
            f"{icon} <b>{name}</b>: <code>{c:,.2f}</code> "
            f"({sign}{diff:,.2f} | {sign}{pct:,.2f}%)\n"
            f"   Khối lượng: <code>{v:,.0f} cp</code>"
        )

    return (
        "📈 <b>THỊ TRƯỜNG CHUNG</b>\n\n"
        f"{_format_index('VNINDEX', vnindex_data)}\n\n"
        f"{_format_index('VN30', vn30_data)}"
    )


def _clean_reason(reason: str) -> str:
    """Chuyển lý do kỹ thuật nội bộ thành diễn giải tiếng Việt dễ hiểu và escape HTML."""
    r = reason.strip()
    while r.startswith("•") or r.startswith("-"):
        r = r.lstrip("•- ").strip()
    if "Không tính được ROE TTM" in r:
        clean = "Cần thêm dữ liệu các quý trước để tính ROE TTM chuẩn xác"
    elif "Thiếu realtime/API realtime" in r:
        clean = "Dữ liệu realtime đang được đồng bộ thêm từ sàn"
    elif "Data Gate chặn: data_status='DATA WARNING'" in r:
        clean = "Dữ liệu đang hoàn thiện đồng bộ (DATA WARNING)"
    elif "Data Gate chặn: signal_status='NO SIGNAL'" in r:
        clean = "Chưa đạt điều kiện kích hoạt vị thế mua mới"
    else:
        clean = r
    return html.escape(clean)


def format_check(
        view: SignalView,
        price_change: float | None = None,
        price_change_pct: float | None = None,
        volume: float | None = None,
) -> str:
    """Format kết quả tra cứu On-demand cho 1 mã cổ phiếu (/check)."""
    symbol = view.symbol
    raw_price = view.price or 0.0
    price = raw_price * 1000 if 0 < raw_price < 1000 else raw_price
    metrics = view.metrics or {}

    lines = [f"🔎 <b>TRA CỨU CỔ PHIẾU: {symbol}</b>"]

    price_parts = []
    if price > 0:
        price_parts.append(f"<code>{price:,.0f} đ</code>")
    if price_change is not None and price_change_pct is not None:
        sign = "+" if price_change > 0 else ""
        icon = "🟢" if price_change > 0 else "🔴" if price_change < 0 else "🟡"
        price_parts.append(f"{icon} {sign}{price_change:,.0f} ({sign}{price_change_pct:.2f}%)")
    if price_parts:
        lines.append(f"💵 <b>Giá hiện tại:</b> {' '.join(price_parts)}")

    vol = volume or metrics.get("volume") or metrics.get("latest_volume")
    if not vol and metrics.get("total_volume"):
        raw_tv = float(metrics["total_volume"])
        vol = raw_tv * 10.0 if raw_tv < 50_000_000 else raw_tv
    if vol:
        lines.append(f"📦 <b>Khối lượng:</b> <code>{vol:,.0f} cp</code>")

    tech_lines = []
    trend = metrics.get("trend_state")
    if trend:
        trend_vi = (
            "🟢 TĂNG (BULLISH)"
            if trend == "BULLISH"
            else "🔴 GIẢM (BEARISH)"
            if trend == "BEARISH"
            else "🟡 TÍCH LŨY (SIDEWAY)"
        )
        tech_lines.append(f"• Xu hướng: {trend_vi}")

    rsi = metrics.get("rsi_14")
    if rsi is not None:
        rsi_desc = "Quá mua (&gt;70)" if rsi >= 70 else "Quá bán (&lt;30)" if rsi <= 30 else "Trung tính"
        tech_lines.append(f"• RSI (14): <code>{rsi:.1f}</code> ({rsi_desc})")

    sma20 = metrics.get("sma_20")
    sma50 = metrics.get("sma_50")
    if sma20 is not None and sma50 is not None:
        sma20_val = sma20 * 1000 if 0 < sma20 < 1000 else sma20
        sma50_val = sma50 * 1000 if 0 < sma50 < 1000 else sma50
        ma_rel = (
            "Giá trên MA20 &amp; MA50"
            if price > sma20_val and price > sma50_val
            else "Giá dưới MA20"
            if price < sma20_val
            else "Giá bám sát MA20"
        )
        tech_lines.append(
            f"• MA20 / MA50: <code>{sma20_val:,.0f} đ</code> / <code>{sma50_val:,.0f} đ</code> ({ma_rel})")

    macd = metrics.get("macd")
    macd_sig = metrics.get("macd_signal")
    if macd is not None and macd_sig is not None:
        macd_desc = "Trên Signal (Tích cực)" if macd > macd_sig else "Dưới Signal (Điều chỉnh)"
        tech_lines.append(f"• MACD: <code>{macd:.2f}</code> | Signal: <code>{macd_sig:.2f}</code> ({macd_desc})")

    foreign_vol = metrics.get("foreign_net_volume_5d")
    if foreign_vol is not None:
        f_icon = "🟢 Mua ròng" if foreign_vol > 0 else "🔴 Bán ròng" if foreign_vol < 0 else "🟡 Cân bằng"
        tech_lines.append(f"• Khối ngoại (5 phiên): {f_icon} <code>{foreign_vol:+,.0f} cp</code>")

    bb_lower = metrics.get("bollinger_lower_20")
    bb_upper = metrics.get("bollinger_upper_20")
    bb_bw = metrics.get("bollinger_bandwidth_20")
    if bb_lower is not None and bb_upper is not None:
        bb_lower_val = bb_lower * 1000 if 0 < bb_lower < 1000 else bb_lower
        bb_upper_val = bb_upper * 1000 if 0 < bb_upper < 1000 else bb_upper
        bw_text = ""
        if bb_bw is not None:
            bw_pct = bb_bw * 100
            state = "Co thắt (Squeeze)" if bw_pct < 8.0 else "Nới rộng (Expanding)" if bw_pct >= 10.0 else "Ổn định"
            bw_text = f" (Độ rộng: <code>{bw_pct:.1f}%</code> — {state})"
        tech_lines.append(f"• Bollinger Bands (20, 2): <code>{bb_lower_val:,.0f} đ</code> — <code>{bb_upper_val:,.0f} đ</code>{bw_text}")

    if tech_lines:
        lines.append("\n📊 <b>Trạng thái chỉ báo:</b>\n" + "\n".join(tech_lines))

    fund_lines = []
    pe = metrics.get("pe_quarter") if metrics.get("pe_quarter") is not None else metrics.get("pe_year",
                                                                                             metrics.get("pe"))
    pb = metrics.get("pb_quarter") if metrics.get("pb_quarter") is not None else metrics.get("pb_year",
                                                                                             metrics.get("pb"))
    roe = metrics.get("roe_quarter") if metrics.get("roe_quarter") is not None else metrics.get("roe_year",
                                                                                                metrics.get("roe"))
    roe_ttm = metrics.get("roe_ttm")

    if pe is not None:
        fund_lines.append(f"P/E: <code>{pe:.1f}</code>")
    if pb is not None:
        fund_lines.append(f"P/B: <code>{pb:.1f}</code>")
    if roe_ttm is not None:
        fund_lines.append(f"ROE: <code>{roe_ttm * 100:.1f}%</code>")
    elif roe is not None:
        fund_lines.append(f"ROE: <code>{roe * 100:.1f}%</code>")

    if fund_lines:
        lines.append(f"\n💼 <b>Chỉ số Cơ bản:</b> {' | '.join(fund_lines)}")

    lines.append("\n📈 <i>Đồ thị kỹ thuật nhanh Bollinger Bands gửi kèm bên dưới:</i>")
    return "\n".join(lines)


def format_signal_scan(
        results: list[tuple[SignalView, Any]],
        total_scanned: int,
        source_label: str = "Watchlist",
) -> str:
    """Format kết quả quét bộ lọc tín hiệu (Alert & Scanner) /signal."""
    lines = [
        "📈 <b>BỘ LỌC TÍN HIỆU (ALERT &amp; SCANNER)</b>",
        f"🎯 Phạm vi quét: <b>{source_label}</b> ({total_scanned} mã)",
    ]

    buys = [(v, d) for (v, d) in results if v.action == "BUY"]
    sells = [(v, d) for (v, d) in results if v.action == "SELL"]
    holds = [(v, d) for (v, d) in results if v.action == "HOLD"]
    no_signals = [(v, d) for (v, d) in results if v.action == "NO SIGNAL"]

    if buys:
        lines.append(f"\n🟢 <b>TÍN HIỆU MUA (BUY) — {len(buys)} mã:</b>")
        for view, decision in buys:
            raw_p = view.price or 0.0
            price = raw_p * 1000 if 0 < raw_p < 1000 else raw_p
            raw_sl = view.stop_loss
            sl = (raw_sl * 1000 if raw_sl and raw_sl < 1000 else raw_sl) if raw_sl else None
            raw_tp = view.take_profit
            tp = (raw_tp * 1000 if raw_tp and raw_tp < 1000 else raw_tp) if raw_tp else None

            item = [f"• <b>{view.symbol}</b>: <code>{price:,.0f} đ</code>"]
            targets = []
            if sl:
                targets.append(f"SL: <code>{sl:,.0f}</code>")
            if tp:
                targets.append(f"TP: <code>{tp:,.0f}</code>")
            if targets:
                item.append(f"({' | '.join(targets)})")
            lines.append(" ".join(item))

    if sells:
        lines.append(f"\n🔴 <b>TÍN HIỆU BÁN / CẢNH BÁO (SELL) — {len(sells)} mã:</b>")
        for view, decision in sells:
            raw_p = view.price or 0.0
            price = raw_p * 1000 if 0 < raw_p < 1000 else raw_p
            reason = _clean_reason(view.reasons[0]) if view.reasons else "Gãy xu hướng"
            lines.append(f"• <b>{view.symbol}</b>: <code>{price:,.0f} đ</code> — <i>{reason}</i>")

    if holds:
        lines.append(f"\n🟡 <b>QUAN SÁT THÊM (HOLD) — {len(holds)} mã:</b>")
        for view, decision in holds:
            raw_p = view.price or 0.0
            price = raw_p * 1000 if 0 < raw_p < 1000 else raw_p
            meta = getattr(view, "metadata", {}) or {}
            sup = meta.get("ob_support")
            res = meta.get("ob_resistance")
            sup_t = f"{sup * 1000:,.0f}" if sup and sup < 1000 else (f"{sup:,.0f}" if sup else "—")
            res_t = f"{res * 1000:,.0f}" if res and res < 1000 else (f"{res:,.0f}" if res else "—")
            lines.append(
                f"• <b>{view.symbol}</b>: <code>{price:,.0f} đ</code> (Hỗ trợ: <code>{sup_t}</code> | Cản: <code>{res_t}</code>)"
            )

    if no_signals:
        lines.append(f"\n⚠️ <b>DỮ LIỆU THIẾU / ĐANG ĐỒNG BỘ:</b> " + ", ".join(f"<code>{v.symbol}</code>" for v, _ in no_signals))

    if not buys and not sells:
        lines.append(
            "\n💡 <i>Hiện tại các mã đang vận động tích lũy trong biên độ hỗ trợ – kháng cự. "
            "Bot sẽ gửi thông báo ngay khi có điểm nổ Mua/Bán mới.</i>"
        )

    lines.append(
        "\n💡 <i>Mẹo: Gõ <code>/check [MÃ]</code> (Ví dụ: <code>/check FPT</code>) để cập nhật giá realtime từ sàn DNSE &amp; làm mới chỉ báo trước khi giải ngân.</i>"
    )

    return "\n".join(lines).strip()


def format_settings(current_mode: str, chat_id: int) -> str:
    mode_text = (
        "🎯 <b>Cấu trúc Nến (Order Block &amp; BB)</b>"
        if current_mode == "STRUCTURE"
        else "🛡 <b>Cố định An toàn (-5% / +10%)</b>"
    )
    return (
        "⚙️ <b>CÀI ĐẶT QUẢN TRỊ RỦI RO (SL &amp; TP)</b>\n\n"
        f"Hiện tại bạn đang áp dụng: {mode_text}\n\n"
        "👉 <i>Hãy chạm vào nút bên dưới để chọn chế độ phù hợp với khẩu vị đầu tư của bạn:</i>\n\n"
        "1️⃣ <b>🛡 Chế độ Cố định An toàn (Mặc định cho người mới / F0):</b>\n"
        "• <b>Cắt lỗ (SL):</b> Cố định <code>-5.0%</code>\n"
        "• <b>Chốt lời (TP):</b> Cố định <code>+10.0%</code>\n"
        "• <b>Tỷ lệ Lời/Lỗ:</b> Chuẩn <code>R:R = 2.0</code> (Lãi gấp đôi Lỗ)\n"
        "💡 <i>Ưu điểm: Đơn giản, dễ nhớ, kỷ luật thép, bảo vệ an toàn tối đa.</i>\n\n"
        "2️⃣ <b>🎯 Chế độ Cấu trúc Nến (Khuyên dùng Pro / Thực chiến):</b>\n"
        "• <b>Cắt lỗ (SL):</b> Đáy vùng Gom hàng (Bullish Order Block) / Hỗ trợ kỹ thuật\n"
        "• <b>Chốt lời (TP):</b> Đỉnh vùng Cản (Bearish Order Block) / Bollinger Upper\n"
        "• <b>Bộ lọc R:R:</b> Tự động hủy lệnh nếu dư địa lời không gấp đôi lỗ (R:R &lt; 2.0)\n"
        "💡 <i>Ưu điểm: Tối ưu lợi nhuận theo từng mã, điểm cắt lỗ chuẩn bài kỹ thuật tránh bị quét oan.</i>"
    )


def format_setup(current_mode: str, chat_id: int) -> str:
    """Format tin nhắn hướng dẫn chọn khẩu vị đầu tư (/setup)."""
    mode_titles = {
        "SHORT_TERM": "⚡️ <b>Ngắn hạn (Kỹ thuật / Momentum)</b>",
        "LONG_TERM": "🏛 <b>Dài hạn (Cơ bản / Tích sản)</b>",
        "BOTH": "🔄 <b>Cả hai (Đa khung thời gian)</b>",
    }
    mode_text = mode_titles.get(current_mode, "⚡️ <b>Ngắn hạn (Kỹ thuật)</b>")
    return (
        "🎯 <b>CÀI ĐẶT KHẨU VỊ ĐẦU TƯ (/setup)</b>\n\n"
        f"Khẩu vị hiện tại của bạn: {mode_text}\n\n"
        "👉 <i>Chạm vào nút bên dưới để chọn chế độ phát tín hiệu phù hợp nhất:</i>\n\n"
        "1️⃣ ⚡️ <b>Chế độ Ngắn hạn (Kỹ thuật / Momentum):</b>\n"
        "• <b>Tần suất:</b> Bám sát nhịp biến động trong phiên theo tín hiệu dòng tiền, bùng nổ nến, Order Block, Bollinger Bands, RSI &amp; MACD.\n"
        "• <b>Quản trị rủi ro:</b> SL/TP sát (Cố định -5%/+10% hoặc cấu trúc nến), kỷ luật thép theo phiên.\n"
        "💡 <i>Phù hợp: Nhà đầu tư lướt sóng, bám bảng điện, đánh theo đà tăng trưởng.</i>\n\n"
        "2️⃣ 🏛 <b>Chế độ Dài hạn (Cơ bản / Tích sản):</b>\n"
        "• <b>Tần suất:</b> Lọc sạch 100% tiếng ồn rung lắc nến ngày. Chỉ gửi tín hiệu khi có <b>sự kiện trọng yếu</b> (định giá P/E, P/B chiết khấu sâu hoặc quá nóng, KQKD quý mới, đột biến đòn bẩy D/E hoặc ROE).\n"
        "• <b>Quản trị rủi ro:</b> Bỏ cắt lỗ theo nến ngày, nới rộng biên an toàn (-15% / +35%) hoặc theo định giá hợp lý.\n"
        "💡 <i>Phù hợp: Nhà đầu tư tích sản, bận rộn, coi trọng giá trị nội tại doanh nghiệp.</i>\n\n"
        "3️⃣ 🔄 <b>Chế độ Cả hai (Đa khung thời gian):</b>\n"
        "• Nhận song song cả 2 luồng tín hiệu (có tiền tố <code>⚡️ [NGẮN HẠN]</code> và <code>🏛 [DÀI HẠN]</code>) để vừa tối ưu điểm ra/vào ngắn hạn, vừa nắm chắc giá trị dài hạn."
    )


def format_signal_single(view: SignalView, decision: Any = None) -> str:
    """Format tín hiệu cho 1 mã cụ thể theo đúng yêu cầu /signal."""
    symbol = view.symbol
    raw_p = view.price or 0.0
    price = raw_p * 1000 if 0 < raw_p < 1000 else raw_p
    action = view.action
    meta = getattr(view, "metadata", {}) or {}

    # Kiểm tra các lý do từ chối từ Risk Gate (chỉ áp dụng khi phát tín hiệu BUY mở vị thế)
    is_blocked = False
    block_msg = ""
    applied_floor_note = False
    if decision is not None and action == "BUY":
        verdict = getattr(decision, "verdict", "ALLOW")
        block_reasons = getattr(decision, "block_reasons", []) or []
        allow_reasons = getattr(decision, "allow_reasons", []) or []

        if verdict == "BLOCK":
            is_blocked = True
            for br in block_reasons:
                reason = str(br or "")
                if "Khoảng cách cắt lỗ quá xa" in reason or "> 20%" in reason:
                    block_msg = "Khoảng cách cắt lỗ quá xa (> 20%) — Khuyến nghị thu hẹp tỷ trọng để bảo toàn vốn."
                    break
                if "R:R" in reason and "< ngưỡng" in reason:
                    block_msg = "Tỷ lệ Lời/Lỗ (R:R) < 2.0 — Biên lợi nhuận mỏng so với rủi ro; cân nhắc giải ngân tỷ trọng thăm dò hoặc chờ điểm vào tối ưu hơn."
                    break
            if not block_msg:
                reasons_str = "; ".join(str(r) for r in block_reasons if r)
                block_msg = f"Chưa thỏa mãn tiêu chuẩn giải ngân danh mục an toàn ({reasons_str})."

        for ar in allow_reasons:
            reason = str(ar or "")
            if "sàn rủi ro 1.0%" in reason or "sàn stop distance" in reason:
                applied_floor_note = True

    if action == "BUY":
        icon = "🟢"
        act_text = "MUA (BUY)"
    elif action == "SELL":
        icon = "🔴"
        act_text = "BÁN / HẠ TỶ TRỌNG (SELL)"
    elif action == "HOLD":
        icon = "🟡"
        act_text = "QUAN SÁT THÊM (HOLD)"
    else:
        icon = "⚠️"
        act_text = "CHƯA CÓ TÍN HIỆU (DỮ LIỆU THIẾU / ĐANG ĐỒNG BỘ)"

    lines = [
        f"📈 <b>BÁO ĐỘNG TÍN HIỆU: {symbol}</b>",
        f"🎯 <b>Hành động:</b> {icon} <b>{act_text}</b>",
    ]

    if action == "HOLD":
        lines.append("📌 <i>Khuyến nghị: Đứng ngoài quan sát thêm, chờ tín hiệu xác nhận dòng tiền.</i>")

    if price > 0:
        price_label = "Vùng giá khuyến nghị" if action == "BUY" else ("Vùng giá cảnh báo" if action == "SELL" else "Giá tham chiếu hiện tại")
        lines.append(f"💵 <b>{price_label}:</b> <code>{price:,.0f} đ</code>")

    raw_sl = view.stop_loss
    sl = (raw_sl * 1000 if raw_sl and raw_sl < 1000 else raw_sl) if raw_sl else None
    raw_tp = view.take_profit
    tp = (raw_tp * 1000 if raw_tp and raw_tp < 1000 else raw_tp) if raw_tp else None

    sl_src = meta.get("sl_source", "FIXED")
    tp_src = meta.get("tp_source", "FIXED")

    if action == "HOLD":
        targets = []
        if sl and price > 0:
            targets.append(f"🛡 <b>Hỗ trợ quan sát (Support):</b> <code>{sl:,.0f} đ</code>")
        if tp and price > 0:
            targets.append(f"🎯 <b>Cản quan sát (Resistance):</b> <code>{tp:,.0f} đ</code>")
        if targets:
            lines.append("\n".join(targets))
    elif action == "BUY" and (sl or tp):
        targets = []
        if sl and price > 0:
            sl_pct = (sl - price) / price * 100
            if sl_src == "ORDER_BLOCK":
                sl_label = "Đáy Bullish OB"
            elif sl_src == "FALLBACK_FIXED":
                sl_label = "Fallback tỷ lệ cố định do chưa có OB"
            else:
                sl_label = "Cố định An toàn"
            targets.append(f"🛑 <b>Cắt lỗ (SL):</b> <code>{sl:,.0f} đ</code> ({sl_pct:.1f}% - <i>{sl_label}</i>)")

        if tp and price > 0:
            tp_pct = (tp - price) / price * 100
            if tp_src == "ORDER_BLOCK":
                tp_label = "Đỉnh Bearish OB"
            elif tp_src == "BOLLINGER_UPPER":
                tp_label = "Dải trên Bollinger"
            elif tp_src == "FALLBACK_FIXED":
                tp_label = "Fallback mục tiêu kỳ vọng"
            else:
                tp_label = "Cố định An toàn"
            targets.append(f"🎯 <b>Chốt lời (TP):</b> <code>{tp:,.0f} đ</code> (+{tp_pct:.1f}% - <i>{tp_label}</i>)")

        if targets:
            lines.append("\n".join(targets))

        # Tính tỷ lệ R:R thực tế nếu có đủ SL và TP
        if sl and tp and price > 0 and price > sl:
            risk = price - sl
            reward = tp - price
            if risk > 0 and reward > 0:
                rr = reward / risk
                lines.append(f"⚖️ <b>Tỷ lệ Lời/Lỗ (R:R):</b> <code>{rr:.2f}</code>")
    elif action == "SELL":
        if sl:
            lines.append(f"🛑 <b>Vùng cản trên / Cắt lỗ:</b> <code>{sl:,.0f} đ</code>")
        if tp:
            lines.append(f"🎯 <b>Ngưỡng hỗ trợ kỳ vọng:</b> <code>{tp:,.0f} đ</code>")

    if is_blocked and block_msg:
        lines.append(f"\n🛡 <b>Lưu ý Quản trị rủi ro:</b> <i>{block_msg}</i>")

    if applied_floor_note:
        lines.append(
            "💡 <i>Lưu ý quản trị rủi ro: Khoảng cách SL quá sát (&lt;1%), hệ thống áp dụng sàn an toàn 1.0% để phân bổ khối lượng.</i>")

    if view.reasons:
        reasons_text = "\n".join(f"• {_clean_reason(r)}" for r in view.reasons)
        lines.append(f"\n💡 <b>Lý do kích hoạt / Nhận định:</b>\n{reasons_text}")
    elif action == "NO SIGNAL":
        lines.append(
            "\n💡 <b>Lý do:</b> Hệ thống chưa đủ dữ liệu giá nến lịch sử hoặc API chưa phản hồi để phân tích.")

    lines.append(f"\n💡 <i>Mẹo: Gõ <code>/check {symbol}</code> để cập nhật giá realtime từng giây từ sàn DNSE trước khi đặt lệnh.</i>")

    return "\n".join(lines)


# Giữ format_signal tương thích nếu có component khác gọi
format_signal = format_signal_single


def format_watchlist(symbols: list[str]) -> str:
    if not symbols:
        return "Danh sách theo dõi đang trống."
    return "📋 <b>Danh sách theo dõi:</b>\n" + ", ".join(f"<code>{s}</code>" for s in symbols)


def format_portfolio(positions: list[dict[str, Any]], metrics: Any) -> str:
    """Hiển thị vị thế đang mở và giá vốn đã khai báo."""
    lines = [
        "💼 <b>DANH MỤC VỊ THẾ</b>",
        f"Tiền mặt: <code>{float(metrics.cash_balance):,.0f} đ</code>",
        f"NAV: <code>{float(metrics.current_nav):,.0f} đ</code>",
        f"Lãi/lỗ đã thực hiện: <code>{float(metrics.realized_pnl):+,.0f} đ</code>",
        "",
    ]
    if not positions:
        lines.append("Chưa có vị thế mở.")
        return "\n".join(lines)
    for position in positions:
        symbol = html.escape(str(position["symbol"]))
        entry = float(position["entry_price"]) * float(position.get("price_unit_vnd") or 1000)
        last = float(position.get("last_price") or position["entry_price"]) * float(
            position.get("price_unit_vnd") or 1000
        )
        quantity = float(position.get("quantity") or 0)
        pnl = float(position.get("unrealized_pnl") or 0)
        lines.append(
            f"• <b>{symbol}</b>: {quantity:,.0f} cp | Giá vốn "
            f"<code>{entry:,.0f} đ</code> | Hiện tại <code>{last:,.0f} đ</code> "
            f"| U P/L <code>{pnl:+,.0f} đ</code>"
        )
    return "\n".join(lines)


def format_access_denied(chat_id: int) -> str:
    return (
        "⛔️ <b>Truy cập bị từ chối</b>\n\n"
        f"Chat ID của bạn (<code>{chat_id}</code>) chưa được cấp quyền.\n"
        "Vui lòng liên hệ admin để được hỗ trợ."
    )


def format_proactive_signal(payload: Any, decision: Any = None) -> str:
    """Format tín hiệu chủ động từ SignalEvent hoặc payload cũ dạng dict."""

    if not isinstance(payload, dict):
        view = SignalView(
            symbol=str(payload.symbol),
            action=str(payload.action),
            data_status=str(payload.data_status),
            signal_status=str(payload.signal_status),
            as_of=str(payload.data_as_of) if payload.data_as_of else None,
            price=payload.reference_price,
            confidence=payload.confidence,
            stop_loss=payload.stop_loss,
            take_profit=payload.take_profit,
            strategy=f"{payload.strategy} v{payload.strategy_version}",
            reasons=tuple(payload.reasons),
            metrics=dict(payload.component_scores),
            metadata=dict(payload.metadata),
        )
        return format_signal_single(view, decision)

    symbol = payload.get("symbol", "UNKNOWN")
    action = payload.get("action", "")
    price = payload.get("price")
    reasons = payload.get("reasons") or []

    icon = "🟢" if action == "BUY" else "🔴" if action == "SELL" else "🔔"
    lines = [f"{icon} <b>TÍN HIỆU CHỦ ĐỘNG: {symbol}</b> - {action}"]

    if price is not None:
        lines.append(f"Giá: <code>{price:,.0f}</code>")

    if reasons:
        lines.append(f"Lý do: {', '.join(reasons)}")

    return "\n".join(lines)


def format_block(symbol: str, info: dict[str, Any]) -> str:
    """Format tin nhắn phân tích Order Block & Stochastic Oscillator."""
    raw_p = info.get("current_price", 0.0)
    price = raw_p * 1000 if 0 < raw_p < 1000 else raw_p

    slow_k = info.get("slow_k", 50.0)
    slow_d = info.get("slow_d", 50.0)
    stoch_desc = info.get("stoch_desc", "")
    cross_desc = info.get("cross_desc", "")

    bullish_ob = info.get("bullish_ob")
    bearish_ob = info.get("bearish_ob")
    sup_levels = info.get("support_levels", [])
    res_levels = info.get("resistance_levels", [])
    strategy = info.get("strategy", "")

    lines = [
        f"🧱 <b>ORDER BLOCK &amp; STOCHASTIC: {symbol}</b>",
        f"💵 <b>Giá hiện tại:</b> <code>{price:,.2f} đ</code>\n",
        "🌊 <b>Stochastic Oscillator (14, 3, 3):</b>",
        f"• Slow %K: <code>{slow_k:.2f}</code> | Slow %D: <code>{slow_d:.2f}</code>",
        f"• Trạng thái: {stoch_desc}",
        f"• Tín hiệu: {cross_desc}\n",
        "🧱 <b>Vùng Order Block (SMC):</b>",
    ]

    if bullish_ob:
        b_bot = bullish_ob["bottom"] * 1000 if 0 < bullish_ob["bottom"] < 1000 else bullish_ob["bottom"]
        b_top = bullish_ob["top"] * 1000 if 0 < bullish_ob["top"] < 1000 else bullish_ob["top"]
        lines.append(
            f"• 🟢 <b>Bullish OB (Cầu gom):</b> <code>{b_bot:,.2f} - {b_top:,.2f} đ</code> "
            f"<i>(từ {bullish_ob['date']})</i>"
        )
    else:
        lines.append("• 🟢 <b>Bullish OB (Cầu gom):</b> Chưa phát hiện vùng gom rõ nét")

    if bearish_ob:
        b_bot = bearish_ob["bottom"] * 1000 if 0 < bearish_ob["bottom"] < 1000 else bearish_ob["bottom"]
        b_top = bearish_ob["top"] * 1000 if 0 < bearish_ob["top"] < 1000 else bearish_ob["top"]
        lines.append(
            f"• 🔴 <b>Bearish OB (Cung xả):</b> <code>{b_bot:,.2f} - {b_top:,.2f} đ</code> "
            f"<i>(từ {bearish_ob['date']})</i>"
        )
    else:
        lines.append("• 🔴 <b>Bearish OB (Cung xả):</b> Chưa phát hiện vùng xả lớn")

    lines.append("\n🛡 <b>Ngưỡng Kháng cự &amp; Hỗ trợ:</b>")
    if res_levels:
        res_str = " | ".join(f"<code>{r * 1000 if 0 < r < 1000 else r:,.2f} đ</code>" for r in res_levels)
        lines.append(f"• Kháng cự (Cản): {res_str}")
    else:
        lines.append("• Kháng cự: Đang ở vùng đỉnh / Chưa có cản gần")

    if sup_levels:
        sup_str = " | ".join(f"<code>{s * 1000 if 0 < s < 1000 else s:,.2f} đ</code>" for s in sup_levels)
        lines.append(f"• Hỗ trợ (Đáy): {sup_str}")
    else:
        lines.append("• Hỗ trợ: Đang ở vùng đáy / Chưa có hỗ trợ gần")

    if strategy:
        lines.append(f"\n💡 <b>Định hướng hành động:</b>\n<i>{strategy}</i>")

    lines.append("\n📊 <i>Biểu đồ nến kỹ thuật kèm Order Block &amp; Stochastic gửi bên dưới:</i>")
    return "\n".join(lines)
