#!/usr/bin/env python3
"""
CLI runner cho backtest dùng backtesting.engine hiện có.

Có các chế độ đầu tư giống runner backtest_real:
    SHORT_TERM  : ngắn hạn, thiên về kỹ thuật/momentum
    LONG_TERM   : dài hạn, thiên về cơ bản/tích sản
    BOTH        : kết hợp góc nhìn ngắn hạn + dài hạn

Ví dụ:
    py backtesting\run_symbol_backtest_v3.py --symbol FPT --months 6
    py backtesting\run_symbol_backtest_v3.py --symbol FPT --months 6 --investment-mode SHORT_TERM
    py backtesting\run_symbol_backtest_v3.py --symbol FPT --months 6 --investment-mode LONG_TERM
    py backtesting\run_symbol_backtest_v3.py --symbol FPT --months 6 --investment-mode BOTH
    py backtesting\run_symbol_backtest_v3.py --symbol FPT --months 6 --sl-tp-mode STRUCTURE
    py backtesting\run_symbol_backtest_v3.py --symbol FPT --start 2026-03-25 --end 2026-09-21 --debug

Quan trọng:
- Đây là CLI runner, KHÔNG tạo backtest engine thứ hai.
- Engine chính: backtesting.engine.run_backtest()
- Data/context: dùng backtesting.historical_dataset.build_backtest_rows().
- --debug có thêm BUY gate diagnostics: PASS/FAIL/SKIP cho từng điều kiện.
- investment_mode được truyền vào historical_dataset để snapshot của từng
  ngày mang đúng chế độ mà QualityTrendStrategy đang sử dụng.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from backtesting.engine import BacktestConfig, run_backtest  # noqa: E402

# Data builder đã được chuyển vào backtesting/.
# Fallback giữ tương thích nếu bạn chạy file trực tiếp trong một số môi trường cũ.
try:
    from backtesting.historical_dataset import (  # noqa: E402
        DATA_DIR,
        _load_ohlcv,
        build_backtest_rows,
    )
except ImportError:
    from historical_dataset import (  # type: ignore # noqa: E402
        DATA_DIR,
        _load_ohlcv,
        build_backtest_rows,
    )

# Dùng CHÍNH config hiện tại của quality_trend_v1 để debug không bị lệch ngưỡng.
try:
    from signals.strategies import QualityTrendConfig, QualityTrendStrategy  # noqa: E402
except ImportError:
    QualityTrendConfig = None  # type: ignore
    QualityTrendStrategy = None  # type: ignore


KNOWN_SECTORS = {
    "FPT": "cong nghe",
    "HPG": "thep",
    "DGC": "hoa chat",
    "MWG": "ban le",
    "PNJ": "ban le",
    "QNS": "thuc pham",
    "VNM": "thuc pham",
    "MSN": "tieu dung",
    "REE": "ha tang tien ich",
    "VIC": "bat dong san",
    "VHM": "bat dong san",
    "BID": "ngan hang",
    "VCB": "ngan hang",
    "CTG": "ngan hang",
    "TCB": "ngan hang",
    "MBB": "ngan hang",
    "ACB": "ngan hang",
}


def _print_report(
    symbol: str,
    start: str,
    end: str,
    investment_mode: str,
    sl_tp_mode: str,
    result: Any,
) -> None:
    metrics = result.metrics()

    print("=" * 68)
    print(
        f"BACKTEST {symbol.upper()} | {start} -> {end} | "
        f"MODE={investment_mode} | SL/TP={sl_tp_mode}"
    )
    print("=" * 68)
    print(f"Vốn ban đầu     : {result.initial_cash:,.0f} VND")
    print(f"Vốn cuối kỳ     : {result.final_equity:,.0f} VND")
    print(f"Lãi/lỗ tổng     : {metrics['total_return_pct']:.2f}%")
    print(f"Max drawdown    : {metrics['max_drawdown_pct']:.2f}%")
    print(f"Số lệnh đã đóng : {int(metrics['trade_count'])}")
    print(f"Tỷ lệ thắng     : {metrics['win_rate_pct']:.1f}%")
    print(f"Kỳ vọng/lệnh    : {metrics['expectancy']:,.0f} VND")

    pf = metrics["profit_factor"]
    print(
        f"Profit factor   : "
        f"{pf:.2f}" if pf != float("inf") else "Profit factor   : inf"
    )

    if result.trades:
        import pandas as pd
        holding_days_list = []
        for trade in result.trades:
            try:
                d_in = pd.to_datetime(trade.entry_date)
                d_out = pd.to_datetime(trade.exit_date)
                holding_days_list.append(max(0, (d_out - d_in).days))
            except Exception:
                pass
        avg_hold = sum(holding_days_list) / len(holding_days_list) if holding_days_list else 0.0
        print(f"Thời gian giữ TB: {avg_hold:.1f} ngày/lệnh")
    print()

    if result.trades:
        print("CHI TIẾT LỆNH:")
        for trade in result.trades:
            print(
                f"  {trade.entry_date} MUA {trade.quantity:>10,.2f} CP "
                f"@ {trade.entry_price:,.2f} -> "
                f"{trade.exit_date} BÁN @ {trade.exit_price:,.2f} "
                f"({trade.exit_trigger:<16}) "
                f"PnL={trade.pnl:,.0f} VND "
                f"({trade.return_pct:+.2f}%)"
            )
    else:
        print("Không có lệnh nào được khớp trong giai đoạn này.")
        print("💡 Gợi ý: Khung thời gian hiện tại quá ngắn; hãy dùng --months 36 (3 năm) hoặc --months 72 (6 năm) để có mẫu kiểm thử đầy đủ.")

    print()

    open_positions = getattr(result, "open_positions", None)
    if open_positions:
        print("VỊ THẾ CÒN MỞ CUỐI KỲ:")
        for position in open_positions:
            print(
                f"  {position['entry_date']} MUA "
                f"{position['symbol']} {position['quantity']:>10,.2f} CP "
                f"@ {position['entry_price']:,.2f} -> "
                f"giá cuối kỳ {position['last_price']:,.2f} | "
                f"PnL tạm tính={position['unrealized_pnl']:,.0f} VND"
            )
        print()


def _signal_date(signal: Any) -> str:
    """
    SignalEvent của repo dùng data_as_of, không dùng as_of.
    Có fallback để runner tương thích với phiên bản model khác.
    """
    for field in ("data_as_of", "date", "timestamp", "created_at", "as_of"):
        value = getattr(signal, field, None)
        if value is not None:
            return str(value)
    return "?"



def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _financial_sector(sector: str | None) -> bool:
    """Dùng cùng cách nhận diện ngành tài chính của strategy nếu import được."""
    if QualityTrendStrategy is not None:
        try:
            return bool(QualityTrendStrategy._is_financial_sector(sector))
        except Exception:
            pass

    normalized = (sector or "").lower()
    keywords = (
        "bank", "banking", "securities", "financial", "finance",
        "ngan hang", "chung khoan", "tai chinh",
    )
    return any(keyword in normalized for keyword in keywords)


def _foreign_stats(row: dict[str, Any], window: int = 5) -> dict[str, Any]:
    sessions = list(row.get("foreign_sessions") or [])[-window:]
    nets: list[float] = []

    for session in sessions:
        if isinstance(session, dict):
            value = session.get("net_volume", session.get("foreign_net_volume"))
        else:
            value = getattr(session, "net_volume", None)
        number = _to_float(value)
        if number is not None:
            nets.append(number)

    return {
        "session_count": len(nets),
        "net_volume": sum(nets),
        "buy_sessions": sum(value > 0 for value in nets),
        "sell_sessions": sum(value < 0 for value in nets),
    }


def _check(
    name: str,
    passed: bool | None,
    detail: str,
    blocker_key: str,
) -> dict[str, Any]:
    """
    passed=True  -> PASS
    passed=False -> FAIL, chặn BUY
    passed=None  -> SKIP, strategy hiện không chặn vì dữ liệu/điều kiện không áp dụng
    """
    return {
        "name": name,
        "passed": passed,
        "detail": detail,
        "blocker_key": blocker_key,
    }


def _buy_gate_checks(row: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Tái hiện các hard gate BUY trong QualityTrendStrategy._evaluate_entry
    chỉ để CHẨN ĐOÁN. Hàm này KHÔNG thay đổi tín hiệu và KHÔNG đặt lệnh.
    """
    snapshot = dict(row.get("snapshot") or {})
    market = row.get("market") or {}
    sector = row.get("sector")
    mode = str(snapshot.get("investment_mode") or "SHORT_TERM").strip().upper()

    # Lấy đúng ngưỡng mặc định từ QualityTrendConfig nếu import được.
    if QualityTrendConfig is not None:
        try:
            cfg = QualityTrendConfig()
            roe_min = float(cfg.roe_ttm_min)
            de_max = float(snapshot.get("debt_to_equity_max") or snapshot.get("de_max") or cfg.debt_to_equity_max)
            rsi_min = float(cfg.rsi_min)
            rsi_max = float(cfg.rsi_max)
            foreign_window = int(cfg.foreign_window)
        except Exception:
            roe_min, de_max, rsi_min, rsi_max, foreign_window = 0.15, 2.0, 50.0, 70.0, 5
    else:
        roe_min, de_max, rsi_min, rsi_max, foreign_window = 0.15, 2.0, 50.0, 70.0, 5

    close = _to_float(snapshot.get("latest_close", row.get("close")))
    sma20 = _to_float(snapshot.get("sma_20"))
    sma50 = _to_float(snapshot.get("sma_50"))
    rsi = _to_float(snapshot.get("rsi_14"))
    macd = _to_float(snapshot.get("macd"))
    macd_signal = _to_float(snapshot.get("macd_signal"))
    roe = _to_float(snapshot.get("roe_ttm"))
    de = _to_float(snapshot.get("debt_to_equity"))
    pe = _to_float(snapshot.get("pe"))
    pb = _to_float(snapshot.get("pb"))
    volume_ratio = _to_float(snapshot.get("volume_ratio_20d"))
    bollinger_upper = _to_float(snapshot.get("bollinger_upper_20"))
    bollinger_bw = _to_float(snapshot.get("bollinger_bandwidth_20"))
    ob_resistance = _to_float(snapshot.get("ob_resistance"))

    m_close = _to_float(market.get("close")) if isinstance(market, dict) else None
    m_sma20 = _to_float(market.get("sma_20")) if isinstance(market, dict) else None

    foreign = _foreign_stats(row, foreign_window)
    is_financial = _financial_sector(sector)

    checks: list[dict[str, Any]] = []

    # Strategy hiện yêu cầu RSI/MACD/MACD signal có dữ liệu trước cả LONG_TERM.
    missing_tech = [
        name
        for name, value in (
            ("RSI", rsi),
            ("MACD", macd),
            ("MACD Signal", macd_signal),
        )
        if value is None
    ]
    checks.append(
        _check(
            "Dữ liệu kỹ thuật",
            not missing_tech,
            "đủ RSI/MACD/Signal" if not missing_tech else f"thiếu {', '.join(missing_tech)}",
            "MISSING_TECH_DATA",
        )
    )
    if missing_tech:
        return checks

    if mode == "LONG_TERM":
        if roe is None:
            checks.append(_check("ROE TTM", None, "thiếu ROE -> strategy hiện không chặn", "ROE_LT"))
        else:
            checks.append(
                _check(
                    "ROE TTM",
                    roe >= roe_min,
                    f"{roe*100:.1f}% {'>=' if roe >= roe_min else '<'} {roe_min*100:.1f}%",
                    "ROE_LT",
                )
            )

        if is_financial:
            checks.append(_check("D/E dài hạn", None, "ngành tài chính -> bỏ qua D/E", "DE_LT"))
        elif de is None:
            checks.append(_check("D/E dài hạn", None, "thiếu D/E -> strategy hiện không chặn", "DE_LT"))
        else:
            checks.append(
                _check(
                    "D/E dài hạn",
                    de <= 1.8,
                    f"{de:.2f} {'<=' if de <= 1.8 else '>'} 1.80",
                    "DE_LT",
                )
            )

        if pe is None:
            checks.append(_check("P/E", None, "thiếu P/E -> strategy hiện không chặn", "PE_LT"))
        else:
            checks.append(
                _check("P/E", pe <= 16.0, f"{pe:.1f} {'<=' if pe <= 16 else '>'} 16.0", "PE_LT")
            )

        if pb is None:
            checks.append(_check("P/B", None, "thiếu P/B -> strategy hiện không chặn", "PB_LT"))
        else:
            checks.append(
                _check("P/B", pb <= 2.2, f"{pb:.1f} {'<=' if pb <= 2.2 else '>'} 2.2", "PB_LT")
            )

        panic_sell = (
            rsi is not None
            and rsi < 30
            and foreign["net_volume"] < 0
            and foreign["sell_sessions"] >= 4
        )
        checks.append(
            _check(
                "Không bị bán tháo",
                not panic_sell,
                (
                    f"RSI={rsi:.1f}, foreign={foreign['net_volume']:+,.0f}, "
                    f"sell={foreign['sell_sessions']}/{foreign['session_count']}"
                ),
                "PANIC_SELL_LT",
            )
        )
        return checks

    # SHORT_TERM và BOTH hiện dùng cùng hard gate BUY trong strategies.py.
    if m_close is None or m_sma20 is None:
        checks.append(
            _check(
                "Market > SMA20",
                None,
                "thiếu market context -> strategy hiện bỏ qua gate này",
                "MARKET",
            )
        )
    else:
        checks.append(
            _check(
                "Market > SMA20",
                m_close > m_sma20,
                f"{m_close:.2f} {'>' if m_close > m_sma20 else '<='} {m_sma20:.2f}",
                "MARKET",
            )
        )

    if roe is None:
        checks.append(_check("ROE TTM", None, "thiếu ROE -> strategy hiện không chặn", "ROE_ST"))
    else:
        checks.append(
            _check(
                "ROE TTM",
                roe >= roe_min,
                f"{roe*100:.1f}% {'>=' if roe >= roe_min else '<'} {roe_min*100:.1f}%",
                "ROE_ST",
            )
        )

    if is_financial:
        checks.append(_check("D/E", None, "ngành tài chính -> bỏ qua D/E", "DE_ST"))
    elif de is None:
        checks.append(_check("D/E", None, "thiếu D/E -> strategy hiện không chặn", "DE_ST"))
    else:
        checks.append(
            _check(
                "D/E",
                de <= de_max,
                f"{de:.2f} {'<=' if de <= de_max else '>'} {de_max:.2f}",
                "DE_ST",
            )
        )

    if close is None or sma20 is None:
        checks.append(_check("Close > SMA20", None, "thiếu Close/SMA20", "CLOSE_SMA20"))
    else:
        checks.append(
            _check(
                "Close > SMA20",
                close > sma20,
                f"{close:.2f} {'>' if close > sma20 else '<='} {sma20:.2f}",
                "CLOSE_SMA20",
            )
        )

    if sma20 is None or sma50 is None:
        checks.append(_check("SMA20 > SMA50", None, "thiếu SMA20/SMA50", "SMA20_SMA50"))
    else:
        checks.append(
            _check(
                "SMA20 > SMA50",
                sma20 > sma50,
                f"{sma20:.2f} {'>' if sma20 > sma50 else '<='} {sma50:.2f}",
                "SMA20_SMA50",
            )
        )

    checks.append(
        _check(
            f"RSI {rsi_min:.0f}-{rsi_max:.0f}",
            rsi_min <= rsi <= rsi_max,
            f"{rsi:.2f} {'trong' if rsi_min <= rsi <= rsi_max else 'ngoài'} [{rsi_min:.0f}, {rsi_max:.0f}]",
            "RSI",
        )
    )

    checks.append(
        _check(
            "MACD > Signal",
            macd > macd_signal,
            f"{macd:.3f} {'>' if macd > macd_signal else '<='} {macd_signal:.3f}",
            "MACD",
        )
    )

    if foreign["session_count"] < foreign_window:
        checks.append(
            _check(
                "Khối ngoại",
                None,
                f"chỉ có {foreign['session_count']}/{foreign_window} phiên -> strategy bỏ qua gate bán mạnh",
                "FOREIGN",
            )
        )
    else:
        foreign_dump = (
            foreign["net_volume"] < 0
            and foreign["sell_sessions"] >= 4
        )
        checks.append(
            _check(
                "Khối ngoại",
                not foreign_dump,
                (
                    f"net={foreign['net_volume']:+,.0f}, "
                    f"sell={foreign['sell_sessions']}/{foreign['session_count']}"
                ),
                "FOREIGN",
            )
        )

    bollinger_trap = (
        bollinger_upper is not None
        and close is not None
        and close > bollinger_upper * 1.01
        and (bollinger_bw is None or bollinger_bw < 0.08)
    )
    if bollinger_upper is None:
        checks.append(_check("Bollinger", None, "thiếu Bollinger upper -> strategy không chặn", "BOLLINGER"))
    else:
        bw_text = "NA" if bollinger_bw is None else f"{bollinger_bw*100:.1f}%"
        checks.append(
            _check(
                "Bollinger",
                not bollinger_trap,
                f"Close={close:.2f}, Upper={bollinger_upper:.2f}, BW={bw_text}",
                "BOLLINGER",
            )
        )

    if volume_ratio is None:
        checks.append(_check("Volume >= 0.8x", None, "thiếu volume ratio -> strategy không chặn", "VOLUME"))
    else:
        checks.append(
            _check(
                "Volume >= 0.8x",
                volume_ratio >= 0.8,
                f"{volume_ratio:.2f}x {'>=' if volume_ratio >= 0.8 else '<'} 0.80x",
                "VOLUME",
            )
        )

    if ob_resistance is None or close is None or close >= ob_resistance:
        checks.append(
            _check(
                "Dư địa tới Order Block",
                None,
                "không có cản phía trên áp dụng cho gate 3.5%",
                "ORDER_BLOCK",
            )
        )
    else:
        upside = (ob_resistance - close) / close
        checks.append(
            _check(
                "Dư địa tới Order Block",
                upside >= 0.035,
                f"{upside*100:.2f}% {'>=' if upside >= 0.035 else '<'} 3.50% (cản={ob_resistance:.2f})",
                "ORDER_BLOCK",
            )
        )

    return checks


