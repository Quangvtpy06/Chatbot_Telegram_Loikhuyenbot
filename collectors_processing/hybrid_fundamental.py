"""Module BCTC Hybrid: Kết hợp vnfinancialdata (nạp nền lịch sử nhiều năm) và vnstock (bù đắp quý gần nhất)."""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_data_path(value: str | Path) -> Path:
    """Neo đường dẫn tương đối vào thư mục gốc ``dnse`` thay vì thư mục chạy."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        parts = list(path.parts)
        if parts and parts[0].casefold() == PROJECT_ROOT.name.casefold():
            parts = parts[1:]
        path = PROJECT_ROOT.joinpath(*parts)
    return path.resolve()


# Cache danh sách mã theo sàn từ vnfinancialdata
_VFD_TICKERS_CACHE: dict[str, set[str]] = {}


def get_vfd_tickers_by_exchange(exchange: str) -> set[str]:
    """Lấy danh sách mã chứng khoán theo sàn từ vnfinancialdata (có cache)."""
    ex_upper = exchange.upper()
    if ex_upper in _VFD_TICKERS_CACHE:
        return _VFD_TICKERS_CACHE[ex_upper]

    try:
        import vnfinancialdata as vfd
        df = vfd.load(exchange=ex_upper, statement="income_statement")
        tickers = set(df["ticker"].dropna().str.strip().str.upper().unique())
        _VFD_TICKERS_CACHE[ex_upper] = tickers
        return tickers
    except Exception as exc:
        LOGGER.warning("Không thể lấy danh sách tickers từ vnfinancialdata cho sàn %s: %s", ex_upper, exc)
        return set()


def detect_symbol_exchange(symbol: str) -> str:
    """Tự động phát hiện sàn giao dịch của mã: HSX (HOSE), HNX, hoặc UPCOM."""
    sym = symbol.strip().upper()
    hsx_tickers = get_vfd_tickers_by_exchange("HSX")
    if sym in hsx_tickers:
        return "HSX"

    hnx_tickers = get_vfd_tickers_by_exchange("HNX")
    if sym in hnx_tickers:
        return "HNX"

    return "UPCOM"


def fetch_historical_fundamentals_vfd(
        symbol: str, exchange: str = "HSX", years_limit: int = 3
) -> dict[str, pd.DataFrame] | None:
    """Nạp dữ liệu BCTC lịch sử theo năm từ vnfinancialdata cho một mã (mặc định 3 năm gần nhất).

    Trả về dict gồm:
    - "fundamentals": DataFrame tổng hợp (wide-format) chuẩn hóa
    - "income": DataFrame kết quả kinh doanh
    - "balance": DataFrame bảng cân đối kế toán
    - "cashflow": DataFrame lưu chuyển tiền tệ
    """
    sym = symbol.strip().upper()
    ex = exchange.strip().upper()
    if ex not in ("HSX", "HNX"):
        LOGGER.info("vnfinancialdata không hỗ trợ sàn %s cho mã %s", ex, sym)
        return None

    try:
        import vnfinancialdata as vfd
    except ImportError:
        LOGGER.error("Chưa cài đặt vnfinancialdata.")
        return None

    dfs: dict[str, pd.DataFrame] = {}
    statement_keys = {
        "income_statement": "income",
        "balance_sheet": "balance",
        "cash_flow": "cashflow",
    }

    for vfd_stmt, key in statement_keys.items():
        try:
            raw_df = vfd.get(ticker=sym, statement=vfd_stmt, exchange=ex)
            if raw_df is not None and not raw_df.empty and "item_name" in raw_df.columns:
                piv = raw_df.pivot_table(
                    index="year",
                    columns="item_name",
                    values="value",
                    aggfunc="first",
                )
                dfs[key] = piv
        except Exception as exc:
            LOGGER.debug("Không thể lấy %s cho %s: %s", vfd_stmt, sym, exc)

    if not dfs or all(df.empty for df in dfs.values()):
        return None

    # Hợp nhất theo năm (chỉ lấy 3 năm gần nhất)
    all_years = sorted(set().union(*[p.index for p in dfs.values() if not p.empty]))
    if not all_years:
        return None
    if years_limit and years_limit > 0:
        all_years = all_years[-years_limit:]

    merged_wide = pd.DataFrame(index=all_years)
    for key, p in dfs.items():
        if not p.empty:
            filtered_p = p.loc[p.index.isin(all_years)]
            dfs[key] = filtered_p
            merged_wide = merged_wide.join(filtered_p, how="left", rsuffix=f"_{key}")

    # Chuẩn hóa các cột cốt lõi
    res = pd.DataFrame(index=merged_wide.index)
    res["symbol"] = sym
    res["period_type"] = "year"
    res["period"] = res.index.astype(str)
    res["report_period"] = res["period"]
    res["year"] = res.index.astype(int)
    res["source"] = "VNFINANCIALDATA"
    res["collected_at"] = datetime.now(timezone.utc).isoformat()

    mapping = {
        "revenue": [
            "Doanh số thuần",
            "Doanh số",
            "Thu nhập lãi và các khoản thu nhập tương tự",
            "Thu nhập lãi thuần",
            "Tổng thu nhập hoạt động",
            "Doanh thu hoạt động",
        ],
        "net_sales": [
            "Doanh số thuần",
            "Doanh số",
            "Thu nhập lãi và các khoản thu nhập tương tự",
            "Thu nhập lãi thuần",
            "Tổng thu nhập hoạt động",
            "Doanh thu hoạt động",
        ],
        "cost_of_goods_sold": ["Giá vốn hàng bán"],
        "gross_profit": ["Lãi gộp"],
        "selling_expenses": ["Chi phí bán hàng"],
        "general_and_admin_expenses": ["Chi phí quản lý doanh nghiệp"],
        "financial_expenses": ["Chi phí tài chính"],
        "interest_expenses": ["Trong đó: Chi phí lãi vay"],
        "profit_before_tax": ["Lãi/(lỗ) ròng trước thuế", "Tổng lợi nhuận trước thuế"],
        "net_profit": [
            "Lãi/(lỗ) thuần sau thuế",
            "Lợi nhuận sau thuế",
            "Lợi nhuận của Cổ đông của Công ty mẹ",
            "Cổ đông của Công ty mẹ",
            "Lợi nhuận sau thuế của cổ đông ngân hàng mẹ",
        ],
        "net_profit_loss_after_tax": [
            "Lãi/(lỗ) thuần sau thuế",
            "Lợi nhuận sau thuế",
            "Lợi nhuận của Cổ đông của Công ty mẹ",
            "Cổ đông của Công ty mẹ",
            "Lợi nhuận sau thuế của cổ đông ngân hàng mẹ",
        ],
        "attributable_to_parent_company": [
            "Lợi nhuận của Cổ đông của Công ty mẹ",
            "Cổ đông của Công ty mẹ",
            "Lợi nhuận sau thuế của cổ đông ngân hàng mẹ",
        ],
        "ebit": ["EBIT"],
        "ebitda": ["EBITDA"],
        "eps": ["Lãi cơ bản trên cổ phiếu"],
        "eps_basic_vnd": ["Lãi cơ bản trên cổ phiếu"],
        "eps_diluted": ["Lãi trên cổ phiếu pha loãng"],
        "eps_diluted_vnd": ["Lãi trên cổ phiếu pha loãng"],
        "total_assets": ["TỔNG TÀI SẢN"],
        "current_assets": ["TÀI SẢN NGẮN HẠN"],
        "non_current_assets": ["TÀI SẢN DÀI HẠN"],
        "owners_equity": [
            "VỐN CHỦ SỞ HỮU",
            "Vốn chủ sở hữu",
            "VỐN CHỦ SỞ HỮU VÀ CÁC QUỸ",
            "Vốn và các quỹ",
        ],
        "equity": [
            "VỐN CHỦ SỞ HỮU",
            "Vốn chủ sở hữu",
            "VỐN CHỦ SỞ HỮU VÀ CÁC QUỸ",
            "Vốn và các quỹ",
        ],
        "total_liabilities": ["NỢ PHẢI TRẢ", "Tổng nợ phải trả"],
        "liabilities": ["NỢ PHẢI TRẢ", "Tổng nợ phải trả"],
        "debt_to_equity": ["debt_to_equity"],
        "short_term_liabilities": ["Nợ ngắn hạn"],
        "long_term_liabilities": ["Nợ dài hạn"],
        "cash_and_cash_equivalents": ["Tiền và các khoản tương đương tiền"],
    }

    for target_col, src_cols in mapping.items():
        for sc in src_cols:
            if sc in merged_wide.columns:
                res[target_col] = merged_wide[sc]
                break

    # Tính toán các tỷ số tài chính dẫn xuất
    if "net_profit" in res.columns and "owners_equity" in res.columns:
        res["roe"] = res["net_profit"] / res["owners_equity"].replace(0, np.nan)
    if "net_profit" in res.columns and "total_assets" in res.columns:
        res["roa"] = res["net_profit"] / res["total_assets"].replace(0, np.nan)
    if "total_liabilities" in res.columns and "owners_equity" in res.columns:
        res["debt_to_equity"] = res["total_liabilities"] / res["owners_equity"].replace(0, np.nan)
        res["debtPerEquity"] = res["debt_to_equity"]
    if "gross_profit" in res.columns and "revenue" in res.columns:
        res["gross_margin"] = res["gross_profit"] / res["revenue"].replace(0, np.nan)
    if "net_profit" in res.columns and "revenue" in res.columns:
        res["net_margin"] = res["net_profit"] / res["revenue"].replace(0, np.nan)

    # Sắp xếp năm giảm dần (năm mới nhất lên đầu) khớp chuẩn vnstock
    res = res.sort_index(ascending=False).reset_index(drop=True)

    result_dict: dict[str, pd.DataFrame] = {"fundamentals": res}
    for key, p in dfs.items():
        if not p.empty:
            df_stmt = p.copy()
            df_stmt.insert(0, "period_type", "year")
            df_stmt.insert(0, "symbol", sym)
            df_stmt.insert(2, "source", "VNFINANCIALDATA")
            df_stmt.insert(3, "statement", key)
            df_stmt.index.name = "period"
            df_stmt = df_stmt.reset_index().sort_values("period", ascending=False)
            result_dict[key] = df_stmt

    return result_dict


def save_vfd_symbol_data(symbol: str, output_dir: Path, exchange: str = "HSX", years_limit: int = 3) -> bool:
    """Nạp BCTC lịch sử 3 năm gần nhất từ vnfinancialdata và lưu vào data/fundamental/year/."""
    output_dir = _resolve_data_path(output_dir)
    data = fetch_historical_fundamentals_vfd(symbol, exchange, years_limit=years_limit)
    if not data or "fundamentals" not in data or data["fundamentals"].empty:
        return False

    year_dir = output_dir / "fundamental" / "year"
    year_dir.mkdir(parents=True, exist_ok=True)

    sym = symbol.strip().upper()
    for key, df in data.items():
        filename = f"{sym}_fundamentals.csv" if key == "fundamentals" else f"{sym}_{key}.csv"
        csv_path = year_dir / filename
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    LOGGER.info("Đã nạp lịch sử BCTC từ vnfinancialdata cho %s (%s dòng)", sym, len(data["fundamentals"]))
    return True


def _has_minimum_quarter_periods(path: Path, minimum: int) -> bool:
    """Kiểm tra file đã có đủ số quý riêng biệt hợp lệ hay chưa."""

    if not path.is_file():
        return False
    try:
        frame = pd.read_csv(path, usecols=["period"])
    except (OSError, ValueError, pd.errors.ParserError):
        return False
    periods = frame["period"].astype("string").str.strip()
    return int(periods[periods.str.fullmatch(r"\d{4}-Q[1-4]", na=False)].nunique()) >= minimum


def sync_symbol_fundamentals_hybrid(
        symbol: str,
        output_dir: Path,
        years_limit: int = 3,
        period_limit_quarter: int = 5,
        delay_seconds: float = 0.2,
        use_system_proxy: bool = False,
) -> None:
    """Mô hình Hybrid: Nạp lịch sử 3 năm gần nhất từ vnfinancialdata và bù đắp các quý gần nhất từ vnstock.

    1. Kiểm tra data/fundamental/year/{symbol}_fundamentals.csv:
       - Nếu chưa có: tải 3 năm gần nhất từ vnfinancialdata (HSX/HNX).
       - Nếu là UPCOM hoặc vnfinancialdata không có: fallback sang vnstock cào 3 năm.
    2. Kiểm tra data/fundamental/quarter/{symbol}_fundamentals.csv:
       - Nếu chưa đủ dữ liệu: cào tối thiểu 5 quý gần nhất bằng vnstock.
    """
    output_dir = _resolve_data_path(output_dir)
    sym = symbol.strip().upper()
    period_limit_quarter = max(5, period_limit_quarter)
    year_file = output_dir / "fundamental" / "year" / f"{sym}_fundamentals.csv"
    quarter_file = output_dir / "fundamental" / "quarter" / f"{sym}_fundamentals.csv"

    # LƯỢT 1: Nạp lịch sử theo năm (3 năm gần nhất)
    if not year_file.is_file():
        exchange = detect_symbol_exchange(sym)
        loaded_vfd = False
        if exchange in ("HSX", "HNX"):
            loaded_vfd = save_vfd_symbol_data(sym, output_dir, exchange, years_limit=years_limit)

        if not loaded_vfd:
            LOGGER.info("Fallback sang vnstock để nạp BCTC theo năm cho %s (%s)", sym, exchange)
            try:
                from .dnse_api_crawl import crawl_fundamentals
            except ImportError:
                from dnse.collectors_processing.dnse_api_crawl import crawl_fundamentals

            try:
                crawl_fundamentals(
                    symbols=[sym],
                    output_dir=output_dir,
                    periods=("year",),
                    reports=("ratio", "income", "balance"),
                    source="KBS",
                    fallback_source="VCI",
                    period_limit=years_limit,
                    delay_seconds=delay_seconds,
                    use_system_proxy=use_system_proxy,
                )
            except Exception as exc:
                LOGGER.error("Lỗi khi fallback vnstock cào year cho %s: %s", sym, exc)

    # LƯỢT 2: Bù đắp ít nhất 5 quý để ROE TTM có vốn chủ đầu kỳ và cuối kỳ.
    if not _has_minimum_quarter_periods(quarter_file, period_limit_quarter):
        try:
            from .dnse_api_crawl import crawl_fundamentals
        except ImportError:
            from dnse.collectors_processing.dnse_api_crawl import crawl_fundamentals

        try:
            crawl_fundamentals(
                symbols=[sym],
                output_dir=output_dir,
                periods=("quarter",),
                reports=("ratio", "income", "balance"),
                source="KBS",
                fallback_source=None,
                period_limit=period_limit_quarter,
                delay_seconds=delay_seconds,
                use_system_proxy=use_system_proxy,
            )
            LOGGER.info("Đã bù đắp BCTC các quý gần nhất qua vnstock cho %s", sym)
        except Exception as exc:
            LOGGER.error("Lỗi khi cào bù đắp quý qua vnstock cho %s: %s", sym, exc)


def preload_all_market_history(
        output_dir: Path,
        exchanges: tuple[str, ...] = ("HSX", "HNX"),
        years_limit: int = 3,
) -> dict[str, int]:
    """Nạp hàng loạt BCTC lịch sử 3 năm gần nhất cho TOÀN BỘ các mã trên HSX và HNX.

    Sử dụng vnfinancialdata nạp trực tiếp dataset Parquet (vài giây, 100% không bị chặn IP).
    """
    output_dir = _resolve_data_path(output_dir)
    try:
        import vnfinancialdata as vfd
    except ImportError:
        LOGGER.error("Chưa cài đặt vnfinancialdata.")
        return {}

    year_dir = output_dir / "fundamental" / "year"
    year_dir.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    screening_rows: list[dict[str, Any]] = []

    for ex in exchanges:
        LOGGER.info("Bắt đầu nạp toàn bộ dataset sàn %s từ vnfinancialdata...", ex)
        try:
            df_inc = vfd.load(exchange=ex, statement="income_statement")
            df_bal = vfd.load(exchange=ex, statement="balance_sheet")
            df_cf = vfd.load(exchange=ex, statement="cash_flow")
        except Exception as exc:
            LOGGER.error("Lỗi khi tải dataset %s: %s", ex, exc)
            continue

        tickers = sorted(df_inc["ticker"].dropna().unique())
        LOGGER.info("Sàn %s có %s mã cổ phiếu. Đang xử lý...", ex, len(tickers))

        piv_inc = df_inc.pivot_table(index=["ticker", "year"], columns="item_name", values="value", aggfunc="first")
        piv_bal = df_bal.pivot_table(index=["ticker", "year"], columns="item_name", values="value", aggfunc="first")
        piv_cf = df_cf.pivot_table(index=["ticker", "year"], columns="item_name", values="value", aggfunc="first")

        merged = piv_inc.join(piv_bal, how="outer", rsuffix="_balance").join(piv_cf, how="outer", rsuffix="_cashflow")

        for sym in tickers:
            if sym not in merged.index.levels[0]:
                continue
            sym_data = merged.loc[sym].dropna(how="all")
            if sym_data.empty:
                continue
            if years_limit and years_limit > 0:
                recent_years = sorted(sym_data.index)[-years_limit:]
                sym_data = sym_data.loc[sym_data.index.isin(recent_years)]
                if sym_data.empty:
                    continue

            res = pd.DataFrame(index=sym_data.index)
            res["symbol"] = sym
            res["period_type"] = "year"
            res["period"] = res.index.astype(str)
            res["report_period"] = res["period"]
            res["year"] = res.index.astype(int)
            res["source"] = "VNFINANCIALDATA"
            res["collected_at"] = datetime.now(timezone.utc).isoformat()

            # Mapping
            revenue_cols = [
                "Doanh số thuần",
                "Doanh số",
                "Thu nhập lãi và các khoản thu nhập tương tự",
                "Thu nhập lãi thuần",
                "Tổng thu nhập hoạt động",
                "Doanh thu hoạt động",
            ]
            for c in revenue_cols:
                if c in sym_data.columns:
                    res["revenue"] = sym_data[c]
                    res["net_sales"] = sym_data[c]
                    break

            profit_cols = [
                "Lãi/(lỗ) thuần sau thuế",
                "Lợi nhuận sau thuế",
                "Lợi nhuận của Cổ đông của Công ty mẹ",
                "Cổ đông của Công ty mẹ",
                "Lợi nhuận sau thuế của cổ đông ngân hàng mẹ",
            ]
            for c in profit_cols:
                if c in sym_data.columns:
                    res["net_profit"] = sym_data[c]
                    res["net_profit_loss_after_tax"] = sym_data[c]
                    break

            equity_cols = ["VỐN CHỦ SỞ HỮU", "Vốn chủ sở hữu", "VỐN CHỦ SỞ HỮU VÀ CÁC QUỸ", "Vốn và các quỹ"]
            for c in equity_cols:
                if c in sym_data.columns:
                    res["owners_equity"] = sym_data[c]
                    res["equity"] = sym_data[c]
                    break

            if "TỔNG TÀI SẢN" in sym_data.columns:
                res["total_assets"] = sym_data["TỔNG TÀI SẢN"]

            liability_cols = ["NỢ PHẢI TRẢ", "Tổng nợ phải trả"]
            for c in liability_cols:
                if c in sym_data.columns:
                    res["total_liabilities"] = sym_data[c]
                    res["liabilities"] = sym_data[c]
                    break

            if "EBIT" in sym_data.columns:
                res["ebit"] = sym_data["EBIT"]
            if "EBITDA" in sym_data.columns:
                res["ebitda"] = sym_data["EBITDA"]
            if "Lãi cơ bản trên cổ phiếu" in sym_data.columns:
                res["eps_basic_vnd"] = sym_data["Lãi cơ bản trên cổ phiếu"]

            # Ratios
            if "net_profit" in res.columns and "owners_equity" in res.columns:
                res["roe"] = res["net_profit"] / res["owners_equity"].replace(0, np.nan)
            if "net_profit" in res.columns and "total_assets" in res.columns:
                res["roa"] = res["net_profit"] / res["total_assets"].replace(0, np.nan)
            if "total_liabilities" in res.columns and "owners_equity" in res.columns:
                res["debt_to_equity"] = res["total_liabilities"] / res["owners_equity"].replace(0, np.nan)

            res = res.sort_index(ascending=False).reset_index(drop=True)
            res.to_csv(year_dir / f"{sym}_fundamentals.csv", index=False, encoding="utf-8-sig")
            if not res.empty:
                latest_row = res.iloc[0].to_dict()
                screening_rows.append(latest_row)

        counts[ex] = len(tickers)
        LOGGER.info("Hoàn tất nạp BCTC lịch sử sàn %s (%s mã)", ex, len(tickers))

    if screening_rows:
        screening_df = pd.DataFrame(screening_rows)
        screening_path = output_dir / "fundamental" / "screening_year.csv"
        screening_df.to_csv(screening_path, index=False, encoding="utf-8-sig")

    return counts


def main() -> None:
    """CLI thực thi nạp dữ liệu BCTC Hybrid."""
    import argparse

    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )

    parser = argparse.ArgumentParser(description="Mô hình BCTC Hybrid (vnfinancialdata + vnstock)")
    parser.add_argument("--preload-all", action="store_true", help="Nạp sẵn lịch sử toàn bộ thị trường (HSX, HNX)")
    parser.add_argument("--symbol", type=str, help="Đồng bộ một mã cổ phiếu cụ thể (VD: FPT)")
    parser.add_argument("--output-dir", type=_resolve_data_path, default=PROJECT_ROOT / "data",
                        help="Thư mục data gốc")
    parser.add_argument("--period-limit", type=int, default=5, help="Số quý gần nhất cào từ vnstock (tối thiểu 5)")

    args = parser.parse_args()

    if args.preload_all:
        LOGGER.info("Bắt đầu nạp sẵn lịch sử nhiều năm cho toàn bộ thị trường...")
        counts = preload_all_market_history(args.output_dir)
        LOGGER.info("Kết quả nạp: %s", counts)
    elif args.symbol:
        sym = args.symbol.strip().upper()
        LOGGER.info("Bắt đầu đồng bộ Hybrid cho mã %s...", sym)
        sync_symbol_fundamentals_hybrid(
            symbol=sym,
            output_dir=args.output_dir,
            period_limit_quarter=args.period_limit,
        )
        LOGGER.info("Hoàn tất đồng bộ Hybrid cho %s", sym)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
