"""Backtest theo ngày, tách tín hiệu khỏi thời điểm khớp lệnh."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from signals.models import SignalEvent
from signals.signal_engine import SignalEngine, create_signal_engine


@dataclass(frozen=True)
class BacktestConfig:
    """Các giả định thực thi được công khai để kết quả tái lập."""

    initial_cash: float = 100_000_000.0
    position_size_pct: float = 0.10
    fee_bps: float = 15.0
    slippage_bps: float = 5.0

    def __post_init__(self) -> None:
        if self.initial_cash <= 0 or not math.isfinite(self.initial_cash):
            raise ValueError("initial_cash phải là số dương hữu hạn")
        if not 0 < self.position_size_pct <= 1:
            raise ValueError("position_size_pct phải nằm trong (0, 1]")
        if self.fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("fee_bps và slippage_bps không được âm")


@dataclass(frozen=True)
class BacktestTrade:
    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    return_pct: float
    exit_trigger: str


@dataclass
class BacktestResult:
    initial_cash: float
    final_equity: float
    equity_curve: list[dict[str, float | str]] = field(default_factory=list)
    trades: list[BacktestTrade] = field(default_factory=list)
    signals: list[SignalEvent] = field(default_factory=list)

    @property
    def total_return_pct(self) -> float:
        return (self.final_equity / self.initial_cash - 1) * 100

    @property
    def max_drawdown_pct(self) -> float:
        peak = self.initial_cash
        drawdown = 0.0
        for point in self.equity_curve:
            equity = float(point["equity"])
            peak = max(peak, equity)
            if peak:
                drawdown = min(drawdown, (equity / peak - 1) * 100)
        return drawdown

    @property
    def win_rate_pct(self) -> float:
        if not self.trades:
            return 0.0
        return sum(trade.pnl > 0 for trade in self.trades) / len(self.trades) * 100

    def metrics(self) -> dict[str, float]:
        wins = [trade.pnl for trade in self.trades if trade.pnl > 0]
        losses = [-trade.pnl for trade in self.trades if trade.pnl < 0]
        gross_loss = sum(losses)
        return {
            "total_return_pct": self.total_return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "win_rate_pct": self.win_rate_pct,
            "expectancy": (
                sum(trade.pnl for trade in self.trades) / len(self.trades)
                if self.trades
                else 0.0
            ),
            "profit_factor": sum(wins) / gross_loss if gross_loss else float("inf"),
            "trade_count": float(len(self.trades)),
        }


def _number(row: Mapping[str, Any], key: str) -> float:
    value = row.get(key)
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Thiếu giá trị số hợp lệ: {key}") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{key} phải là số dương hữu hạn")
    return result


def _execution_price(price: float, side: str, config: BacktestConfig) -> float:
    slippage = config.slippage_bps / 10_000
    return price * (1 + slippage if side == "BUY" else 1 - slippage)


def _with_costs(gross: float, notional: float, config: BacktestConfig) -> float:
    return gross - notional * config.fee_bps / 10_000


def run_backtest(
    rows: Iterable[Mapping[str, Any]],
    *,
    config: BacktestConfig | None = None,
    engine: SignalEngine | None = None,
) -> BacktestResult:
    """Chạy backtest từ snapshot đã chuẩn hóa.

    Mỗi row phải có ``date``, ``open``, ``high``, ``low``, ``close`` và các
    trường snapshot cần cho chiến lược. BUY được khớp ở open phiên kế tiếp;
    SELL trong cùng phiên dùng low/high chỉ để xác định trigger và khớp tại
    mức stop/take-profit. Vì vậy không dùng close tương lai để tạo tín hiệu.
    """

    bars = list(rows)
    if not bars:
        raise ValueError("Backtest cần ít nhất một phiên")
    config = config or BacktestConfig()
    engine = engine or create_signal_engine()
    cash = config.initial_cash
    position: dict[str, Any] | None = None
    pending_buy: SignalEvent | None = None
    result = BacktestResult(config.initial_cash, config.initial_cash)

    for index, row in enumerate(bars):
        date = str(row.get("date") or row.get("data_as_of") or "")
        if not date:
            raise ValueError("Mỗi phiên backtest cần date hoặc data_as_of")
        open_price = _number(row, "open")
        high = _number(row, "high")
        low = _number(row, "low")
        close = _number(row, "close")
        symbol = str(row.get("symbol") or "UNKNOWN").upper()

        if pending_buy is not None and position is None:
            entry_price = _execution_price(open_price, "BUY", config)
            quantity = cash * config.position_size_pct / entry_price
            cash -= quantity * entry_price
            position = {
                "symbol": symbol,
                "entry_date": date,
                "entry_price": entry_price,
                "quantity": quantity,
            }
            pending_buy = None

        snapshot = dict(row.get("snapshot") or row)
        snapshot.setdefault("symbol", symbol)
        snapshot.setdefault("latest_close", close)
        snapshot.setdefault("realtime_price", close)
        snapshot.setdefault("realtime_as_of", date)
        snapshot.setdefault("realtime_age_minutes", 0)
        foreign_sessions = row.get("foreign_sessions") or snapshot.pop(
            "foreign_sessions", ()
        )
        market = row.get("market") or snapshot.pop("market", None)
        if position is not None:
            signal = engine.generate(
                snapshot,
                market=market,
                foreign_sessions=foreign_sessions,
                position={
                    "has_position": True,
                    "entry_price": position["entry_price"],
                },
                session_low=low,
                session_high=high,
                sector=row.get("sector"),
            )
            result.signals.append(signal)
            if signal.action == "SELL":
                trigger = str(signal.metadata.get("sell_trigger") or "SIGNAL")
                if trigger == "STOP_LOSS" and signal.stop_loss is not None:
                    exit_price = signal.stop_loss
                elif trigger == "TAKE_PROFIT" and signal.take_profit is not None:
                    exit_price = signal.take_profit
                else:
                    exit_price = close
                exit_price = _execution_price(exit_price, "SELL", config)
                notional = position["quantity"] * exit_price
                gross = position["quantity"] * (exit_price - position["entry_price"])
                pnl = _with_costs(gross, notional, config)
                cash += notional - notional * config.fee_bps / 10_000
                result.trades.append(
                    BacktestTrade(
                        symbol=position["symbol"],
                        entry_date=position["entry_date"],
                        exit_date=date,
                        entry_price=position["entry_price"],
                        exit_price=exit_price,
                        quantity=position["quantity"],
                        pnl=pnl,
                        return_pct=pnl
                        / (position["entry_price"] * position["quantity"])
                        * 100,
                        exit_trigger=trigger,
                    )
                )
                position = None
        else:
            signal = engine.generate(
                snapshot,
                market=market,
                foreign_sessions=foreign_sessions,
                sector=row.get("sector"),
            )
            result.signals.append(signal)
            if signal.action == "BUY" and index + 1 < len(bars):
                pending_buy = signal

        equity = cash
        if position is not None:
            equity += position["quantity"] * close
        result.equity_curve.append({"date": date, "equity": equity})

    result.final_equity = float(result.equity_curve[-1]["equity"])
    return result