def _debug_icon(passed: bool | None) -> str:
    if passed is True:
        return "✅"
    if passed is False:
        return "❌"
    return "➖"


def _print_buy_gate_debug(
    rows: list[dict[str, Any]],
    result: Any,
    *,
    limit: int = 10,
) -> None:
    signals = list(getattr(result, "signals", []) or [])
    actions_by_date: dict[str, str] = {}
    for signal in signals:
        actions_by_date[_signal_date(signal)[:10]] = str(getattr(signal, "action", "UNKNOWN"))

    # 1) Tổng hợp gate với đầy đủ 3 trạng thái: PASS, FAIL, SKIP (thiếu dữ liệu).
    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        for check in _buy_gate_checks(row):
            key = str(check["blocker_key"])
            info = stats.setdefault(
                key,
                {"name": check["name"], "pass": 0, "fail": 0, "skip": 0},
            )
            if check["passed"] is True:
                info["pass"] += 1
            elif check["passed"] is False:
                info["fail"] += 1
            else:
                info["skip"] += 1

    total_sessions = max(len(rows), 1)
    print(f"[DEBUG] BUY GATE DIAGNOSTICS - Thống kê {total_sessions} phiên (PASS / FAIL / SKIP):")
    print(f"  {'Tên điều kiện':<26} | {'PASS (✅)':<16} | {'FAIL (❌)':<16} | {'SKIP/THIẾU DATA (➖)':<20}")
    print(f"  {'-'*26}-+-{'-'*16}-+-{'-'*16}-+-{'-'*20}")

    # Sắp xếp theo FAIL giảm dần, sau đó theo SKIP giảm dần
    ranked = sorted(
        stats.values(),
        key=lambda item: (int(item["fail"]), int(item["skip"])),
        reverse=True,
    )
    for item in ranked:
        p_pct = 100.0 * item["pass"] / total_sessions
        f_pct = 100.0 * item["fail"] / total_sessions
        s_pct = 100.0 * item["skip"] / total_sessions
        warn_mark = " ⚠️ (DATA LỆCH/THIẾU)" if item["skip"] > 0 else ""
        print(
            f"  {item['name']:<26} | "
            f"{item['pass']:>3}/{total_sessions} ({p_pct:5.1f}%) | "
            f"{item['fail']:>3}/{total_sessions} ({f_pct:5.1f}%) | "
            f"{item['skip']:>3}/{total_sessions} ({s_pct:5.1f}%){warn_mark}"
        )
    print()

    # 2) Chi tiết từng gate ở các phiên gần nhất.
    selected_rows = rows[-max(1, limit):]
    print(f"[DEBUG] BUY GATE - {len(selected_rows)} phiên gần nhất:")
    for row in selected_rows:
        date_text = str(row.get("date", "?"))[:10]
        action = actions_by_date.get(date_text, "?")
        checks = _buy_gate_checks(row)
        failed = [check for check in checks if check["passed"] is False]
        skipped = [check for check in checks if check["passed"] is None]

        if not failed:
            headline = "ĐỦ HARD GATE BUY"
        else:
            headline = f"BLOCKED bởi {len(failed)} gate"

        print()
        print(f"  {date_text} | signal={action:<10} | {headline}")
        for check in checks:
            print(
                f"    {_debug_icon(check['passed'])} "
                f"{check['name']:<24} | {check['detail']}"
            )

        if failed:
            print(
                "    => BUY FAILED: "
                + "; ".join(str(check["name"]) for check in failed)
            )
        elif skipped:
            print(
                "    => Không có gate FAIL; có gate SKIP do không áp dụng/thiếu dữ liệu."
            )
        else:
            print("    => Tất cả hard gate BUY đều PASS.")

    print()



