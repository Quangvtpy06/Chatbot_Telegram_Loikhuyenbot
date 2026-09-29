"""Danh mục cổ phiếu xác thực từ DNSE, lưu cache trong bộ nhớ."""

import threading
import time

from .dnse_api_crawl import DNSEClient, DNSEConfig

_lock = threading.Lock()
_symbols: frozenset[str] = frozenset()
_expires_at = 0.0


def equity_symbols() -> frozenset[str]:
    """Không cho đồng bộ khi chưa xác thực được danh mục hoặc cache hết hạn."""
    global _symbols, _expires_at
    with _lock:
        if _symbols and time.monotonic() < _expires_at:
            return _symbols
        client = DNSEClient(DNSEConfig())
        try:
            symbols = frozenset(client.list_symbols())
            if not symbols:
                raise RuntimeError("DNSE trả về danh mục cổ phiếu rỗng")
            _symbols = symbols
            _expires_at = time.monotonic() + 900
            return _symbols
        finally:
            client.close()


def require_equity(symbol: str) -> None:
    """Chặn mã không có trong danh mục cổ phiếu trước khi gọi API dữ liệu."""
    if symbol not in equity_symbols():
        raise ValueError(f"Không tồn tại mã cổ phiếu {symbol} trong danh mục DNSE.")
