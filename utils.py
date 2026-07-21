"""
共享工具函数：交易日检查、技术指标计算、数据获取辅助
"""
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import akshare as ak
import warnings
warnings.filterwarnings("ignore")


# ---- 交易日历 ----
def is_trade_day(date_str: str = None) -> bool:
    """检查是否为 A 股交易日。"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    try:
        cal = ak.tool_trade_date_hist_sina()
        trade_dates = set(cal["trade_date"].astype(str).values)
        return date_str in trade_dates
    except Exception:
        # 周末直接返回 False
        wd = datetime.strptime(date_str, "%Y-%m-%d").weekday()
        return wd < 5


# ---- 技术指标计算 ----
def calc_macd(close: pd.Series, fast=12, slow=26, signal=9):
    """计算 MACD 指标，返回 (DIF, DEA, MACD_hist)。"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd_hist = 2 * (dif - dea)
    return dif, dea, macd_hist


def calc_kdj(high, low, close, n=9, k_smooth=3, d_smooth=3):
    """计算 KDJ 指标，返回 (K, D, J)。"""
    low_n = low.rolling(window=n).min()
    high_n = high.rolling(window=n).max()
    rsv = (close - low_n) / (high_n - low_n + 1e-9) * 100
    k = rsv.ewm(com=k_smooth - 1, adjust=False).mean()
    d = k.ewm(com=d_smooth - 1, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def calc_rsi(close, period=6):
    """计算 RSI 指标 (Wilder's smoothing)。"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    rsi = 100 - 100 / (1 + rs)
    return rsi


def calc_boll(close, period=20, std_mult=2):
    """计算布林带 (UPPER, MID, LOWER)。"""
    mid = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return upper, mid, lower


def calc_chg_n(df_kline, n):
    """从 K 线 DataFrame 计算 N 日涨跌幅（需要有 date/close）。"""
    if len(df_kline) < n + 1:
        return None
    return (df_kline.iloc[-1]["收盘"] / df_kline.iloc[-(n + 1)]["收盘"] - 1) * 100


# ---- 数据获取 ----
def get_a_spot(codes: list) -> pd.DataFrame:
    """获取 A 股实时行情并筛选指定代码。"""
    try:
        spot = ak.stock_zh_a_spot_em()
        result = spot[spot["代码"].isin(codes)].copy()
        return result
    except Exception:
        return pd.DataFrame()


def get_hk_spot(codes: list) -> pd.DataFrame:
    """获取港股实时行情并筛选指定代码。"""
    try:
        spot = ak.stock_hk_spot_em()
        result = spot[spot["代码"].isin(codes)].copy()
        return result
    except Exception:
        return pd.DataFrame()


def get_kline_a(code: str, days=120) -> pd.DataFrame:
    """获取 A 股日 K 线（前复权）。"""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    try:
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=start, end_date=end, adjust="qfq"
        )
        return df
    except Exception:
        return pd.DataFrame()


def get_kline_hk(code: str, days=120) -> pd.DataFrame:
    """获取港股日 K 线（前复权）。"""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    try:
        df = ak.stock_hk_hist(
            symbol=code, period="daily",
            start_date=start, end_date=end, adjust="qfq"
        )
        return df
    except Exception:
        return pd.DataFrame()


def get_fund_flow(code: str, market: str) -> dict | None:
    """获取个股资金流向。market: sh/sz"""
    try:
        df = ak.stock_individual_fund_flow(stock=code, market=market)
        if df.empty:
            return None
        latest = df.iloc[-1]
        return {
            "date": str(latest.get("日期", "")),
            "main_net": float(latest.get("主力净流入-净额", 0) or 0),
            "main_pct": float(latest.get("主力净流入-净占比", 0) or 0),
            "super_large_net": float(latest.get("超大单净流入-净额", 0) or 0),
            "large_net": float(latest.get("大单净流入-净额", 0) or 0),
            "medium_net": float(latest.get("中单净流入-净额", 0) or 0),
            "small_net": float(latest.get("小单净流入-净额", 0) or 0),
        }
    except Exception:
        return None


def get_last_n_fund_flows(code: str, market: str, n=3) -> list:
    """获取最近 N 个交易日资金流向（用于判断连续流出）。"""
    try:
        df = ak.stock_individual_fund_flow(stock=code, market=market)
        recent = df.tail(n)
        results = []
        for _, row in recent.iterrows():
            results.append({
                "date": str(row.get("日期", "")),
                "main_net": float(row.get("主力净流入-净额", 0) or 0),
            })
        return results
    except Exception:
        return []


def get_stock_news(code: str, limit=5) -> list:
    """获取个股新闻。"""
    try:
        df = ak.stock_news_em(stock=code)
        if df.empty:
            return []
        news_list = []
        for _, row in df.head(limit).iterrows():
            news_list.append({
                "title": str(row.get("标题", "")),
                "time": str(row.get("发布时间", "")),
            })
        return news_list
    except Exception:
        return []


def get_market_news(limit=10) -> list:
    """获取 A 股市场重要新闻。"""
    try:
        df = ak.stock_info_global_em()
        if df.empty:
            return []
        news_list = []
        for _, row in df.head(limit).iterrows():
            news_list.append({
                "title": str(row.get("标题", "")),
                "time": str(row.get("发布时间", "")),
                "summary": str(row.get("摘要", "")),
            })
        return news_list
    except Exception:
        return []


def get_index_spot() -> pd.DataFrame:
    """获取指数实时行情。"""
    try:
        return ak.stock_zh_index_spot_em()
    except Exception:
        return pd.DataFrame()


def get_industry_board() -> pd.DataFrame:
    """获取申万一级行业板块行情。"""
    try:
        return ak.stock_board_industry_name_em()
    except Exception:
        return pd.DataFrame()


def get_concept_board() -> pd.DataFrame:
    """获取概念板块行情。"""
    try:
        return ak.stock_board_concept_name_em()
    except Exception:
        return pd.DataFrame()


def get_hot_stocks(top_n=10) -> pd.DataFrame:
    """获取热门个股（人气榜）。"""
    try:
        df = ak.stock_hot_rank_em()
        return df.head(top_n) if not df.empty else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def get_market_stats() -> dict:
    """获取市场总览统计（涨跌家数、成交额等）。"""
    try:
        spot = ak.stock_zh_a_spot_em()
        if spot.empty:
            return {}

        up = (spot["涨跌幅"] > 0).sum()
        down = (spot["涨跌幅"] < 0).sum()
        flat = (spot["涨跌幅"] == 0).sum()
        limit_up = (
            spot["涨跌幅"] >= 9.9
        ).sum()  # approx
        limit_down = (
            spot["涨跌幅"] <= -9.9
        ).sum()
        total_volume = spot["成交额"].sum() / 1e8  # 亿元

        return {
            "up": int(up),
            "down": int(down),
            "flat": int(flat),
            "limit_up": int(limit_up),
            "limit_down": int(limit_down),
            "total_volume": round(total_volume, 2),
        }
    except Exception:
        return {}