def _print_debug(result: Any) -> None:
    signals = list(getattr(result, "signals", []) or [])

    counts: dict[str, int] = {}
    for signal in signals:
        action = str(getattr(signal, "action", "UNKNOWN"))
        counts[action] = counts.get(action, 0) + 1

    print("[DEBUG] Phân bổ tín hiệu:")
    for action in ("BUY", "SELL", "HOLD", "NO SIGNAL"):
        if counts.get(action):
            print(f"  {action:<10}: {counts[action]} phiên")

    other_actions = {
        key: value
        for key, value in counts.items()
        if key not in {"BUY", "SELL", "HOLD", "NO SIGNAL"}
    }
    for action, count in sorted(other_actions.items()):
        print(f"  {action:<10}: {count} phiên")
    print()

    if signals:
        print("[DEBUG] 10 tín hiệu gần nhất:")
        for signal in signals[-10:]:
            action = str(getattr(signal, "action", "UNKNOWN"))
            signal_date = _signal_date(signal)
            reasons = getattr(signal, "reasons", []) or []
            reason_text = " | ".join(str(x) for x in reasons[:3])
            print(f"  {signal_date} | {action:<10} | {reason_text}")
        print()


def _resolve_period(
    full_price: Any,
    start: str | None,
    end: str | None,
    months: int,
) -> tuple[str, str]:
    last_available = str(full_price["date"].max())[:10]
    resolved_end = end or last_available

    if start:
        resolved_start = start
    else:
        end_date = date.fromisoformat(resolved_end)
        resolved_start = (
            end_date - timedelta(days=30 * months)
        ).isoformat()

    return resolved_start, resolved_end


