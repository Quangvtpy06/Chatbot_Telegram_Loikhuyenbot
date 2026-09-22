"""Ghép dữ liệu lịch sử thật (giá, chỉ báo, thị trường, cơ bản, khối ngoại)
thành danh sách "rows" đúng định dạng mà backtesting.engine.run_backtest() cần.

CẬP NHẬT theo signals/strategies.py bản strategy_spec 1.5.0 / QualityTrendStrategy
version 1.6.0 (đã đọc & đối chiếu toàn bộ diff so với bản trước):

  1. Chiến lược giờ sinh 4 loại tín hiệu: BUY / SELL / HOLD / NO SIGNAL (trước
     chỉ có BUY / NO SIGNAL). HOLD = "chưa đủ điều kiện mua, chưa có tín hiệu
     bán tháo".
  2. Cổng vào lệnh (entry gate) được NỚI LỎNG: thiếu VNINDEX hoặc thiếu đủ 5
     phiên khối ngoại KHÔNG còn chặn cứng như trước — các điều kiện đó chỉ bị
     bỏ qua nếu thiếu dữ liệu, không chặn toàn bộ tín hiệu nữa. -> không còn
     cần hack "--ignore-foreign-gate" như phiên bản cũ.
  3. Thêm 3 chỉ báo mới bắt buộc để chiến lược hoạt động đầy đủ: Bollinger
     Bands (bollinger_upper_20 / bollinger_lower_20), Stochastic %K/%D
     (stochastic_k_14 / stochastic_d_3), và Order Block hỗ trợ/kháng cự
     (ob_support / ob_resistance) — tái tạo lại đúng
     AnalyticsPipeline._detect_order_blocks nhưng tính CHO TỪNG NGÀY (rolling
     60 phiên tính đến đúng ngày đó, không nhìn trước tương lai).
  4. Hỗ trợ 2 chế độ SL/TP: FIXED (% cố định, mặc định) và STRUCTURE (theo
     Order Block/Bollinger) — set qua snapshot["sl_tp_mode"].
  5. ROE TTM tính lại ĐÚNG công thức chính thức trong analytics.py:
     tổng net_profit 4 quý liên tiếp / vốn chủ bình quân đầu-cuối kỳ — thay vì
     lấy tạm cột "roe" như bản trước (file ratio.csv bản mới có sẵn cột
     published_date, net_profit, owners_equity nên không cần merge với
     income.csv nữa).
"""

from __future__ import annotations

import math
import re
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# Các cảnh báo dưới đây VÔ HẠI (không ảnh hưởng kết quả, đã kiểm chứng số
# liệu ra đúng) — chỉ là pandas/numpy cảnh báo về hành vi nội bộ khi tính
# trung bình/rolling trên vùng dữ liệu còn thiếu ở đầu chuỗi (trước khi đủ
# min_periods) hoặc downcast kiểu dữ liệu trong .fillna(). Ẩn đi cho log gọn.
warnings.filterwarnings("ignore", message="Mean of empty slice")
warnings.filterwarnings("ignore", category=FutureWarning, message=".*Downcasting object dtype arrays.*")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


# ---------------------------------------------------------------------------
# 1. Giá + chỉ báo kỹ thuật (full rolling series, không chỉ dòng cuối)
# ---------------------------------------------------------------------------

def _load_ohlcv(symbol: str, history_dir: Path) -> pd.DataFrame:
    path = history_dir / f"{symbol.upper()}_1D.csv"
    if not path.exists():
        raise FileNotFoundError(f"Không tìm thấy lịch sử giá: {path}")
    df = pd.read_csv(path)
    df = df.rename(columns={"c": "close", "h": "high", "l": "low", "o": "open", "v": "volume", "t": "timestamp"})
    df["date"] = (
        pd.to_datetime(df["timestamp"], unit="s", utc=True)
        .dt.tz_convert("Asia/Ho_Chi_Minh")
        .dt.date.astype(str)
    )
    df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


