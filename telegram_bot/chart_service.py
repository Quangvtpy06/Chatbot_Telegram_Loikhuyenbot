"""Vẽ biểu đồ kỹ thuật Bollinger Bands cho Telegram Bot."""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

LOGGER = logging.getLogger(__name__)


def _read_history_csv(csv_path: Path) -> list[dict[str, Any]]:
    """Đọc file history CSV và trả về list of dict đã sắp xếp theo thời gian."""

    rows: list[dict[str, Any]] = []
    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def _parse_ohlcv(rows: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    """Chuyển list of dict thành arrays OHLCV, sắp xếp theo thời gian."""

    dates: list[str] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    volumes: list[float] = []

    for row in rows:
        # Tìm timestamp/date field
        date_val = row.get("t") or row.get("timestamp") or row.get("date") or ""
        close_val = row.get("c") or row.get("close")
        open_val = row.get("o") or row.get("open")
        high_val = row.get("h") or row.get("high")
        low_val = row.get("l") or row.get("low")
        vol_val = row.get("v") or row.get("volume") or "0"

        if close_val is None or close_val == "":
            continue

        try:
            c = float(close_val)
            o = float(open_val) if open_val else c
            h = float(high_val) if high_val else c
            lo = float(low_val) if low_val else c
            v = float(vol_val) if vol_val else 0
        except (TypeError, ValueError):
            continue

        # Chuẩn hóa timestamp
        if date_val:
            try:
                ts = float(date_val)
                # Nếu là unix timestamp
                if ts > 1e9:
                    if ts > 1e12:
                        ts /= 1000
                    date_str = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                else:
                    date_str = str(date_val)
            except (TypeError, ValueError):
                date_str = str(date_val)[:10]
        else:
            date_str = ""

        dates.append(date_str)
        opens.append(o)
        highs.append(h)
        lows.append(lo)
        closes.append(c)
        volumes.append(v)

    # Sắp xếp theo ngày
    if dates:
        combined = sorted(zip(dates, opens, highs, lows, closes, volumes))
        dates, opens, highs, lows, closes, volumes = zip(*combined)

    return {
        "dates": np.array(dates),
        "open": np.array(opens, dtype=float),
        "high": np.array(highs, dtype=float),
        "low": np.array(lows, dtype=float),
        "close": np.array(closes, dtype=float),
        "volume": np.array(volumes, dtype=float),
    }


def _append_realtime_candle(symbol: str, data_dir: Path, data: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Bổ sung nến realtime đang hình thành trong phiên hôm nay vào mảng OHLCV của đồ thị."""
    realtime_file = data_dir / "realtime" / "trades_latest.jsonl"
    if not realtime_file.is_file():
        return data
    try:
        import json
        with open(realtime_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if str(row.get("symbol", "")).upper() == symbol.upper():
                    time_val = str(row.get("time") or row.get("timestamp") or "")
                    trade_date = time_val[:10]
                    if not trade_date:
                        continue
                    dates_list = list(data["dates"])
                    c = float(row.get("matchPrice") or row.get("close") or 0.0)
                    o = float(row.get("openPrice") or row.get("open") or c)
                    h = float(row.get("highestPrice") or row.get("high") or c)
                    lo = float(row.get("lowestPrice") or row.get("low") or c)
                    v = float(row.get("totalVolumeTraded") or row.get("volume") or 0.0)
                    if c <= 0:
                        continue
                    if dates_list and dates_list[-1] == trade_date:
                        data["open"][-1] = o
                        data["high"][-1] = max(data["high"][-1], h)
                        data["low"][-1] = min(data["low"][-1], lo)
                        data["close"][-1] = c
                        data["volume"][-1] = v
                    elif dates_list and trade_date > dates_list[-1]:
                        data["dates"] = np.append(data["dates"], trade_date)
                        data["open"] = np.append(data["open"], o)
                        data["high"] = np.append(data["high"], h)
                        data["low"] = np.append(data["low"], lo)
                        data["close"] = np.append(data["close"], c)
                        data["volume"] = np.append(data["volume"], v)
                    break
    except Exception as exc:
        LOGGER.debug("Không thể nạp nến realtime cho biểu đồ %s: %s", symbol, exc)
    return data


def generate_bollinger_chart(
    symbol: str,
    data_dir: Path,
    days: int = 120,
) -> Path | None:
    """Vẽ biểu đồ Bollinger Bands và trả về đường dẫn file ảnh PNG.

    Args:
        symbol: Mã cổ phiếu (VD: FPT)
        data_dir: Thư mục data gốc chứa history/
        days: Số phiên giao dịch hiển thị trên biểu đồ
    """

    import matplotlib
    matplotlib.use("Agg")  # Không cần GUI
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.patches import FancyBboxPatch

    history_file = data_dir / "history" / f"{symbol.upper()}_1D.csv"
    if not history_file.is_file():
        LOGGER.error("Không tìm thấy file lịch sử: %s", history_file)
        return None

    rows = _read_history_csv(history_file)
    if len(rows) < 25:
        LOGGER.error("Không đủ dữ liệu lịch sử cho %s (%s phiên)", symbol, len(rows))
        return None

    data = _parse_ohlcv(rows)
    data = _append_realtime_candle(symbol, data_dir, data)
    n = len(data["close"])
    if n < 25:
        return None

    # Lấy N phiên gần nhất
    display_start = max(0, n - days)
    close = data["close"][display_start:]
    high = data["high"][display_start:]
    low = data["low"][display_start:]
    open_price = data["open"][display_start:]
    volume = data["volume"][display_start:]
    date_strs = data["dates"][display_start:]

    # Cần tính BB từ dữ liệu đầy đủ rồi cắt
    full_close = data["close"]
    sma_20_full = np.full(n, np.nan)
    upper_full = np.full(n, np.nan)
    lower_full = np.full(n, np.nan)

    for i in range(19, n):
        window = full_close[i - 19:i + 1]
        sma = np.mean(window)
        std = np.std(window, ddof=0)
        sma_20_full[i] = sma
        upper_full[i] = sma + 2 * std
        lower_full[i] = sma - 2 * std

    sma_20 = sma_20_full[display_start:]
    bb_upper = upper_full[display_start:]
    bb_lower = lower_full[display_start:]

    # Parse dates cho matplotlib
    try:
        x_dates = [datetime.strptime(d[:10], "%Y-%m-%d") for d in date_strs]
    except ValueError:
        x_dates = list(range(len(close)))

    # ========== VẼ BIỂU ĐỒ ==========
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.size": 10,
    })

    fig, (ax_price, ax_vol) = plt.subplots(
        2, 1,
        figsize=(14, 8),
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.05},
        facecolor="#1a1a2e",
    )

    # ----- Ax Price -----
    ax_price.set_facecolor("#16213e")

    # Bollinger fill
    valid_mask = ~np.isnan(sma_20)
    if valid_mask.any():
        x_arr = np.array(x_dates) if isinstance(x_dates[0], datetime) else np.array(x_dates)
        ax_price.fill_between(
            x_arr, bb_upper, bb_lower,
            where=valid_mask,
            alpha=0.15,
            color="#00d2ff",
            label="Bollinger Band (2σ)",
        )
        ax_price.plot(x_arr[valid_mask], bb_upper[valid_mask],
                      color="#00d2ff", linewidth=0.8, alpha=0.7)
        ax_price.plot(x_arr[valid_mask], bb_lower[valid_mask],
                      color="#00d2ff", linewidth=0.8, alpha=0.7)
        ax_price.plot(x_arr[valid_mask], sma_20[valid_mask],
                      color="#ffa500", linewidth=1.2, alpha=0.9,
                      label=f"SMA(20): {sma_20[valid_mask][-1]:,.2f}")

    # Candlestick thủ công (tránh phụ thuộc mplfinance)
    num_bars = len(close)
    bar_width = max(0.4, min(0.8, 50 / num_bars))

    for i in range(num_bars):
        x = x_dates[i]
        o, h, lo, c = open_price[i], high[i], low[i], close[i]
        color = "#26a69a" if c >= o else "#ef5350"

        # Thân nến
        body_bottom = min(o, c)
        body_height = abs(c - o) or (h - lo) * 0.01

        if isinstance(x, datetime):
            ax_price.bar(x, body_height, bottom=body_bottom, width=bar_width,
                         color=color, edgecolor=color, linewidth=0.5)
            ax_price.vlines(x, lo, h, color=color, linewidth=0.5)
        else:
            ax_price.bar(x, body_height, bottom=body_bottom, width=bar_width,
                         color=color, edgecolor=color, linewidth=0.5)
            ax_price.vlines(x, lo, h, color=color, linewidth=0.5)

    # Giá trị BB hiện tại
    latest_close = close[-1]
    latest_bb_u = bb_upper[~np.isnan(bb_upper)][-1] if any(~np.isnan(bb_upper)) else None
    latest_bb_l = bb_lower[~np.isnan(bb_lower)][-1] if any(~np.isnan(bb_lower)) else None

    info_text = f"Close: {latest_close:,.2f}"
    if latest_bb_u is not None:
        info_text += f"  |  BB Upper: {latest_bb_u:,.2f}"
    if latest_bb_l is not None:
        info_text += f"  |  BB Lower: {latest_bb_l:,.2f}"

    ax_price.set_title(
        f"{symbol.upper()}  —  Bollinger Bands (20, 2)",
        fontsize=14, fontweight="bold", color="#e0e0e0", pad=12,
    )
    ax_price.legend(loc="upper left", fontsize=9,
                    facecolor="#16213e", edgecolor="#333",
                    labelcolor="#e0e0e0")

    ax_price.text(
        0.99, 0.02, info_text,
        transform=ax_price.transAxes,
        fontsize=9, color="#aaa",
        ha="right", va="bottom",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="#16213e",
                  edgecolor="#444", alpha=0.8),
    )

    from matplotlib.ticker import FormatStrFormatter
    ax_price.yaxis.set_major_formatter(FormatStrFormatter("%.2f"))
    ax_price.tick_params(colors="#888", labelsize=9)
    ax_price.grid(True, alpha=0.15, color="#555")
    ax_price.spines["top"].set_visible(False)
    ax_price.spines["right"].set_visible(False)
    ax_price.spines["left"].set_color("#333")
    ax_price.spines["bottom"].set_color("#333")
    ax_price.set_xticklabels([])

    # ----- Ax Volume -----
    ax_vol.set_facecolor("#16213e")

    vol_colors = ["#26a69a" if close[i] >= open_price[i] else "#ef5350"
                  for i in range(num_bars)]
    ax_vol.bar(x_dates, volume, width=bar_width, color=vol_colors, alpha=0.6)

    ax_vol.set_ylabel("Volume", fontsize=9, color="#888")
    ax_vol.tick_params(colors="#888", labelsize=8)
    ax_vol.grid(True, alpha=0.15, color="#555")
    ax_vol.spines["top"].set_visible(False)
    ax_vol.spines["right"].set_visible(False)
    ax_vol.spines["left"].set_color("#333")
    ax_vol.spines["bottom"].set_color("#333")

    # Format trục X
    if isinstance(x_dates[0], datetime):
        ax_vol.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
        ax_vol.xaxis.set_major_locator(mdates.AutoDateLocator())
        fig.autofmt_xdate(rotation=30, ha="right")

    # Watermark
    fig.text(
        0.5, 0.5, "DNSE Bot",
        fontsize=40, color="#ffffff", alpha=0.03,
        ha="center", va="center",
        transform=fig.transFigure,
    )

    # Lưu file
    output_dir = data_dir / "charts"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{symbol.upper()}_bollinger.png"

    fig.savefig(
        output_path,
        dpi=150,
        bbox_inches="tight",
        facecolor=fig.get_facecolor(),
        edgecolor="none",
    )
    plt.close(fig)

    LOGGER.info("Đã tạo biểu đồ BB: %s", output_path)
    return output_path


def generate_order_block_chart(
    symbol: str,
    data_dir: Path,
    days: int = 90,
) -> tuple[Path | None, dict[str, Any]]:
    """Vẽ biểu đồ Order Block, Kháng cự/Hỗ trợ và Stochastic Oscillator (14, 3, 3).

    Args:
        symbol: Mã cổ phiếu (VD: FPT)
        data_dir: Thư mục data gốc chứa history/
        days: Số phiên giao dịch hiển thị trên biểu đồ

    Returns:
        tuple (đường_dẫn_file_ảnh, dict_thông_tin_phân_tích)
    """

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import FormatStrFormatter

    history_file = data_dir / "history" / f"{symbol.upper()}_1D.csv"
    if not history_file.is_file():
        LOGGER.error("Không tìm thấy file lịch sử: %s", history_file)
        return None, {}

    rows = _read_history_csv(history_file)
    if len(rows) < 30:
        LOGGER.error("Không đủ dữ liệu lịch sử cho %s (%s phiên)", symbol, len(rows))
        return None, {}

    data = _parse_ohlcv(rows)
    data = _append_realtime_candle(symbol, data_dir, data)
    n_total = len(data["close"])
    if n_total < 30:
        return None, {}

    full_close = data["close"]
    full_open = data["open"]
    full_high = data["high"]
    full_low = data["low"]
    full_dates = data["dates"]

    # 1. Tính Stochastic Oscillator (14, 3, 3) trên toàn bộ mảng
    fast_k = np.full(n_total, np.nan)
    for i in range(13, n_total):
        l14 = np.min(full_low[i - 13 : i + 1])
        h14 = np.max(full_high[i - 13 : i + 1])
        if h14 > l14:
            fast_k[i] = ((full_close[i] - l14) / (h14 - l14)) * 100.0
        else:
            fast_k[i] = 50.0

    slow_k = np.full(n_total, np.nan)
    for i in range(15, n_total):
        w = fast_k[i - 2 : i + 1]
        if not np.isnan(w).any():
            slow_k[i] = float(np.mean(w))

    slow_d = np.full(n_total, np.nan)
    for i in range(17, n_total):
        w = slow_k[i - 2 : i + 1]
        if not np.isnan(w).any():
            slow_d[i] = float(np.mean(w))

    # 2. Cắt dữ liệu hiển thị N phiên gần nhất
    display_start = max(0, n_total - days)
    c = full_close[display_start:]
    o = full_open[display_start:]
    h = full_high[display_start:]
    lo = full_low[display_start:]
    d_strs = full_dates[display_start:]
    k_vals = slow_k[display_start:]
    d_vals = slow_d[display_start:]
    num_bars = len(c)

    latest_close = float(c[-1])
    latest_k = float(k_vals[-1]) if not np.isnan(k_vals[-1]) else 50.0
    latest_d = float(d_vals[-1]) if not np.isnan(d_vals[-1]) else 50.0
    prev_k = float(k_vals[-2]) if num_bars >= 2 and not np.isnan(k_vals[-2]) else latest_k
    prev_d = float(d_vals[-2]) if num_bars >= 2 and not np.isnan(d_vals[-2]) else latest_d

    # 3. Xác định các Swing Highs & Swing Lows (Kháng cự / Hỗ trợ)
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []

    for i in range(2, num_bars - 2):
        if h[i] >= h[i - 1] and h[i] >= h[i - 2] and h[i] >= h[i + 1] and h[i] >= h[i + 2]:
            swing_highs.append((i, float(h[i])))
        if lo[i] <= lo[i - 1] and lo[i] <= lo[i - 2] and lo[i] <= lo[i + 1] and lo[i] <= lo[i + 2]:
            swing_lows.append((i, float(lo[i])))

    # Gom cụm các mức kháng cự (trên giá hiện tại)
    res_raw = sorted([val for _, val in swing_highs if val > latest_close * 1.003])
    resistance_levels: list[float] = []
    for r in res_raw:
        if not resistance_levels or (r - resistance_levels[-1]) / resistance_levels[-1] > 0.015:
            resistance_levels.append(round(r, 2))
    resistance_levels = resistance_levels[:3]  # 3 mức kháng cự gần nhất

    # Gom cụm các mức hỗ trợ (dưới giá hiện tại)
    sup_raw = sorted([val for _, val in swing_lows if val < latest_close * 0.997], reverse=True)
    support_levels: list[float] = []
    for s in sup_raw:
        if not support_levels or (support_levels[-1] - s) / support_levels[-1] > 0.015:
            support_levels.append(round(s, 2))
    support_levels = support_levels[:3]  # 3 mức hỗ trợ gần nhất

    # 4. Xác định Order Block (Bullish OB & Bearish OB)
    bullish_ob: dict[str, Any] | None = None
    bearish_ob: dict[str, Any] | None = None

    # Tìm Bullish OB: cây nến giảm cuối cùng trước đợt tăng mạnh phá đỉnh nến đó
    for i in range(num_bars - 4, 3, -1):
        if c[i] < o[i]:  # Nến giảm
            # Kiểm tra sau đó có nhịp tăng vượt đỉnh nến này không
            future_highs = h[i + 1 : min(num_bars, i + 5)]
            if len(future_highs) > 0 and np.max(future_highs) > h[i] * 1.01:
                # Kiểm tra đáy nến chưa bị xuyên thủng bởi các phiên sau
                subsequent_lows = lo[i + 1 :]
                if len(subsequent_lows) == 0 or np.min(subsequent_lows) >= lo[i] * 0.992:
                    top_box = float(max(o[i], c[i]))
                    bottom_box = float(lo[i])
                    bullish_ob = {
                        "bar_idx": i,
                        "date": d_strs[i],
                        "top": round(top_box, 2),
                        "bottom": round(bottom_box, 2),
                    }
                    break

    # Tìm Bearish OB: cây nến tăng cuối cùng trước đợt giảm mạnh phá đáy nến đó
    for i in range(num_bars - 4, 3, -1):
        if c[i] > o[i]:  # Nến tăng
            future_lows = lo[i + 1 : min(num_bars, i + 5)]
            if len(future_lows) > 0 and np.min(future_lows) < lo[i] * 0.99:
                subsequent_highs = h[i + 1 :]
                if len(subsequent_highs) == 0 or np.max(subsequent_highs) <= h[i] * 1.008:
                    top_box = float(h[i])
                    bottom_box = float(min(o[i], c[i]))
                    bearish_ob = {
                        "bar_idx": i,
                        "date": d_strs[i],
                        "top": round(top_box, 2),
                        "bottom": round(bottom_box, 2),
                    }
                    break

    # 5. Phân tích Stochastic
    if latest_k >= 80:
        stoch_state = "OVERBOUGHT"
        stoch_desc = "🔴 Quá mua (&gt;80) — Rủi ro điều chỉnh ngắn hạn"
    elif latest_k <= 20:
        stoch_state = "OVERSOLD"
        stoch_desc = "🟢 Quá bán (&lt;20) — Vùng bắt đáy tiềm năng"
    else:
        stoch_state = "NEUTRAL"
        stoch_desc = "🟡 Vùng cân bằng (20 - 80)"

    stoch_cross = "NONE"
    if prev_k <= prev_d and latest_k > latest_d:
        stoch_cross = "GOLDEN_CROSS"
        cross_desc = "🔥 %K vừa cắt lên %D (Golden Cross — Tín hiệu đảo chiều tăng)"
    elif prev_k >= prev_d and latest_k < latest_d:
        stoch_cross = "DEATH_CROSS"
        cross_desc = "⚠️ %K vừa cắt xuống %D (Death Cross — Áp lực bán gia tăng)"
    else:
        cross_desc = "➡️ %K và %D tiếp diễn xu hướng hiện tại"

    # Đề xuất chiến lược
    strategy_points: list[str] = []
    if stoch_state == "OVERSOLD" or (stoch_cross == "GOLDEN_CROSS" and latest_k < 40):
        strategy_points.append("Stochastic đang ở vùng quá bán hoặc xuất hiện giao cắt tăng, mở ra cơ hội bắt đáy ngắn hạn.")
    elif stoch_state == "OVERBOUGHT" or (stoch_cross == "DEATH_CROSS" and latest_k > 65):
        strategy_points.append("Stochastic đang ở vùng quá mua hoặc xuất hiện giao cắt giảm, nên cân nhắc chốt lời bớt từng phần.")

    if bullish_ob and bullish_ob["bottom"] <= latest_close <= bullish_ob["top"] * 1.015:
        strategy_points.append(f"Giá đang nằm trong/sát vùng Bullish Order Block ({bullish_ob['bottom']:.2f} - {bullish_ob['top']:.2f}). Đây là vùng cầu hỗ trợ mạnh.")
    elif bearish_ob and bearish_ob["bottom"] * 0.985 <= latest_close <= bearish_ob["top"]:
        strategy_points.append(f"Giá đang tiệm cận vùng Bearish Order Block ({bearish_ob['bottom']:.2f} - {bearish_ob['top']:.2f}). Cần đề phòng lực cung xả mạnh.")

    if not strategy_points:
        strategy_points.append("Giá đang vận động tích lũy giữa các mốc cản và hỗ trợ. Kiên nhẫn chờ điểm chạm OB hoặc tín hiệu đảo chiều rõ nét.")

    info_dict: dict[str, Any] = {
        "current_price": latest_close,
        "slow_k": latest_k,
        "slow_d": latest_d,
        "stoch_state": stoch_state,
        "stoch_desc": stoch_desc,
        "stoch_cross": stoch_cross,
        "cross_desc": cross_desc,
        "support_levels": support_levels,
        "resistance_levels": resistance_levels,
        "bullish_ob": bullish_ob,
        "bearish_ob": bearish_ob,
        "strategy": " ".join(strategy_points),
    }

    # 6. Vẽ Biểu đồ 2 Subplots
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.size": 10,
    })

    fig, (ax_price, ax_stoch) = plt.subplots(
        2, 1,
        figsize=(14, 9),
        gridspec_kw={"height_ratios": [3.2, 1.2], "hspace": 0.08},
        facecolor="#1a1a2e",
    )

    # ----- Ax Price -----
    ax_price.set_facecolor("#16213e")
    indices = np.arange(num_bars)
    bar_width = max(0.4, min(0.75, 45 / num_bars))

    # Nến Candlestick
    for i in range(num_bars):
        open_val, high_val, low_val, close_val = o[i], h[i], lo[i], c[i]
        is_bull = close_val >= open_val
        candle_color = "#26a69a" if is_bull else "#ef5350"

        body_bottom = min(open_val, close_val)
        body_height = max(abs(close_val - open_val), (high_val - low_val) * 0.02)

        ax_price.bar(
            i, body_height, bottom=body_bottom,
            width=bar_width, color=candle_color, edgecolor=candle_color, linewidth=0.6,
        )
        ax_price.vlines(i, low_val, high_val, color=candle_color, linewidth=0.7)

    # Vẽ Bullish Order Block
    if bullish_ob:
        b_idx = bullish_ob["bar_idx"]
        b_top = bullish_ob["top"]
        b_bot = bullish_ob["bottom"]
        rect_w = num_bars - b_idx + 0.5
        rect = Rectangle(
            (b_idx - 0.5, b_bot),
            rect_w,
            b_top - b_bot,
            facecolor="#00e676",
            edgecolor="#00e676",
            linestyle="--",
            linewidth=1.0,
            alpha=0.22,
        )
        ax_price.add_patch(rect)
        ax_price.text(
            num_bars - 0.5, (b_top + b_bot) / 2,
            f" Bullish OB [{b_bot:.2f} - {b_top:.2f}]",
            color="#00e676", fontsize=8.5, va="center", fontweight="bold",
        )

    # Vẽ Bearish Order Block
    if bearish_ob:
        b_idx = bearish_ob["bar_idx"]
        b_top = bearish_ob["top"]
        b_bot = bearish_ob["bottom"]
        rect_w = num_bars - b_idx + 0.5
        rect = Rectangle(
            (b_idx - 0.5, b_bot),
            rect_w,
            b_top - b_bot,
            facecolor="#ff5252",
            edgecolor="#ff5252",
            linestyle="--",
            linewidth=1.0,
            alpha=0.22,
        )
        ax_price.add_patch(rect)
        ax_price.text(
            num_bars - 0.5, (b_top + b_bot) / 2,
            f" Bearish OB [{b_bot:.2f} - {b_top:.2f}]",
            color="#ff5252", fontsize=8.5, va="center", fontweight="bold",
        )

    # Vẽ Kháng cự & Hỗ trợ
    for r in resistance_levels:
        ax_price.axhline(r, color="#ffa726", linestyle=":", linewidth=1.0, alpha=0.75)
        ax_price.text(
            0.5, r, f"Kháng cự: {r:,.2f}",
            color="#ffa726", fontsize=8, va="bottom", ha="left",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="#16213e", edgecolor="#ffa726", alpha=0.6),
        )

    for s in support_levels:
        ax_price.axhline(s, color="#29b6f6", linestyle=":", linewidth=1.0, alpha=0.75)
        ax_price.text(
            0.5, s, f"Hỗ trợ: {s:,.2f}",
            color="#29b6f6", fontsize=8, va="top", ha="left",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="#16213e", edgecolor="#29b6f6", alpha=0.6),
        )

    ax_price.set_title(
        f"{symbol.upper()}  —  ORDER BLOCK & KHÁNG CỰ / HỖ TRỢ",
        fontsize=14, fontweight="bold", color="#e0e0e0", pad=12,
    )
    ax_price.set_xlim(-0.8, num_bars + 12)
    ax_price.yaxis.set_major_formatter(FormatStrFormatter("%.2f"))
    ax_price.tick_params(colors="#888", labelsize=9)
    ax_price.grid(True, alpha=0.15, color="#555")
    ax_price.spines["top"].set_visible(False)
    ax_price.spines["right"].set_visible(False)
    ax_price.spines["left"].set_color("#333")
    ax_price.spines["bottom"].set_color("#333")
    ax_price.set_xticks([])

    # ----- Ax Stochastic -----
    ax_stoch.set_facecolor("#16213e")
    ax_stoch.plot(indices, k_vals, color="#00d2ff", linewidth=1.5, label=f"Slow %K (14,3): {latest_k:.2f}")
    ax_stoch.plot(indices, d_vals, color="#ff9100", linewidth=1.3, linestyle="--", label=f"Slow %D (3): {latest_d:.2f}")

    # Vùng Quá mua & Quá bán
    ax_stoch.axhline(80, color="#ef5350", linestyle="--", linewidth=0.9, alpha=0.8)
    ax_stoch.axhline(50, color="#777777", linestyle=":", linewidth=0.8, alpha=0.5)
    ax_stoch.axhline(20, color="#26a69a", linestyle="--", linewidth=0.9, alpha=0.8)

    ax_stoch.fill_between(indices, 80, 100, color="#ef5350", alpha=0.12)
    ax_stoch.fill_between(indices, 0, 20, color="#26a69a", alpha=0.12)

    ax_stoch.text(num_bars + 0.5, 85, "Quá Mua (80)", color="#ef5350", fontsize=8, va="center")
    ax_stoch.text(num_bars + 0.5, 15, "Quá Bán (20)", color="#26a69a", fontsize=8, va="center")

    ax_stoch.set_title(
        "Stochastic Oscillator (14, 3, 3) — Bắt Đỉnh & Đáy Ngắn Hạn",
        fontsize=11, fontweight="bold", color="#e0e0e0", pad=6, loc="left",
    )
    ax_stoch.set_ylim(-2, 102)
    ax_stoch.set_yticks([20, 50, 80])
    ax_stoch.set_xlim(-0.8, num_bars + 12)
    ax_stoch.tick_params(colors="#888", labelsize=8.5)
    ax_stoch.grid(True, alpha=0.15, color="#555")
    ax_stoch.spines["top"].set_visible(False)
    ax_stoch.spines["right"].set_visible(False)
    ax_stoch.spines["left"].set_color("#333")
    ax_stoch.spines["bottom"].set_color("#333")

    ax_stoch.legend(loc="upper left", fontsize=8.5, facecolor="#16213e", edgecolor="#333", labelcolor="#e0e0e0")

    # Format ngày trục X
    tick_step = max(1, num_bars // 8)
    tick_positions = list(range(0, num_bars, tick_step))
    if tick_positions[-1] != num_bars - 1:
        tick_positions.append(num_bars - 1)

    tick_labels = [str(d_strs[idx])[-5:] if idx < len(d_strs) else "" for idx in tick_positions]
    ax_stoch.set_xticks(tick_positions)
    ax_stoch.set_xticklabels(tick_labels, rotation=30, ha="right", color="#aaa", fontsize=8.5)

    # Watermark
    fig.text(
        0.5, 0.5, "FINSHIELD — Order Block",
        fontsize=38, color="#ffffff", alpha=0.03,
        ha="center", va="center",
        transform=fig.transFigure,
    )

    # Lưu file
    output_dir = data_dir / "charts"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{symbol.upper()}_block.png"

    fig.savefig(
        output_path,
        dpi=150,
        bbox_inches="tight",
        facecolor=fig.get_facecolor(),
        edgecolor="none",
    )
    plt.close(fig)

    LOGGER.info("Đã tạo biểu đồ Order Block: %s", output_path)
    return output_path, info_dict