def _get_available_symbols() -> list[str]:
    """Lấy toàn bộ danh sách mã cổ phiếu có sẵn dữ liệu lịch sử trong data/history/."""
    history_dir = DATA_DIR / "history"
    if not history_dir.is_dir():
        return []
    symbols = []
    for p in sorted(history_dir.glob("*_1D.csv")):
        sym = p.name.replace("_1D.csv", "").upper()
        if sym not in {"VNINDEX", "VN30"}:
            symbols.append(sym)
    return symbols


def _print_portfolio_summary_table(
    results_by_sym: dict[str, tuple[str, str, Any]],
    args: Any,
) -> None:
    """In bảng so sánh và tổng hợp danh mục gọn gàng, trực quan khi chạy nhiều mã."""
    tot_n = 6 if args.investment_mode == "SHORT_TERM" else 8
    regime_str = args.market_regime
    is_pure = not args.standard_exit and args.pure_asset_exit
    exit_mode_str = "Pure Asset Exit" if is_pure else "Standard Exit"
    entry_str = f"K={args.confluence_k}/{tot_n}" if args.confluence_k is not None else "Baseline"

    print("\n" + "=" * 105)
    print(f"BẢNG TỔNG HỢP BACKTEST DANH MỤC ({len(results_by_sym)} MÃ) | {entry_str} | {exit_mode_str} | REGIME: {regime_str}")
    print("=" * 105)
    header = (
        f"{'Mã':<6} {'Số lệnh':>8} {'Thắng/Thua':>12} {'Win Rate':>10} "
        f"{'PnL (VND)':>16} {'Tỷ suất':>9} {'Profit Fac':>11} {'Max DD':>9} {'Kỳ vọng/lệnh':>16}"
    )
    print(header)
    print("-" * 105)

    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_pnl = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    winning_symbols = 0

    for symbol, (st, en, res) in results_by_sym.items():
        if res is None:
            print(f"{symbol:<6} {'LỖI/KHÔNG DỮ LIỆU':>16}")
            continue
        m = res.metrics()
        trades = res.trades
        n_trades = len(trades)
        wins = len([t for t in trades if t.pnl > 0])
        losses = len([t for t in trades if t.pnl <= 0])
        wr = (wins / n_trades * 100) if n_trades > 0 else 0.0
        pnl = res.final_equity - res.initial_cash
        ret_pct = m["total_return_pct"]
        pf = m["profit_factor"]
        pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
        dd = m["max_drawdown_pct"]
        exp = m["expectancy"]

        total_trades += n_trades
        total_wins += wins
        total_losses += losses
        total_pnl += pnl
        if pnl > 0:
            winning_symbols += 1

        for t in trades:
            if t.pnl > 0:
                gross_profit += t.pnl
            else:
                gross_loss += abs(t.pnl)

        print(
            f"{symbol:<6} {n_trades:>8} {f'{wins}/{losses}':>12} {f'{wr:.1f}%':>10} "
            f"{f'{pnl:+,.0f} VND':>16} {f'{ret_pct:+.2f}%':>9} {pf_str:>11} {f'{dd:.2f}%':>9} "
            f"{f'{exp:+,.0f} VND':>16}"
        )

    print("-" * 105)
    pooled_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0.0
    pooled_pf = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    pooled_pf_str = f"{pooled_pf:.2f}" if pooled_pf != float("inf") else "inf"
    pooled_exp = (total_pnl / total_trades) if total_trades > 0 else 0.0

    print(
        f"{'TỔNG':<6} {total_trades:>8} {f'{total_wins}/{total_losses}':>12} {f'{pooled_wr:.1f}%':>10} "
        f"{f'{total_pnl:+,.0f} VND':>16} {'-':>9} {pooled_pf_str:>11} {'-':>9} "
        f"{f'{pooled_exp:+,.0f} VND':>16}"
    )
    print("=" * 105)
    print("📊 Thống kê danh mục:")
    print(f"  - Số mã sinh lời (Breadth) : {winning_symbols}/{len(results_by_sym)} mã ({winning_symbols/len(results_by_sym)*100:.1f}%)")
    print(f"  - Tổng PnL ròng tích lũy   : {total_pnl:+,.0f} VND")
    print(f"  - Tỷ lệ thắng gộp (WR)     : {pooled_wr:.1f}% ({total_wins} thắng / {total_trades} lệnh)")
    print(f"  - Profit Factor gộp        : {pooled_pf_str}")
    print(f"  - Kỳ vọng lợi nhuận/lệnh   : {pooled_exp:+,.0f} VND/lệnh")
    print("=" * 105 + "\n")