def _detect_order_blocks_asof(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, open_p: np.ndarray, window: int = 60
) -> tuple[float | None, float | None]:
    """Tái tạo NGUYÊN VĂN AnalyticsPipeline._detect_order_blocks (analytics.py),
    nhận mảng ĐÃ CẮT tới đúng ngày cần tính (không nhìn trước tương lai)."""

    n = len(close)
    if n < 20:
        return None, None
    latest_c = float(close[-1])
    start_idx = max(0, n - window)
    h_w = high[start_idx:]
    lo_w = low[start_idx:]
    c_w = close[start_idx:]
    o_w = open_p[start_idx:]
    num_w = len(c_w)

    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []
    for i in range(2, num_w - 2):
        if h_w[i] >= h_w[i - 1] and h_w[i] >= h_w[i - 2] and h_w[i] >= h_w[i + 1] and h_w[i] >= h_w[i + 2]:
            swing_highs.append((i, float(h_w[i])))
        if lo_w[i] <= lo_w[i - 1] and lo_w[i] <= lo_w[i - 2] and lo_w[i] <= lo_w[i + 1] and lo_w[i] <= lo_w[i + 2]:
            swing_lows.append((i, float(lo_w[i])))

    bullish_ob_low: float | None = None
    for sh_idx, _ in reversed(swing_highs):
        for j in range(sh_idx - 1, max(0, sh_idx - 6), -1):
            if c_w[j] < o_w[j]:
                cand_low = float(lo_w[j])
                if cand_low < latest_c:
                    bullish_ob_low = cand_low
                    break
        if bullish_ob_low is not None:
            break
    ob_support = bullish_ob_low
    if ob_support is None:
        valid_lows = [sl_val for _, sl_val in swing_lows if sl_val < latest_c * 0.995]
        if valid_lows:
            ob_support = max(valid_lows)

    bearish_ob_high: float | None = None
    for sl_idx, _ in reversed(swing_lows):
        for j in range(sl_idx - 1, max(0, sl_idx - 6), -1):
            if c_w[j] > o_w[j]:
                cand_high = float(h_w[j])
                if cand_high > latest_c:
                    bearish_ob_high = cand_high
                    break
        if bearish_ob_high is not None:
            break
    ob_resistance = bearish_ob_high
    if ob_resistance is None:
        valid_highs = [sh_val for _, sh_val in swing_highs if sh_val > latest_c * 1.005]
        if valid_highs:
            ob_resistance = min(valid_highs)

    res_support = ob_support if (ob_support is not None and 0 < ob_support < latest_c) else None
    res_resistance = ob_resistance if (ob_resistance is not None and ob_resistance > latest_c) else None
    return res_support, res_resistance


