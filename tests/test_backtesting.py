from __future__ import annotations

from backtesting import BacktestConfig, run_backtest


def _row(date: str, close: float, low: float, high: float) -> dict:
    return {
        "date": date,
        "symbol": "FPT",
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "data_status": "OK",
        "signal_status": "ELIGIBLE",
        "sma_20": close - 2,
        "sma_50": close - 4,
        "rsi_14": 60,
        "macd": 2,
        "macd_signal": 1,
        "roe_ttm": 0.20,
        "debt_to_equity": 0.5,
        "market": {"close": 1100, "sma_20": 1000},
        "foreign_sessions": [{"net_volume": 100}] * 5,
    }


def test_buy_is_executed_on_next_session_open() -> None:
    next_day = _row("2026-09-22", 120, 119, 121)
    next_day["open"] = 110
    result = run_backtest(
        [_row("2026-09-21", 100, 99, 101), next_day],
        config=BacktestConfig(fee_bps=0, slippage_bps=0),
    )

    assert result.signals[0].action == "BUY"
    assert not result.trades
    assert result.equity_curve[-1]["equity"] > 100_000_000


def test_stop_loss_has_priority_over_take_profit() -> None:
    rows = [
        _row("2026-09-21", 100, 99, 101),
        _row("2026-09-22", 100, 94, 111),
        _row("2026-09-23", 100, 99, 101),
    ]
    result = run_backtest(
        rows, config=BacktestConfig(fee_bps=0, slippage_bps=0)
    )

    assert result.trades
    assert result.trades[0].exit_trigger == "STOP_LOSS"
    assert result.trades[0].exit_price == 95