def _run_symbol_pipeline(symbol: str, args: Any) -> tuple[str, str, list[dict], Any, str | None]:
    """Chạy toàn bộ pipeline nạp dữ liệu và backtest cho một mã cổ phiếu."""
    sector = args.sector or KNOWN_SECTORS.get(symbol, "khac")
    try:
        full_price = _load_ohlcv(symbol, DATA_DIR / "history")
    except Exception as exc:
        return "", "", [], None, f"Không đọc được dữ liệu giá {symbol}: {exc}"

    start, end = _resolve_period(
        full_price,
        args.start,
        args.end,
        args.months,
    )

    try:
        rows, _ = build_backtest_rows(
            symbol,
            start_date=start,
            end_date=end,
            sector=sector,
            sl_tp_mode=args.sl_tp_mode,
            fixed_sl_pct=args.fixed_sl_pct,
            fixed_tp_pct=args.fixed_tp_pct,
            investment_mode=args.investment_mode,
        )
    except TypeError:
        rows, _ = build_backtest_rows(
            symbol,
            start_date=start,
            end_date=end,
            sector=sector,
            sl_tp_mode=args.sl_tp_mode,
            fixed_sl_pct=args.fixed_sl_pct,
            fixed_tp_pct=args.fixed_tp_pct,
        )

    if not rows:
        return start, end, [], None, "Không có dữ liệu trong khoảng ngày yêu cầu."

    if args.de_max is not None:
        for row in rows:
            if "snapshot" in row and isinstance(row["snapshot"], dict):
                row["snapshot"]["debt_to_equity_max"] = args.de_max

    if args.confluence_k is not None:
        for row in rows:
            if "snapshot" in row and isinstance(row["snapshot"], dict):
                row["snapshot"]["confluence_k"] = args.confluence_k

    is_pure_asset_exit = False if args.standard_exit else args.pure_asset_exit
    for row in rows:
        if "snapshot" in row and isinstance(row["snapshot"], dict):
            row["snapshot"]["pure_asset_exit"] = is_pure_asset_exit

    if args.market_regime:
        for row in rows:
            if "snapshot" in row and isinstance(row["snapshot"], dict):
                row["snapshot"]["market_regime"] = args.market_regime

    config = BacktestConfig(
        initial_cash=args.initial_cash,
        position_size_pct=args.position_size_pct,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
    )

    try:
        result = run_backtest(rows, config=config)
    except Exception as exc:
        return start, end, rows, None, f"Backtest thất bại: {type(exc).__name__}: {exc}"

    return start, end, rows, result, None