def _rolling_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Tính lại đúng công thức của AnalyticsPipeline._analyze_price_history,
    nhưng giữ lại TOÀN BỘ chuỗi (mỗi ngày một giá trị) thay vì chỉ .iloc[-1]."""

    out = df.copy()
    close = out["close"].astype(float)
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    volume = out["volume"].astype(float)

    out["sma_10"] = close.rolling(10, min_periods=10).mean()
    sma_20 = close.rolling(20, min_periods=20).mean()
    sma_50 = close.rolling(50, min_periods=50).mean()
    out["sma_20"] = sma_20
    out["sma_50"] = sma_50
    out["sma_200"] = close.rolling(200, min_periods=200).mean()
    ema_12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema_26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    out["ema_12"] = ema_12
    out["ema_20"] = close.ewm(span=20, adjust=False, min_periods=20).mean()
    out["ema_26"] = ema_26
    out["ema_50"] = close.ewm(span=50, adjust=False, min_periods=50).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    rsi = rsi.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    out["rsi_14"] = rsi

    macd = ema_12 - ema_26
    macd_signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    out["macd"] = macd
    out["macd_signal"] = macd_signal
    out["macd_histogram"] = macd - macd_signal

    # --- Bollinger Bands (20, 2 std) ---
    rolling_std_20 = close.rolling(20, min_periods=20).std(ddof=0)
    out["bollinger_middle_20"] = sma_20
    out["bollinger_upper_20"] = sma_20 + 2 * rolling_std_20
    out["bollinger_lower_20"] = sma_20 - 2 * rolling_std_20
    # Dùng cho bộ lọc "bull-trap"/"false breakdown" mới trong strategies.py:
    # bề rộng dải Bollinger tương đối so với SMA20 (0.08 = 8%).
    out["bollinger_bandwidth_20"] = (
        (out["bollinger_upper_20"] - out["bollinger_lower_20"]) / sma_20.replace(0, np.nan)
    )

    # --- ATR14 (dùng cho tham khảo, không bắt buộc ở entry gate mới) ---
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    out["atr_14"] = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    # --- Stochastic %K/%D (14, 3) ---
    lowest_14 = low.rolling(14, min_periods=14).min()
    highest_14 = high.rolling(14, min_periods=14).max()
    stochastic_k = 100 * (close - lowest_14) / (highest_14 - lowest_14).replace(0, np.nan)
    out["stochastic_k_14"] = stochastic_k
    out["stochastic_d_3"] = stochastic_k.rolling(3, min_periods=3).mean()

    avg_volume_20 = volume.rolling(20, min_periods=5).mean()
    out["average_volume_20d"] = avg_volume_20
    # Dùng cho bộ lọc "xác nhận khối lượng" mới (chặn BUY nếu < 0.8x bình quân).
    out["volume_ratio_20d"] = volume / avg_volume_20.replace(0, np.nan)

    # --- Order Block hỗ trợ/kháng cự: tính CHO TỪNG NGÀY, chỉ dùng dữ liệu
    # đến đúng ngày đó (rolling as-of, không lookahead). ---
    h_arr = high.to_numpy(dtype=float)
    lo_arr = low.to_numpy(dtype=float)
    c_arr = close.to_numpy(dtype=float)
    o_arr = out["open"].astype(float).to_numpy(dtype=float)
    ob_support_list: list[float | None] = []
    ob_resistance_list: list[float | None] = []
    for i in range(len(out)):
        sup, res = _detect_order_blocks_asof(h_arr[: i + 1], lo_arr[: i + 1], c_arr[: i + 1], o_arr[: i + 1])
        ob_support_list.append(sup)
        ob_resistance_list.append(res)
    out["ob_support"] = ob_support_list
    out["ob_resistance"] = ob_resistance_list

    required = ["rsi_14", "macd_signal", "sma_20", "sma_50"]
    out["indicator_data_complete"] = out[required].notna().all(axis=1)
    return out


# ---------------------------------------------------------------------------
# 2. Ngữ cảnh thị trường (VNINDEX, fallback VN30)
# ---------------------------------------------------------------------------

def _load_market(history_dir: Path) -> pd.DataFrame:
    frames = []
    for symbol in ("VNINDEX", "VN30"):
        path = history_dir / f"{symbol}_1D.csv"
        if path.exists():
            df = _load_ohlcv(symbol, history_dir)
            df["sma_20"] = df["close"].rolling(20, min_periods=20).mean()
            df["sma_50"] = df["close"].rolling(50, min_periods=50).mean()
            df["sma_200"] = df["close"].rolling(200, min_periods=200).mean()
            df = df.rename(
                columns={
                    "close": "market_close",
                    "sma_20": "market_sma_20",
                    "sma_50": "market_sma_50",
                    "sma_200": "market_sma_200",
                }
            )
            df["market_symbol"] = symbol
            frames.append(df[["date", "market_close", "market_sma_20", "market_sma_50", "market_sma_200", "market_symbol"]])
    if not frames:
        raise FileNotFoundError("Không tìm thấy lịch sử VNINDEX/VN30")
    merged = frames[0]
    for extra in frames[1:]:
        merged = merged.set_index("date").combine_first(extra.set_index("date")).reset_index()
    return merged.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 3. Cơ bản: ROE TTM đúng công thức 4 quý + D/E theo quý, forward-fill theo
#    published_date (giờ có sẵn trong chính file ratio.csv, không cần merge
#    với income.csv như bản cũ nữa).
# ---------------------------------------------------------------------------

_PERIOD_RE = re.compile(r"^(\d{4})-Q([1-4])$")

_QUARTER_END_MONTH_DAY = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def _period_end(period: str) -> pd.Timestamp | None:
    """Tái tạo AnalyticsPipeline._period_end cho kỳ dạng 'YYYY-Qn'."""

    match = _PERIOD_RE.match(str(period).strip())
    if not match:
        return None
    year, q = int(match.group(1)), int(match.group(2))
    month, day = _QUARTER_END_MONTH_DAY[q]
    return pd.Timestamp(year=year, month=month, day=day)


def _estimate_missing_published_dates(quarter: pd.DataFrame) -> pd.DataFrame:
    """Với các dòng thiếu published_date (một số nguồn dữ liệu để trống hẳn
    cột này), ước lượng = ngày kết thúc kỳ báo cáo + 35 ngày — đúng cách
    AnalyticsPipeline._build_symbol_quality xử lý ở bản mới, để không phải bỏ
    hẳn các mã bị thiếu (vd. VIC, ACB, MBB, VNM trong dữ liệu thật đã crawl)."""

    quarter = quarter.copy()
    missing = quarter["published_date"].isna() | (quarter["published_date"].astype(str) == "NaT")
    quarter["published_date_estimated"] = missing
    if missing.any():
        estimated = quarter.loc[missing, "period"].map(_period_end)
        estimated = (estimated + pd.Timedelta(days=35)).dt.date.astype(str)
        quarter.loc[missing, "published_date"] = estimated
    return quarter


def _calculate_roe_ttm_series(quarter: pd.DataFrame) -> pd.DataFrame:
    """Tái tạo AnalyticsPipeline._calculate_roe_ttm nhưng trả về ROE TTM cho
    TỪNG quý có đủ dữ liệu (không chỉ quý mới nhất). ``quarter`` phải có sẵn
    period, published_date, net_profit (từ income.csv), owners_equity (từ
    balance.csv) — xem _load_fundamentals()."""

    quarter = quarter.copy()
    required = {"period", "net_profit", "owners_equity"}
    if quarter.empty or not required.issubset(quarter.columns):
        return pd.DataFrame(columns=["published_date", "roe_ttm"])

    parsed = quarter["period"].astype(str).str.extract(_PERIOD_RE)
    quarter["_year"] = pd.to_numeric(parsed[0], errors="coerce")
    quarter["_q"] = pd.to_numeric(parsed[1], errors="coerce")
    quarter["_ordinal"] = quarter["_year"] * 4 + quarter["_q"] - 1
    quarter["net_profit"] = pd.to_numeric(quarter["net_profit"], errors="coerce")
    quarter["owners_equity"] = pd.to_numeric(quarter["owners_equity"], errors="coerce")
    quarter = quarter.dropna(subset=["_ordinal"]).drop_duplicates("_ordinal", keep="last")
    quarter = quarter.sort_values("_ordinal").set_index("_ordinal", drop=False)

    records = []
    for latest_ordinal in quarter["_ordinal"]:
        latest_ordinal = int(latest_ordinal)
        ttm_ordinals = list(range(latest_ordinal - 3, latest_ordinal + 1))
        beginning_ordinal = latest_ordinal - 4
        if not all(o in quarter.index for o in ttm_ordinals):
            continue
        ttm_rows = quarter.loc[ttm_ordinals]
        net_profit = ttm_rows["net_profit"]
        if beginning_ordinal in quarter.index and pd.notna(quarter.at[beginning_ordinal, "owners_equity"]):
            beginning_equity = quarter.at[beginning_ordinal, "owners_equity"]
        else:
            beginning_equity = ttm_rows.iloc[0]["owners_equity"]
        ending_equity = quarter.at[latest_ordinal, "owners_equity"]
        if net_profit.isna().any() or pd.isna(beginning_equity) or pd.isna(ending_equity):
            continue
        average_equity = (float(beginning_equity) + float(ending_equity)) / 2
        if average_equity <= 0:
            continue
        published_date = quarter.at[latest_ordinal, "published_date"]
        records.append(
            {
                "published_date": published_date,
                "roe_ttm": float(net_profit.sum()) / average_equity,
            }
        )
    return pd.DataFrame(records, columns=["published_date", "roe_ttm"])


def _load_fundamentals(symbol: str, fundamental_dir: Path) -> pd.DataFrame:
    """Đọc đúng file *_fundamentals.csv — file DUY NHẤT mà chính
    AnalyticsPipeline thật sự đọc (glob '*/*_fundamentals.csv'), đã gộp sẵn
    net_profit/owners_equity/roe/debt_to_equity/published_date đầy đủ, sạch,
    đúng đơn vị (phân số) cho mọi mã — không cần merge ratio/income/balance
    riêng lẻ hay đoán đơn vị như trước nữa."""

    path = fundamental_dir / "quarter" / f"{symbol.upper()}_fundamentals.csv"
    if not path.exists():
        return pd.DataFrame(columns=["published_date", "roe_ttm", "debt_to_equity", "pe", "pb", "published_date_estimated"])

    df = pd.read_csv(path)
    df["published_date"] = pd.to_datetime(df["published_date"], errors="coerce", utc=True).dt.date.astype(str)
    df.loc[df["published_date"] == "NaT", "published_date"] = pd.NA
    quarter = df[df["period_type"] == "quarter"].copy()
    quarter = _estimate_missing_published_dates(quarter)
    quarter = quarter.dropna(subset=["published_date"])

    roe_ttm_series = _calculate_roe_ttm_series(quarter)

    for optional_col in ("pe_ratio", "pb_ratio"):
        if optional_col not in quarter.columns:
            quarter[optional_col] = np.nan

    quarter = quarter[
        ["published_date", "published_date_estimated", "debt_to_equity", "roe", "pe_ratio", "pb_ratio"]
    ].sort_values("published_date")
    merged = quarter.merge(roe_ttm_series, on="published_date", how="left")
    merged["roe_ttm"] = pd.to_numeric(merged["roe_ttm"], errors="coerce")
    # roe_ttm đúng công thức 4-quý khi tính được (giống hệt
    # AnalyticsPipeline._calculate_roe_ttm); nếu quý đó chưa đủ 4 quý liên
    # tiếp (vd. công ty mới niêm yết) thì rơi về roe quý đơn lẻ của chính
    # file này (đã đúng đơn vị phân số sẵn, không cần quy đổi %).
    merged["roe_ttm"] = merged["roe_ttm"].fillna(pd.to_numeric(merged["roe"], errors="coerce")).astype(float)
    merged["debt_to_equity"] = pd.to_numeric(merged["debt_to_equity"], errors="coerce")
    merged["pe"] = pd.to_numeric(merged["pe_ratio"], errors="coerce")
    merged["pb"] = pd.to_numeric(merged["pb_ratio"], errors="coerce")
    merged = merged.sort_values("published_date")
    return merged[["published_date", "roe_ttm", "debt_to_equity", "pe", "pb", "published_date_estimated"]]


def _asof_fundamentals(dates: pd.Series, fundamentals: pd.DataFrame) -> pd.DataFrame:
    left = pd.DataFrame({"date": pd.to_datetime(dates).astype("datetime64[ns]")})
    right = fundamentals.copy()
    right["published_date"] = pd.to_datetime(right["published_date"]).astype("datetime64[ns]")
    merged = pd.merge_asof(
        left.sort_values("date"),
        right.sort_values("published_date"),
        left_on="date",
        right_on="published_date",
        direction="backward",
    )
    cols = ["roe_ttm", "debt_to_equity", "pe", "pb", "published_date_estimated"]
    for col in cols:
        if col not in merged.columns:
            merged[col] = np.nan
    return merged[cols]


# ---------------------------------------------------------------------------
# 4. Khối ngoại — dùng đúng dữ liệu crawl được, KHÔNG bịa thêm
# ---------------------------------------------------------------------------

def _load_foreign_flow(symbol: str, foreign_dir: Path) -> pd.DataFrame:
    path = foreign_dir / "foreign_flow_history.csv"
    if not path.exists():
        return pd.DataFrame(columns=["date", "foreign_net_volume", "foreign_buy_volume", "foreign_sell_volume"])
    df = pd.read_csv(path)
    df = df[df["symbol"].str.upper() == symbol.upper()].copy()
    df["date"] = pd.to_datetime(df["trading_date"]).dt.date.astype(str)
    df = df.sort_values("date")
    return df[["date", "foreign_net_volume", "foreign_buy_volume", "foreign_sell_volume"]]


# ---------------------------------------------------------------------------
# 5. Lắp ráp rows cho run_backtest()
# ---------------------------------------------------------------------------

def build_backtest_rows(
    symbol: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    data_dir: Path = DATA_DIR,
    sector: str | None = None,
    foreign_window: int = 5,
    sl_tp_mode: str = "FIXED",
    fixed_sl_pct: float | None = None,
    fixed_tp_pct: float | None = None,
    investment_mode: str = "SHORT_TERM",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Trả về (rows, warnings). rows là input cho run_backtest().

    Chỉ báo kỹ thuật (kể cả Order Block) được tính trên TOÀN BỘ lịch sử giá
    có sẵn theo kiểu rolling as-of (để sma_50, rsi_14... đủ dữ liệu khởi động
    và Order Block không nhìn trước tương lai) rồi mới cắt về
    [start_date, end_date].

    sl_tp_mode: "FIXED" (mặc định, % cố định theo config chiến lược) hoặc
    "STRUCTURE" (theo Order Block/Bollinger — tính năng mới).
    investment_mode: "SHORT_TERM" (mặc định, kỹ thuật/momentum), "LONG_TERM"
    (cơ bản/tích sản: ROE, D/E, P/E, P/B) hoặc "BOTH" (ngắn hạn + chú thích
    góc nhìn dài hạn) — khớp với strategies.py bản mới nhất.
    """

    price = _rolling_indicators(_load_ohlcv(symbol, data_dir / "history"))
    market = _load_market(data_dir / "market" / "history")
    fundamentals = _load_fundamentals(symbol, data_dir / "fundamental")
    foreign = _load_foreign_flow(symbol, data_dir / "foreign")

    df = price.merge(market, on="date", how="left")
    df["prev_close"] = df["close"].shift(1)
    fnd = _asof_fundamentals(df["date"], fundamentals)
    df = pd.concat([df.reset_index(drop=True), fnd.reset_index(drop=True)], axis=1)
    df = df.merge(foreign, on="date", how="left")

    if start_date:
        df = df[df["date"] >= start_date]
    if end_date:
        df = df[df["date"] <= end_date]
    df = df.reset_index(drop=True)

    warnings: dict[str, Any] = {}
    missing_market_days = int(df["market_close"].isna().sum())
    if missing_market_days:
        warnings["missing_market_days"] = missing_market_days
    missing_fundamental_days = int(df["roe_ttm"].isna().sum())
    if missing_fundamental_days:
        warnings["missing_fundamental_days"] = missing_fundamental_days
    estimated_days = int(
        df.get("published_date_estimated", pd.Series(dtype=bool))
        .astype("boolean")
        .fillna(False)
        .astype(bool)
        .sum()
    )
    if estimated_days:
        warnings["published_date_estimated_days"] = estimated_days

    foreign_dates = set(foreign["date"]) if not foreign.empty else set()
    covered_days = sum(1 for d in df["date"] if d in foreign_dates)
    warnings["foreign_flow_days_available_in_window"] = covered_days
    warnings["foreign_flow_days_total_in_window"] = len(df)
    warnings["foreign_flow_window_required"] = foreign_window
    warnings["sl_tp_mode"] = sl_tp_mode

    rows: list[dict[str, Any]] = []
    for i, record in df.iterrows():
        date = record["date"]
        snapshot: dict[str, Any] = {
            "symbol": symbol.upper(),
            "data_status": "OK",
            "signal_status": "ELIGIBLE",
            "latest_close": record["close"],
            "sma_20": _nan_to_none(record.get("sma_20")),
            "sma_50": _nan_to_none(record.get("sma_50")),
            "sma_200": _nan_to_none(record.get("sma_200")),
            "rsi_14": _nan_to_none(record.get("rsi_14")),
            "macd": _nan_to_none(record.get("macd")),
            "macd_signal": _nan_to_none(record.get("macd_signal")),
            "stochastic_k_14": _nan_to_none(record.get("stochastic_k_14")),
            "stochastic_d_3": _nan_to_none(record.get("stochastic_d_3")),
            "bollinger_upper_20": _nan_to_none(record.get("bollinger_upper_20")),
            "bollinger_lower_20": _nan_to_none(record.get("bollinger_lower_20")),
            "bollinger_bandwidth_20": _nan_to_none(record.get("bollinger_bandwidth_20")),
            "volume_ratio_20d": _nan_to_none(record.get("volume_ratio_20d")),
            "ob_support": _nan_to_none(record.get("ob_support")),
            "ob_resistance": _nan_to_none(record.get("ob_resistance")),
            "roe_ttm": _nan_to_none(record.get("roe_ttm")),
            "debt_to_equity": _nan_to_none(record.get("debt_to_equity")),
            "pe": _nan_to_none(record.get("pe")),
            "pb": _nan_to_none(record.get("pb")),
            "indicator_data_complete": bool(record.get("indicator_data_complete")),
            "average_volume_20d": _nan_to_none(record.get("average_volume_20d")),
            "price_unit_vnd": 1000.0,
            "board_lot_size": 100,
            # Cả 16 mã hiện đang test đều niêm yết HOSE — đủ để Gate7
            # (tick size, biên độ giá trần/sàn 7%) của risk_manager hoạt
            # động đúng. Nếu backtest mã HNX/UPCOM sau này, đổi giá trị này.
            "exchange": "HOSE",
            "previous_close": _nan_to_none(record.get("prev_close")),
            "sl_tp_mode": sl_tp_mode,
            "investment_mode": investment_mode,
        }
        if fixed_sl_pct is not None:
            snapshot["fixed_sl_pct"] = fixed_sl_pct
        if fixed_tp_pct is not None:
            snapshot["fixed_tp_pct"] = fixed_tp_pct

        market_close = _nan_to_none(record.get("market_close"))
        market_sma_20 = _nan_to_none(record.get("market_sma_20"))
        market_sma_50 = _nan_to_none(record.get("market_sma_50"))
        market_sma_200 = _nan_to_none(record.get("market_sma_200"))
        market_ctx = (
            {
                "close": market_close,
                "sma_20": market_sma_20,
                "sma_50": market_sma_50,
                "sma_200": market_sma_200,
                "symbol": record.get("market_symbol") or "VNINDEX",
            }
            if market_close is not None and market_sma_20 is not None
            else None
        )

        # Cửa sổ 5 phiên khối ngoại LIÊN TỤC TRƯỚC/BAO GỒM ngày xét lệnh, lấy
        # đúng dữ liệu đã crawl được. Lưu ý: với bản chiến lược mới, thiếu
        # cửa sổ này KHÔNG còn chặn tín hiệu BUY (chỉ bỏ qua kiểm tra khối
        # ngoại), nên không cần "bịa" dữ liệu như bản cũ nữa.
        window_start = max(0, i - foreign_window + 1)
        window = df.iloc[window_start : i + 1]
        foreign_sessions = []
        if window["foreign_net_volume"].notna().all() and len(window) == foreign_window:
            for _, wrow in window.iterrows():
                foreign_sessions.append(
                    {
                        "net_volume": float(wrow["foreign_net_volume"]),
                        "buy_volume": _nan_to_none(wrow.get("foreign_buy_volume")),
                        "sell_volume": _nan_to_none(wrow.get("foreign_sell_volume")),
                        "as_of": wrow["date"],
                    }
                )

        rows.append(
            {
                "date": date,
                "symbol": symbol.upper(),
                "open": record["open"],
                "high": record["high"],
                "low": record["low"],
                "close": record["close"],
                "snapshot": snapshot,
                "market": market_ctx,
                "foreign_sessions": foreign_sessions,
                "sector": sector,
            }
        )
    return rows, warnings


def _nan_to_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None
