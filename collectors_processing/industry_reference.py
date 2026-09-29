"""Đồng bộ phân ngành toàn thị trường theo schema VCI dùng trong vnstock."""

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

LOGGER = logging.getLogger(__name__)
_lock = threading.Lock()


def refresh_industries(data_dir: Path, use_system_proxy: bool = False) -> None:
    """Lưu phân ngành từ nguồn thật; lỗi API không ghi đè dữ liệu đang có."""
    path = data_dir / "reference" / "industries.csv"
    with _lock:
        if path.is_file() and time.time() - path.stat().st_mtime < 86400:
            return
        try:
            with requests.Session() as session:
                session.trust_env = use_system_proxy
                response = session.get(
                    "https://iq.vietcap.com.vn/api/iq-insight-service/v2/company/search-bar",
                    params={"language": "1"}, timeout=20,
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                             "Origin": "https://trading.vietcap.com.vn", "Referer": "https://trading.vietcap.com.vn/"},
                )
                response.raise_for_status()
                companies = response.json()["data"]
            rows = []
            for company in companies:
                industry = next((company.get(f"icbLv{level}") for level in (3, 4, 2, 1)
                                 if (company.get(f"icbLv{level}") or {}).get("name")), None)
                if company.get("code") and industry:
                    rows.append({"symbol": str(company["code"]).upper(), "sector": industry["name"],
                                 "industry_code": industry.get("code"), "source": "VCI",
                                 "collected_at": datetime.now(timezone.utc).isoformat()})
            if not rows:
                raise ValueError("Nguồn VCI chưa trả phân ngành hợp lệ")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            pd.DataFrame(rows).drop_duplicates("symbol").to_csv(temporary, index=False, encoding="utf-8-sig")
            temporary.replace(path)
        except (requests.RequestException, ValueError, KeyError, TypeError, OSError):
            LOGGER.exception("Chưa cập nhật được phân ngành; giữ dữ liệu cũ nếu có")