def _export_single_to_md(
    file_path: Path,
    symbol: str,
    start: str,
    end: str,
    args: Any,
    result: Any,
) -> None:
    metrics = result.metrics()
    pnl = result.final_equity - result.initial_cash
    pf = metrics["profit_factor"]
    pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
    wins = len([t for t in result.trades if t.pnl > 0])
    losses = len([t for t in result.trades if t.pnl <= 0])
    is_pure = not args.standard_exit and args.pure_asset_exit
    exit_mode_str = "Pure Asset Exit" if is_pure else "Standard Exit"
    tot_n = 6 if args.investment_mode == "SHORT_TERM" else 8
    entry_str = f"K={args.confluence_k}/{tot_n}" if args.confluence_k is not None else "Baseline"

    lines = [
        f"# 📊 Báo Cáo Backtest Cổ Phiếu {symbol.upper()}",
        "",
        f"- **Khoảng thời gian:** {start} -> {end}",
        f"- **Chế độ đầu tư:** `{args.investment_mode}` | **SL/TP:** `{args.sl_tp_mode}`",
        f"- **Cơ chế chiến lược:** {entry_str} | {exit_mode_str} | **Regime:** {args.market_regime}",
        f"- **Vốn ban đầu:** {result.initial_cash:,.0f} VND",
        f"- **Vốn cuối kỳ:** {result.final_equity:,.0f} VND",
        f"- **Lợi nhuận ròng:** **{pnl:+,.0f} VND** ({metrics['total_return_pct']:+.2f}%)",
        f"- **Tỷ lệ thắng:** {metrics['win_rate_pct']:.1f}% ({wins} thắng / {losses} thua)",
        f"- **Profit Factor:** {pf_str}",
        f"- **Max Drawdown:** {metrics['max_drawdown_pct']:.2f}%",
        f"- **Kỳ vọng/lệnh:** {metrics['expectancy']:+,.0f} VND",
        "",
        "## Chi tiết các giao dịch",
        "",
        "| STT | Ngày mua | Giá mua | Khối lượng | Ngày bán | Giá bán | Lý do thoát | PnL (VND) | Lãi/Lỗ (%) |",
        "| :---: | :---: | :---: | :---: | :---: | :---: | :--- | :---: | :---: |",
    ]
    for idx, t in enumerate(result.trades, 1):
        lines.append(
            f"| {idx} | {t.entry_date} | {t.entry_price:,.2f} | {t.quantity:,.0f} | "
            f"{t.exit_date} | {t.exit_price:,.2f} | `{t.exit_trigger}` | {t.pnl:+,.0f} | {t.return_pct:+.2f}% |"
        )
    if not result.trades:
        lines.append("| - | - | - | - | - | - | Không có lệnh nào khớp trong kỳ | - | - |")

    file_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n📄 Đã xuất báo cáo markdown vào: {file_path.resolve()}")


