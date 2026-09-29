"""Ánh xạ trường cơ bản dùng chung cho dữ liệu cũ và dữ liệu mới thu thập."""

import numpy as np
import pandas as pd

FUNDAMENTAL_ALIASES = {
    "pe": ("pe_ratio",),
    "pb": ("pb_ratio",),
    "ps": ("ps_ratio",),
    "eps": ("eps_basic_vnd", "earnings_per_share_vnd", "earning_per_share_vnd"),
    "eps_diluted": ("eps_diluted_vnd", "diluted_earnings_per_share"),
    "eps_ttm": ("trailing_eps",),
    "ev_to_ebitda": ("ev_ebitda",),
}


def normalize_fundamental_fields(frame: pd.DataFrame) -> pd.DataFrame:
    """Bù từng ô thiếu từ alias, giữ nguyên số liệu chuẩn đã có và đơn vị nguồn."""
    result = frame.copy()
    for target, aliases in FUNDAMENTAL_ALIASES.items():
        values = pd.Series(np.nan, index=result.index, dtype=float)
        for column in (target, *aliases):
            if column in result:
                numeric = pd.to_numeric(result[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
                values = values.fillna(numeric)
        result[target] = values
    return result
