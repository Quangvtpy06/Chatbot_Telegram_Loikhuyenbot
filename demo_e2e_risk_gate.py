"""Demo end-to-end: Signal Engine → Risk Gate → Notification Service.

Chạy luồng đầy đủ với dữ liệu giả lập, không cần API/Telegram thật.

Chạy:
    cd dnse
    python demo_e2e_risk_gate.py
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Fix UnicodeEncodeError trên Windows terminal
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# Thêm project root vào path
project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from signals.signal_engine import create_signal_engine
from signals.risk_manager import RiskConfig, create_risk_gate
from notifications.service import NotificationService
from storage.repositories import (
    NotificationRepository,
    PositionRepository,
    RiskDecisionRepository,
)

# ---------------------------------------------------------------------------
# Cấu hình
# ---------------------------------------------------------------------------
DB_PATH = project_root / "data" / "analytics" / "risk_gate_e2e.sqlite"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Xóa DB cũ để demo tái lập được
for suffix in ("", "-wal", "-shm"):
    Path(f"{DB_PATH}{suffix}").unlink(missing_ok=True)

risk_config = RiskConfig(
    min_reward_risk_ratio=2.0,
    cooldown_seconds=3_600,
    max_open_positions=10,
    require_stop_loss=True,
    require_take_profit=True,
    allow_short_sell=False,
    require_position_for_hold=False,  # Demo không cần vị thế thật cho HOLD
)

gate = create_risk_gate(DB_PATH, config=risk_config)
pos_repo = PositionRepository(DB_PATH)


# ---------------------------------------------------------------------------
# Fake Telegram sender
# ---------------------------------------------------------------------------

class FakeTelegramSender:
    """Giả lập Telegram — in ra terminal thay vì gửi thật."""

    def __init__(self):
        self.sent_count = 0

    def send(self, recipient: str, message: str) -> str | None:
        self.sent_count += 1
        msg_id = f"tg_msg_{self.sent_count}"
        print(f"  📨 Telegram → {recipient}: {message[:80]}...")
        return msg_id


fake_sender = FakeTelegramSender()
notif_repo = NotificationRepository(DB_PATH)
notif_service = NotificationService(
    repository=notif_repo,
    sender=fake_sender,
    channel="telegram",
)


# ---------------------------------------------------------------------------
# Helper — tạo snapshot giả lập từ DNSE realtime
# ---------------------------------------------------------------------------

def make_snapshot(
        symbol: str,
        realtime_price: float,
        latest_close: float | None = None,
        sma_20: float | None = None,
        rsi_14: float | None = None,
        roe_ttm: float | None = None,
        revenue_growth: float | None = None,
        profit_growth: float | None = None,
        debt_to_equity: float | None = None,
        pe: float | None = None,
        pb: float | None = None,
        eps: float | None = None,
        volume: float | None = None,
        avg_volume_20: float | None = None,
) -> dict:
    """Tạo snapshot giả lập data đã qua analytics."""
    now = datetime.now(timezone.utc)
    return {
        "symbol": symbol,
        "realtime_price": realtime_price,
        "latest_close": latest_close or realtime_price,
        "realtime_as_of": (now - timedelta(seconds=5)).isoformat(),
        "price_as_of": (now - timedelta(seconds=5)).isoformat(),
        "sma_20": sma_20 or realtime_price * 0.95,
        "sma_50": realtime_price * 0.90,
        "ema_12": realtime_price * 0.98,
        "ema_26": realtime_price * 0.95,
        "rsi_14": rsi_14 or 55.0,
        "roe_ttm": roe_ttm or 0.18,
        "revenue_growth": revenue_growth or 12.0,
        "profit_growth": profit_growth or 15.0,
        "debt_to_equity": debt_to_equity or 0.4,
        "pe_ratio": pe or 12.0,
        "pb_ratio": pb or 1.8,
        "eps": eps or 5000.0,
        "volume": volume or 1_500_000,
        "average_volume_20d": avg_volume_20 or 1_200_000,
        "latest_volume": volume or 1_500_000,
        "previous_close": (latest_close or realtime_price) * 0.995,
        "market_id": "STO",
        "price_unit_vnd": 1000.0,
        "board_lot_size": 100,
        "macd": realtime_price * 0.01,
        "macd_signal": realtime_price * 0.005,
        "bb_upper": realtime_price * 1.05,
        "bb_lower": realtime_price * 0.95,
        "bb_middle": realtime_price,
        "atr_14": realtime_price * 0.02,
        "data_status": "OK",
        "signal_status": "ELIGIBLE",
        "indicator_data_complete": True,
        "source_consistent": True,
    }


def make_market(close: float = 1280.0, sma_20: float = 1260.0):
    """Market context (VN-Index)."""
    return {"close": close, "sma_20": sma_20, "symbol": "VNINDEX"}


# ---------------------------------------------------------------------------
# Chạy luồng end-to-end
# ---------------------------------------------------------------------------

print("\n" + "═" * 70)
print("   SIGNAL ENGINE → RISK GATE → NOTIFICATION — End-to-End Demo")
print("═" * 70)

engine = create_signal_engine()

scenarios = [
    {
        "label": "1. FPT — BUY candidate",
        "snapshot": make_snapshot("FPT", 112.5, sma_20=110.0, roe_ttm=0.22,
                                  revenue_growth=15.0, profit_growth=18.0),
        "market": make_market(),
        "sector": "Technology",
    },
    {
        "label": "2. VCB — BUY candidate (Ngân hàng)",
        "snapshot": make_snapshot("VCB", 80.0, sma_20=78.0, roe_ttm=0.20,
                                  revenue_growth=10.0, profit_growth=12.0),
        "market": make_market(),
        "sector": "Financials",
    },
    {
        "label": "3. HPG — snapshot yếu (RSI cao, nợ lớn)",
        "snapshot": make_snapshot("HPG", 30.0, sma_20=28.0, rsi_14=75.0,
                                  roe_ttm=0.08, debt_to_equity=1.5),
        "market": make_market(),
        "sector": "Materials",
    },
    {
        "label": "4. MBB — market dưới SMA20",
        "snapshot": make_snapshot("MBB", 25.0, sma_20=24.0, roe_ttm=0.16),
        "market": make_market(close=1200, sma_20=1250),  # VN-Index dưới SMA20
        "sector": "Financials",
    },
]

RECIPIENT = "chat_demo_123"

for sc in scenarios:
    print(f"\n{'─' * 70}")
    print(f"  📊 {sc['label']}")
    print(f"{'─' * 70}")

    # Bước 1: Signal Engine sinh tín hiệu
    signal = engine.generate(
        sc["snapshot"],
        market=sc["market"],
        foreign_sessions=[
            {"net_volume": 100_000, "as_of": sc["snapshot"]["price_as_of"]},
            {"net_volume": 120_000, "as_of": sc["snapshot"]["price_as_of"]},
            {"net_volume": 80_000, "as_of": sc["snapshot"]["price_as_of"]},
            {"net_volume": 50_000, "as_of": sc["snapshot"]["price_as_of"]},
            {"net_volume": 200_000, "as_of": sc["snapshot"]["price_as_of"]},
        ],
        sector=sc.get("sector"),
    )
    action_icon = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡", "NO SIGNAL": "⚪"}.get(signal.action, "?")
    print(f"  Signal Engine → {action_icon} {signal.action} {signal.symbol}")
    if signal.reasons:
        for r in signal.reasons[:3]:
            print(f"    • {r}")

    # Bước 2: Risk Gate đánh giá
    if signal.action == "NO SIGNAL" or signal.signal_status != "ELIGIBLE":
        print(f"  Risk Gate    → ⏭️ SKIP (action={signal.action}, status={signal.signal_status})")
        decision = gate.evaluate(signal)
        print(f"  Risk Gate    → ❌ BLOCK | {decision.block_reasons[0] if decision.block_reasons else '—'}")
        continue

    decision = gate.evaluate(signal)
    verdict_icon = "✅ ALLOW" if decision.is_allowed else "❌ BLOCK"
    print(f"  Risk Gate    → {verdict_icon}")
    if decision.is_allowed:
        if decision.reward_risk_ratio:
            print(f"    R:R = {decision.reward_risk_ratio:.2f}")
        if decision.position_size_pct:
            print(f"    Position size = {decision.position_size_pct:.1%}")
    else:
        for r in decision.block_reasons:
            print(f"    🚫 {r}")

    # Bước 3: Notification Service
    result = notif_service.dispatch(signal, decision, recipient=RECIPIENT)
    if result.delivered:
        print(f"  Notification → 📨 Đã gửi (msg_id={result.message_id})")
    else:
        print(f"  Notification → ⏭️ Bỏ qua ({result.reason})")

# ---------------------------------------------------------------------------
# Thống kê
# ---------------------------------------------------------------------------

repo = RiskDecisionRepository(DB_PATH)
counts = repo.count_by_verdict()
recent = repo.get_recent(limit=10)

print(f"\n{'═' * 70}")
print(f"   📊 THỐNG KÊ RISK DECISIONS")
print(f"{'═' * 70}")
print(f"  ✅ ALLOW : {counts.get('ALLOW', 0)}")
print(f"  ❌ BLOCK : {counts.get('BLOCK', 0)}")
print(f"  📨 Telegram đã gửi: {fake_sender.sent_count}")

print(f"\n  Danh sách quyết định:")
for d in recent:
    icon = "✅" if d.verdict == "ALLOW" else "❌"
    rr_str = f" R:R={d.reward_risk_ratio:.2f}" if d.reward_risk_ratio else ""
    print(f"  {icon} [{d.symbol:6}] {d.action:10} → {d.verdict}{rr_str}")

print(f"\n  📁 DB lưu tại: {DB_PATH.resolve()}\n")