def _export_portfolio_to_md(
    file_path: Path,
    results_by_sym: dict[str, tuple[str, str, Any]],
    args: Any,
) -> None:
    tot_n = 6 if args.investment_mode == "SHORT_TERM" else 8
    regime_str = args.market_regime
    is_pure = not args.standard_exit and args.pure_asset_exit
    exit_mode_str = "Pure Asset Exit" if is_pure else "Standard Exit"
    entry_str = f"K={args.confluence_k}/{tot_n}" if args.confluence_k is not None else "Baseline"

    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_pnl = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    winning_symbols = 0

    rows_md = []
    for symbol, (st, en, res) in results_by_sym.items():
        if res is None:
            rows_md.append(f"| {symbol} | LỖI | - | - | - | - | - | - | - |")
            continue
        m = res.metrics()
        trades = res.trades
        n_trades = len(trades)
        wins = len([t for t in trades if t.pnl > 0])
        losses = len([t for t in trades if t.pnl <= 0])
        wr = (wins / n_trades * 100) if n_trades > 0 else 0.0
        pnl = res.final_equity - res.initial_cash
        ret_pct = m["total_return_pct"]
        pf = m["profit_factor"]
        pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
        dd = m["max_drawdown_pct"]
        exp = m["expectancy"]

        total_trades += n_trades
        total_wins += wins
        total_losses += losses
        total_pnl += pnl
        if pnl > 0:
            winning_symbols += 1

        for t in trades:
            if t.pnl > 0:
                gross_profit += t.pnl
            else:
                gross_loss += abs(t.pnl)

        rows_md.append(
            f"| **{symbol}** | {n_trades} | {wins}/{losses} | {wr:.1f}% | "
            f"{pnl:+,.0f} VND | {ret_pct:+.2f}% | {pf_str} | {dd:.2f}% | {exp:+,.0f} VND |"
        )

    pooled_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0.0
    pooled_pf = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    pooled_pf_str = f"{pooled_pf:.2f}" if pooled_pf != float("inf") else "inf"
    pooled_exp = (total_pnl / total_trades) if total_trades > 0 else 0.0

    lines = [
        f"# 📊 Báo Cáo Tổng Hợp Backtest Danh Mục ({len(results_by_sym)} Mã)",
        "",
        f"- **Cơ chế vào lệnh:** {entry_str}",
        f"- **Cơ chế thoát lệnh:** {exit_mode_str}",
        f"- **Bộ lọc thị trường:** {regime_str}",
        f"- **Khung thời gian:** Lùi {args.months} tháng | Vốn: {args.initial_cash:,.0f} VND / mã",
        "",
        "## Bảng tổng hợp chi tiết",
        "",
        "| Mã | Số lệnh | Thắng/Thua | Win Rate | PnL Ròng (VND) | Tỷ suất | Profit Factor | Max DD | Kỳ vọng/lệnh |",
        "| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    lines.extend(rows_md)
    lines.extend([
        f"| **TỔNG** | **{total_trades}** | **{total_wins}/{total_losses}** | **{pooled_wr:.1f}%** | **{total_pnl:+,.0f} VND** | - | **{pooled_pf_str}** | - | **{pooled_exp:+,.0f} VND** |",
        "",
        "### Thống kê chung:",
        f"- **Độ rộng danh mục:** {winning_symbols}/{len(results_by_sym)} mã có lãi ({winning_symbols/len(results_by_sym)*100:.1f}%)",
        f"- **Tổng PnL ròng:** {total_pnl:+,.0f} VND",
        f"- **Profit Factor gộp:** {pooled_pf_str}",
        f"- **Kỳ vọng lợi nhuận:** {pooled_exp:+,.0f} VND / lệnh",
    ])

    file_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n📄 Đã xuất báo cáo danh mục markdown vào: {file_path.resolve()}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backtest một hoặc nhiều mã bằng backtesting.engine."
    )

    parser.add_argument(
        "symbol_pos",
        nargs="?",
        default=None,
        help="Mã CP (VD: FPT), danh sách phân tách dấu phẩy (VD: FPT,HPG,MWG), hoặc 'ALL' để chạy toàn bộ rổ.",
    )
    parser.add_argument("--symbol", default=None, help="Mã cổ phiếu cần backtest (mặc định FPT)")
    parser.add_argument(
        "--symbols",
        default=None,
        help="Danh sách mã phân tách bằng dấu phẩy (VD: FPT,HPG,MWG) hoặc 'ALL' để chạy toàn bộ rổ",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="In chi tiết từng lệnh của từng mã khi chạy nhiều mã (mặc định chỉ in bảng tổng hợp gọn)",
    )
    parser.add_argument(
        "--output-md",
        default=None,
        help="Đường dẫn file .md để xuất báo cáo kết quả (Ví dụ: --output-md backtest_results.md)",
    )
    parser.add_argument(
        "--sector",
        default=None,
        help="Ví dụ: 'ngan hang' cho BID/VCB/TCB...",
    )
    parser.add_argument("--start", default=None, help="YYYY-MM-DD")
    parser.add_argument(
        "--end",
        default=None,
        help="YYYY-MM-DD; mặc định là ngày cuối có dữ liệu",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=36,
        help="Số tháng lùi lại nếu không truyền --start (mặc định 36 tháng = 3 năm)",
    )
    parser.add_argument(
        "--initial-cash",
        type=float,
        default=100_000_000.0,
        help="Vốn ban đầu, mặc định 100 triệu VND",
    )

    # GIỮ NGUYÊN 3 chế độ của backtest_real.
    parser.add_argument(
        "--investment-mode",
        choices=["SHORT_TERM", "LONG_TERM", "BOTH"],
        default="SHORT_TERM",
        help=(
            "SHORT_TERM = ngắn hạn/kỹ thuật; "
            "LONG_TERM = dài hạn/cơ bản; "
            "BOTH = kết hợp hai góc nhìn"
        ),
    )

    parser.add_argument(
        "--sl-tp-mode",
        choices=["FIXED", "STRUCTURE"],
        default="FIXED",
        help=(
            "FIXED = SL/TP theo %% cố định; "
            "STRUCTURE = theo Order Block/Bollinger"
        ),
    )
    parser.add_argument("--fixed-sl-pct", type=float, default=None)
    parser.add_argument("--fixed-tp-pct", type=float, default=None)

    parser.add_argument(
        "--position-size-pct",
        type=float,
        default=0.10,
        help=(
            "Tỷ trọng vốn/lệnh cho backtesting.engine. "
            "Mặc định 10%%. Lưu ý: đây là engine hiện tại, "
            "không phải risk_sim của backtest_real."
        ),
    )
    parser.add_argument(
        "--fee-bps",
        type=float,
        default=15.0,
        help="Phí giao dịch theo bps",
    )
    parser.add_argument(
        "--slippage-bps",
        type=float,
        default=5.0,
        help="Trượt giá theo bps",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "In phân bổ BUY/SELL/HOLD, lý do tín hiệu và BUY gate diagnostics "
            "(PASS/FAIL/SKIP từng điều kiện)"
        ),
    )
    parser.add_argument(
        "--debug-buy-limit",
        type=int,
        default=10,
        help="Số phiên gần nhất in chi tiết BUY gate khi dùng --debug (mặc định 10)",
    )
    parser.add_argument(
        "--de-max",
        type=float,
        default=None,
        help="Ngưỡng D/E tối đa cho phép mua (mặc định lấy từ QualityTrendConfig = 2.0)",
    )
    parser.add_argument(
        "--confluence-k",
        type=int,
        default=4,
        help="Số tiêu chí Confluence Tầng 2 tối thiểu cần đạt (1-9). Mặc định 4 (tối ưu ngắn hạn).",
    )
    parser.add_argument(
        "--pure-asset-exit",
        action="store_true",
        default=True,
        help="Chỉ thoát lệnh theo SL/TP và cấu trúc giá cổ phiếu; bỏ qua MARKET_EXIT và FOREIGN_NET_SELL (mặc định BẬT).",
    )
    parser.add_argument(
        "--standard-exit",
        action="store_true",
        help="Bật lại cơ chế thoát lệnh cũ theo thị trường chung (MARKET_EXIT) và khối ngoại (FOREIGN_NET_SELL).",
    )
    parser.add_argument(
        "--market-regime",
        type=str,
        default="SMA20",
        choices=["SMA20", "SMA50", "SMA200", "DUAL"],
        help="Chế độ lọc xu hướng thị trường chung VNINDEX: SMA20 (mặc định), SMA50, SMA200, DUAL (SMA50 & SMA200).",
    )

    args = parser.parse_args()

    if args.confluence_k is not None and not (1 <= args.confluence_k <= 9):
        parser.error("--confluence-k phải nằm trong khoảng [1, 9]")
    if args.months <= 0:
        parser.error("--months phải > 0")
    if args.debug_buy_limit <= 0:
        parser.error("--debug-buy-limit phải > 0")
    if args.initial_cash <= 0:
        parser.error("--initial-cash phải > 0")
    if not 0 < args.position_size_pct <= 1:
        parser.error("--position-size-pct phải nằm trong (0, 1]")
    if args.fee_bps < 0 or args.slippage_bps < 0:
        parser.error("--fee-bps và --slippage-bps không được âm")

    if args.fixed_sl_pct is not None and not 0 < args.fixed_sl_pct < 1:
        parser.error("--fixed-sl-pct phải nằm trong (0, 1)")
    if args.fixed_tp_pct is not None and not 0 < args.fixed_tp_pct < 1:
        parser.error("--fixed-tp-pct phải nằm trong (0, 1)")

    # Phân giải danh sách mã cần chạy
    raw_symbols = args.symbols or args.symbol_pos or args.symbol or "FPT"
    if raw_symbols.strip().upper() == "ALL":
        target_symbols = _get_available_symbols()
        if not target_symbols:
            print("❌ Không tìm thấy mã cổ phiếu nào trong data/history/")
            return 1
    elif "," in raw_symbols:
        target_symbols = [s.strip().upper() for s in raw_symbols.split(",") if s.strip()]
    else:
        target_symbols = [raw_symbols.strip().upper()]

    # TRƯỜNG HỢP 1: Chạy 1 mã duy nhất (In báo cáo chi tiết từng lệnh)
    if len(target_symbols) == 1:
        symbol = target_symbols[0]
        start, end, rows, result, err = _run_symbol_pipeline(symbol, args)
        if err:
            print(f"❌ {err}")
            return 2 if "thất bại" in err else 1

        is_pure_asset_exit = False if args.standard_exit else args.pure_asset_exit
        tot_n = 6 if args.investment_mode == "SHORT_TERM" else 8
        entry_mode_str = f"Hybrid Confluence K={args.confluence_k}/{tot_n} (4 Hard Gates + Confluence)" if args.confluence_k is not None else "Baseline (AND Hard Gates)"
        exit_mode_str = "Pure Asset Exit (Chỉ thoát theo SL/TP/Trend của CP)" if is_pure_asset_exit else "Standard Exit (Gồm cả Market & Foreign Exit)"
        sector = args.sector or KNOWN_SECTORS.get(symbol, "khac")

        print()
        print("### Chuẩn bị dữ liệu backtest ###")
        print(f"Mã             : {symbol}")
        print(f"Ngành          : {sector}")
        print(f"Khoảng         : {start} -> {end}")
        print(f"Investment mode : {args.investment_mode}")
        print(f"SL/TP mode      : {args.sl_tp_mode}")
        print(f"[MODE] Cơ chế vào lệnh: {entry_mode_str}")
        print(f"[EXIT] Cơ chế thoát lệnh: {exit_mode_str}")
        print(f"[REGIME] Bộ lọc thị trường VNINDEX: {args.market_regime}")
        print(f"[DATA] Số phiên: {len(rows)}")

        roe_vals = [r["snapshot"].get("roe_ttm") for r in rows if r.get("snapshot")]
        valid_roe = [float(v) for v in roe_vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
        de_vals = [r["snapshot"].get("debt_to_equity") for r in rows if r.get("snapshot")]
        valid_de = [float(v) for v in de_vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
        roe_str = f"hợp lệ {len(valid_roe)}/{len(rows)} phiên (avg={sum(valid_roe)/len(valid_roe)*100:.1f}%)" if valid_roe else "THIẾU/NONE 100% phiên"
        de_str = f"hợp lệ {len(valid_de)}/{len(rows)} phiên (avg={sum(valid_de)/len(valid_de):.2f})" if valid_de else "THIẾU/NONE 100% phiên"
        print(f"[DATA INSPECT] ROE TTM: {roe_str} | D/E: {de_str}")

        print()
        print("### Backtest bằng backtesting.engine ###")
        print()
        _print_report(
            symbol,
            start,
            end,
            args.investment_mode,
            args.sl_tp_mode,
            result,
        )

        if args.debug:
            _print_debug(result)
            _print_buy_gate_debug(
                rows,
                result,
                limit=args.debug_buy_limit,
            )

        if args.output_md:
            _export_single_to_md(
                Path(args.output_md),
                symbol,
                start,
                end,
                args,
                result,
            )

        return 0

    # TRƯỜNG HỢP 2: Chạy nhiều mã cùng lúc (Batch Backtest)
    print("\n" + "=" * 80)
    print(f"🚀 BẮT ĐẦU BATCH BACKTEST: {len(target_symbols)} MÃ ({', '.join(target_symbols)})")
    print(f"Khung thời gian: lùi {args.months} tháng | Vốn/mã: {args.initial_cash:,.0f} VND")
    print("=" * 80)

    results_by_sym: dict[str, tuple[str, str, Any]] = {}
    for idx, symbol in enumerate(target_symbols, 1):
        print(f"  [{idx:>2}/{len(target_symbols)}] Đang xử lý {symbol:<5} ... ", end="", flush=True)
        st, en, r_rows, res, err = _run_symbol_pipeline(symbol, args)
        if err:
            print(f"❌ {err}")
            results_by_sym[symbol] = (st, en, None)
        else:
            n_t = len(res.trades)
            pnl_val = res.final_equity - res.initial_cash
            wr_val = res.metrics()["win_rate_pct"]
            print(f"✅ Hoàn tất: {n_t:>2} lệnh | PnL={pnl_val:+12,.0f} VND | WR={wr_val:5.1f}%")
            results_by_sym[symbol] = (st, en, res)

            if args.detail:
                _print_report(
                    symbol,
                    st,
                    en,
                    args.investment_mode,
                    args.sl_tp_mode,
                    res,
                )

    # In bảng tổng kết danh mục
    _print_portfolio_summary_table(results_by_sym, args)
    if args.output_md:
        _export_portfolio_to_md(
            Path(args.output_md),
            results_by_sym,
            args,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
