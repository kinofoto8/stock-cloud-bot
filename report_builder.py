"""
收盘复盘报告生成器 v7 — 纯腾讯+Sina架构 (不依赖东方财富API)
核心改进:
  1. 市场总览: Sina分页统计涨跌家数 + 腾讯获取成交额
  2. 涨停跌停: Sina分页统计 (区分ST/科创/创业板不同涨跌停限制)
  3. 自选股: 腾讯批量行情 (1次调用获取全部含换手率/量比/成交额)
  4. 指数行情: 腾讯批量行情 (1次调用)
  5. K线: 腾讯K线API (主) + Sina K线 (备)
  6. 新闻: Sina lid=2510 股票新闻
  7. 板块/资金: EM clist 尝试 (GitHub Actions可能不可用)
"""
import json
import time
import re
import os
import requests
from datetime import datetime, timezone, timedelta

# ============================================================
# 配置
# ============================================================
INDICES = [
    {"code": "000001", "name": "上证指数", "sina": "s_sh000001", "secid": "1.000001"},
    {"code": "399001", "name": "深证成指", "sina": "s_sz399001", "secid": "0.399001"},
    {"code": "399006", "name": "创业板指", "sina": "s_sz399006", "secid": "0.399006"},
    {"code": "000688", "name": "科创50",   "sina": "s_sh000688", "secid": "1.000688"},
    {"code": "000300", "name": "沪深300",  "sina": "s_sh000300", "secid": "1.000300"},
    {"code": "000905", "name": "中证500",  "sina": "s_sh000905", "secid": "1.000905"},
    {"code": "000852", "name": "中证1000", "sina": "s_sh000852", "secid": "1.000852"},
]

WATCHLIST = [
    {"code": "601899", "name": "紫金矿业",      "sina": "sh601899", "market": "A", "secid": "1.601899"},
    {"code": "000426", "name": "兴业银锡",      "sina": "sz000426", "market": "A", "secid": "0.000426"},
    {"code": "600489", "name": "中金黄金",      "sina": "sh600489", "market": "A", "secid": "1.600489"},
    {"code": "000408", "name": "藏格矿业",      "sina": "sz000408", "market": "A", "secid": "0.000408"},
    {"code": "600331", "name": "宏达股份",      "sina": "sh600331", "market": "A", "secid": "1.600331"},
    {"code": "002240", "name": "盛新锂能",      "sina": "sz002240", "market": "A", "secid": "0.002240"},
    {"code": "588170", "name": "科创半导体ETF华夏", "sina": "sh588170", "market": "A", "secid": "1.588170"},
    {"code": "600988", "name": "赤峰黄金",      "sina": "sh600988", "market": "A", "secid": "1.600988"},
    {"code": "000807", "name": "云铝股份",      "sina": "sz000807", "market": "A", "secid": "0.000807"},
    {"code": "000933", "name": "神火股份",      "sina": "sz000933", "market": "A", "secid": "0.000933"},
    {"code": "00883",  "name": "中国海洋石油",   "sina": "hk00883",  "market": "HK", "secid": "116.00883"},
    {"code": "09992",  "name": "泡泡玛特",      "sina": "hk09992",  "market": "HK", "secid": "116.09992"},
    {"code": "02259",  "name": "紫金黄金国际",   "sina": "hk02259",  "market": "HK", "secid": "116.02259"},
]

GITHUB_PAGES_BASE = "https://kinofoto8.github.io/stock-cloud-bot"

# ============================================================
# 工具函数
# ============================================================
def safe_float(val, default=0.0):
    if val is None: return default
    try: return float(val)
    except (ValueError, TypeError): return default

def safe_int(val, default=0):
    if val is None: return default
    try: return int(float(val))
    except (ValueError, TypeError): return default

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
})

# ============================================================
# 纯 Python 技术指标计算
# ============================================================
def calc_ema(values, period):
    """指数移动平均。"""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]  # SMA 起始
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    # 补齐前 N-1 个
    return [None] * (period - 1) + result

def calc_macd(closes, fast=12, slow=26, signal=9):
    """MACD: 返回 (DIF, DEA, MACD柱) 三个列表。"""
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    n = len(closes)
    dif = [None] * n
    for i in range(slow - 1, n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif[i] = round(ema_fast[i] - ema_slow[i], 4)
    dea = calc_ema([d for d in dif if d is not None], signal)
    # 补齐 dea
    dea_full = [None] * (n - len(dea)) + dea
    macd_hist = [None] * n
    for i in range(n):
        if dif[i] is not None and dea_full[i] is not None:
            macd_hist[i] = round(2 * (dif[i] - dea_full[i]), 4)
    return dif, dea_full, macd_hist

def calc_kdj(highs, lows, closes, n=9):
    """KDJ: 返回 (K, D, J) 三个列表。"""
    length = len(closes)
    k_vals = [None] * length
    d_vals = [None] * length
    j_vals = [None] * length

    prev_k = 50.0
    prev_d = 50.0

    for i in range(n - 1, length):
        h = max(highs[i - n + 1:i + 1])
        l = min(lows[i - n + 1:i + 1])
        if h == l:
            rsv = 50.0
        else:
            rsv = (closes[i] - l) / (h - l) * 100

        prev_k = 2.0 / 3 * prev_k + 1.0 / 3 * rsv
        prev_d = 2.0 / 3 * prev_d + 1.0 / 3 * prev_k
        k_vals[i] = round(prev_k, 2)
        d_vals[i] = round(prev_d, 2)
        j_vals[i] = round(3 * prev_k - 2 * prev_d, 2)

    return k_vals, d_vals, j_vals

def calc_rsi(closes, period=6):
    """RSI (Wilder smoothing)。"""
    n = len(closes)
    if n < period + 1:
        return [None] * n
    rsi = [None] * n
    gains = []
    losses = []
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = 100.0 - 100.0 / (1 + rs)

    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100.0 - 100.0 / (1 + rs)

    return rsi

def calc_boll(closes, period=20, std_mult=2):
    """布林带: 返回 (UPPER, MID, LOWER) 三个列表。"""
    n = len(closes)
    upper = [None] * n
    mid = [None] * n
    lower = [None] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        avg = sum(window) / period
        variance = sum((x - avg) ** 2 for x in window) / period
        std = variance ** 0.5
        mid[i] = round(avg, 2)
        upper[i] = round(avg + std_mult * std, 2)
        lower[i] = round(avg - std_mult * std, 2)
    return upper, mid, lower

def calc_ma(values, period):
    """简单移动平均。"""
    n = len(values)
    if n < period:
        return [None] * n
    ma = [None] * (period - 1)
    for i in range(period - 1, n):
        ma.append(round(sum(values[i - period + 1:i + 1]) / period, 2))
    return ma

def calc_obv(closes, volumes):
    """能量潮 OBV。"""
    obv = [0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])
    return obv

# ============================================================
# Sina 财经 API — 批量获取个股/指数行情
# ============================================================
SINA_QUOTE_URL = "https://hq.sinajs.cn/list="

def fetch_sina_quotes(sina_codes):
    """批量获取 Sina 行情数据。返回 dict: {sina_code: parsed_dict}。"""
    if not sina_codes:
        return {}

    url = SINA_QUOTE_URL + ",".join(sina_codes)
    try:
        resp = SESSION.get(url, timeout=15)
        resp.raise_for_status()
        resp.encoding = "gb2312"
        raw = resp.text
    except Exception as e:
        print(f"  [WARN] Sina API 请求失败: {e}")
        return {}

    result = {}
    lines = raw.strip().split("\n")

    for line in lines:
        m = re.match(r'var hq_str_(\w+)="(.+)"', line)
        if not m:
            continue
        code = m.group(1)
        data = m.group(2).split(",")
        if len(data) < 5:
            continue

        try:
            name = data[0]
            is_index = code.startswith("s_")

            if is_index:
                price = safe_float(data[1])
                change = safe_float(data[2])
                pct = safe_float(data[3])
                volume = safe_float(data[4])
                amount = safe_float(data[5]) / 1e8  # 元 → 亿
                result[code] = {
                    "name": name, "price": price, "change": change,
                    "pct": pct, "volume": volume, "amount": amount,
                    "is_index": True,
                }
            else:
                price = safe_float(data[3])
                open_p = safe_float(data[1])
                prev_close = safe_float(data[2])
                high = safe_float(data[4])
                low = safe_float(data[5])
                volume_hand = safe_float(data[8])
                amount = safe_float(data[9]) / 1e8  # 元 → 亿

                if prev_close > 0 and price > 0:
                    pct = round((price - prev_close) / prev_close * 100, 2)
                    change = round(price - prev_close, 2)
                else:
                    pct = 0
                    change = 0

                result[code] = {
                    "name": name, "price": price, "open": open_p,
                    "prev_close": prev_close, "high": high, "low": low,
                    "change": change, "pct": pct,
                    "volume_hand": volume_hand, "amount": amount,
                    "is_index": False,
                }
        except Exception as e:
            print(f"  [WARN] 解析 {code} 失败: {e}")
            continue

    return result

# ============================================================
# 腾讯财经 API — 批量获取个股/指数行情 (含换手率/量比/成交额)
# ============================================================
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="

def fetch_tencent_quotes(symbols):
    """批量获取腾讯行情数据。symbols: ["sh601899", "sz000426", ...]
    返回: {symbol: {name, code, price, open, prev_close, high, low,
                    change, pct, volume_hand, amount(亿), turnover(%),
                    volume_ratio, amplitude(%)}, ...}
    """
    if not symbols:
        return {}

    url = TENCENT_QUOTE_URL + ",".join(symbols)
    try:
        resp = SESSION.get(url, timeout=15)
        resp.raise_for_status()
        resp.encoding = "gbk"
        raw = resp.text
    except Exception as e:
        print(f"  [WARN] 腾讯API请求失败: {e}")
        return {}

    result = {}
    for line in raw.strip().split(";"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r'v_(\w+)="(.+)"', line)
        if not m:
            continue
        symbol = m.group(1)
        parts = m.group(2).split("~")
        if len(parts) < 50:
            continue

        try:
            name = parts[1]
            code = parts[2]
            price = safe_float(parts[3])
            prev_close = safe_float(parts[4])
            open_p = safe_float(parts[5])
            volume_hand = safe_float(parts[6])      # 成交量(A股:手, 港股:股)
            change = safe_float(parts[31])           # 涨跌额
            pct = safe_float(parts[32])              # 涨跌幅(%)
            high = safe_float(parts[33])             # 最高
            low = safe_float(parts[34])              # 最低
            amplitude = safe_float(parts[43])        # 振幅(%)

            # HK stocks have different field layout (78 fields vs 88 for A-share)
            is_hk = symbol.startswith("hk")
            if is_hk:
                # 港股: parts[37] 是成交额(港元)，parts[38]/[49]无意义
                amount_hkd = safe_float(parts[37])
                amount_yi = amount_hkd / 1e8  # 港元 → 亿港元
                turnover = 0.0
                vol_ratio = 0.0
            else:
                # A股: parts[37] 是成交额(万元)
                amount_wan = safe_float(parts[37])
                amount_yi = amount_wan / 1e4  # 万元 → 亿元
                turnover = safe_float(parts[38])         # 换手率(%)
                vol_ratio = safe_float(parts[49])        # 量比

            result[symbol] = {
                "name": name, "code": code, "price": price,
                "open": open_p, "prev_close": prev_close,
                "high": high, "low": low,
                "change": change, "pct": pct,
                "volume_hand": volume_hand,
                "amount": amount_yi,           # A股:亿元 / 港股:亿港元
                "turnover": turnover,           # %
                "volume_ratio": vol_ratio,
                "amplitude": amplitude,         # %
                "is_hk": is_hk,
            }
        except Exception as e:
            print(f"  [WARN] 腾讯解析 {symbol} 失败: {e}")
            continue

    return result

# ============================================================
# 腾讯 K 线 API
# ============================================================
def get_kline_tencent(symbol, datalen=60):
    """获取腾讯日K线数据。
    返回: [{date, open, close, high, low, volume, amount, pct}, ...]
    """
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{datalen},qfq"
    try:
        resp = SESSION.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        kline_data = (data.get("data") or {}).get(symbol, {})
        day_data = kline_data.get("day") or kline_data.get("qfqday") or []
        if not day_data:
            return []

        result = []
        prev_close = None
        for k in day_data:
            # [date, open, close, high, low, volume]
            close = safe_float(k[2]) if len(k) > 2 else 0
            open_p = safe_float(k[1]) if len(k) > 1 else 0
            high = safe_float(k[3]) if len(k) > 3 else 0
            low = safe_float(k[4]) if len(k) > 4 else 0
            volume = safe_float(k[5]) if len(k) > 5 else 0
            dt = k[0] if len(k) > 0 else ""

            if prev_close and prev_close > 0 and close > 0:
                pct = round((close - prev_close) / prev_close * 100, 2)
            else:
                pct = 0.0

            result.append({
                "date": dt,
                "open": open_p,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
                "amount": volume * close,  # 估算
                "amplitude": round((high - low) / prev_close * 100, 2) if prev_close and prev_close > 0 else 0,
                "pct": pct,
                "change": round(close - prev_close, 4) if prev_close else 0,
                "turnover": 0,
            })
            prev_close = close
        return result
    except Exception as e:
        print(f"  [WARN] 腾讯K线请求异常 {symbol}: {e}")
        return []

# ============================================================
# 东方财富 API — 板块/资金流向 (GitHub Actions可能不可用)
# ============================================================
EM_SESSION = requests.Session()
EM_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
})

# 多个 ut 令牌，轮换使用避免限流
EM_UT_LIST = [
    "fa5fd1943c7b386f172d6893dbfba10b",
    "b587f3c7b386f172d6893dbfba10b",
    "7c8a9a3c7b386f172d6893dbfba10b",
]
EM_UT = EM_UT_LIST[0]

def em_fetch_json(url, params):
    """请求东方财富 JSON 接口，自动添加 ut 令牌，多 token 轮换重试。"""
    for ut_idx, ut in enumerate(EM_UT_LIST):
        p = dict(params)
        p["ut"] = ut
        try:
            resp = EM_SESSION.get(url, params=p, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("data") is not None:
                return data
        except Exception as e:
            if ut_idx == len(EM_UT_LIST) - 1:
                print(f"  [WARN] EM 请求失败(所有ut): {type(e).__name__}")
            time.sleep(0.5)
    return {}

def em_fetch_raw(url, params):
    """请求东方财富接口，返回原始 text。"""
    params["ut"] = EM_UT
    try:
        resp = EM_SESSION.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"  [WARN] EM raw 请求失败: {e}")
        return ""

def em_ulist_get(secids, fields):
    """批量获取多个证券的行情数据 (ulist.np API)。
    使用 clist 字段编号: f2=价格, f3=涨跌幅, f6=成交额, f7=振幅, f8=换手率,
    f10=量比, f12=代码, f14=名称, f104=上涨家数, f105=下跌家数, f106=平盘家数
    """
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        "secids": secids,
        "fields": fields,
        "fltt": "2",
        "invt": "2",
    }
    for ut_idx, ut in enumerate(EM_UT_LIST):
        p = dict(params)
        p["ut"] = ut
        try:
            resp = EM_SESSION.get(url, params=p, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("data") is not None:
                return (data.get("data") or {}).get("diff", [])
        except Exception:
            if ut_idx == len(EM_UT_LIST) - 1:
                print(f"  [WARN] ulist.np 请求失败: secids={secids[:40]}")
            time.sleep(0.5)
    return []

def em_get_zt_dt_pool(pool_type, date_str):
    """获取涨停/跌停池数据。
    pool_type: 'ZT' for 涨停, 'DT' for 跌停
    date_str: YYYYMMDD
    """
    url = f"https://push2ex.eastmoney.com/getTopic{pool_type}Pool"
    params = {
        "dpt": "wz.ztzt",
        "Pageindex": "0",
        "pagesize": "500",
        "sort": "fbt:asc",
        "date": date_str,
    }
    for ut_idx, ut in enumerate(EM_UT_LIST):
        p = dict(params)
        p["ut"] = ut
        try:
            resp = EM_SESSION.get(url, params=p, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("data") is not None:
                pool = (data.get("data") or {}).get("pool", [])
                return len(pool)
        except Exception:
            if ut_idx == len(EM_UT_LIST) - 1:
                print(f"  [WARN] {pool_type}Pool 请求失败")
            time.sleep(0.5)
    return 0

# ============================================================
# K 线数据获取 — Sina API (主) + 东方财富 (备)
# ============================================================
def get_kline_sina(sina_code, datalen=60):
    """获取 Sina 日K线数据。
    返回: [{date, open, close, high, low, volume, amount, pct}, ...]
    """
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {
        "symbol": sina_code,
        "scale": "240",      # 日K
        "datalen": str(datalen),
    }
    try:
        resp = SESSION.get(url, params=params, timeout=15)
        resp.raise_for_status()
        if not resp.text or resp.text.strip() == "[]" or len(resp.text) < 10:
            return []
        raw = json.loads(resp.text)
        if not raw:
            return []

        result = []
        prev_close = None
        for k in raw:
            close = safe_float(k.get("close"))
            open_p = safe_float(k.get("open"))
            high = safe_float(k.get("high"))
            low = safe_float(k.get("low"))
            volume = safe_float(k.get("volume"))
            dt = k.get("day", "")

            if prev_close and prev_close > 0 and close > 0:
                pct = round((close - prev_close) / prev_close * 100, 2)
            else:
                pct = 0.0

            # Sina 不提供 amount，用 volume 估算 (volume 是股数)
            amount = volume * close  # 估算

            result.append({
                "date": dt,
                "open": open_p,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
                "amount": amount,
                "amplitude": round((high - low) / prev_close * 100, 2) if prev_close and prev_close > 0 else 0,
                "pct": pct,
                "change": round(close - prev_close, 4) if prev_close else 0,
                "turnover": 0,
            })
            prev_close = close
        return result
    except Exception as e:
        print(f"  [WARN] Sina K线请求异常 {sina_code}: {e}")
        return []

def get_kline_em(secid, days=60):
    """获取东方财富日K线数据（备用）。
    返回: [{date, open, close, high, low, volume, amount, pct}, ...]
    """
    beijing_tz = timezone(timedelta(hours=8))
    end_date = datetime.now(beijing_tz).strftime("%Y%m%d")
    start_date = (datetime.now(beijing_tz) - timedelta(days=days + 10)).strftime("%Y%m%d")

    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "klt": "101",        # 日K
        "fqt": "1",          # 前复权
        "beg": start_date,
        "end": end_date,
        "lmt": str(days + 5),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }

    try:
        data = em_fetch_json(url, params)
        klines_raw = (data.get("data") or {}).get("klines", [])
        if not klines_raw:
            return []

        result = []
        for line in klines_raw:
            parts = line.split(",")
            if len(parts) < 8:
                continue
            result.append({
                "date": parts[0],
                "open": safe_float(parts[1]),
                "close": safe_float(parts[2]),
                "high": safe_float(parts[3]),
                "low": safe_float(parts[4]),
                "volume": safe_float(parts[5]),
                "amount": safe_float(parts[6]),
                "amplitude": safe_float(parts[7]),
                "pct": safe_float(parts[8]),
                "change": safe_float(parts[9]),
                "turnover": safe_float(parts[10]),
            })
        return result
    except Exception as e:
        print(f"  [WARN] K线请求异常 {secid}: {e}")
        return []

def get_kline(tencent_symbol, days=60, secid=None, sina_code=None):
    """获取K线数据: 腾讯优先，Sina备用，EM最后兜底。
    tencent_symbol: 腾讯格式代码 (sh000001, sz000426 等)
    """
    # 腾讯 K线
    klines = get_kline_tencent(tencent_symbol, days)
    if len(klines) >= 20:
        return klines
    # Sina 备用
    sc = sina_code or tencent_symbol
    print(f"  [INFO] 腾讯K线不足({len(klines)}条)，尝试Sina: {sc}")
    klines = get_kline_sina(sc, days)
    if len(klines) >= 20:
        return klines
    # EM 最后兜底
    if secid:
        print(f"  [INFO] Sina K线也不足({len(klines)}条)，尝试EM: {secid}")
        klines = get_kline_em(secid, days)
    return klines

# ============================================================
# 常规数据获取（复用 V3 逻辑）
# ============================================================
def get_market_overview():
    """市场总览：涨跌家数、成交额、涨停跌停。
    用 Sina 分页遍历全部A股统计涨跌家数和涨停跌停。
    用腾讯获取上证+深证成交额。
    """
    print("  [1/5] 获取市场总览 (Sina分页统计)...")

    # === Sina 分页统计涨跌家数 ===
    up = down = flat = 0
    limit_up = limit_down = 0
    total_stocks = 0

    sina_api = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    for page in range(1, 80):  # 最多80页
        try:
            params = {
                "page": str(page), "num": "100",
                "sort": "symbol", "asc": "1", "node": "hs_a",
            }
            resp = SESSION.get(sina_api, params=params, timeout=15)
            if resp.status_code != 200 or not resp.text.strip():
                break
            stocks = json.loads(resp.text)
            if not stocks:
                break

            for s in stocks:
                pct = safe_float(s.get("changepercent", 0))
                code = s.get("code", "")
                name = s.get("name", "") or s.get("symbol", "")

                if pct > 0: up += 1
                elif pct < 0: down += 1
                else: flat += 1
                total_stocks += 1

                # 涨停跌停判断 (区分不同板块的涨跌停限制)
                if "ST" in name or "*ST" in name:
                    limit = 5.0
                elif code.startswith("688") or code.startswith("30"):
                    limit = 20.0
                elif code.startswith("8") or code.startswith("4"):
                    limit = 30.0
                else:
                    limit = 10.0

                if pct >= limit - 0.3:
                    limit_up += 1
                if pct <= -limit + 0.3:
                    limit_down += 1

            if page % 10 == 0:
                print(f"    已扫描 {total_stocks} 只 (第{page}页)...")
        except Exception as e:
            print(f"    [WARN] Sina分页第{page}页失败: {e}")
            break

    # === 腾讯获取两市成交额 ===
    tq = fetch_tencent_quotes(["sh000001", "sz399001"])
    total_amount = tq.get("sh000001", {}).get("amount", 0) + tq.get("sz399001", {}).get("amount", 0)

    # 如果腾讯也失败，用Sina指数成交额
    if total_amount == 0:
        sq = fetch_sina_quotes(["s_sh000001", "s_sz399001"])
        total_amount = sq.get("s_sh000001", {}).get("amount", 0) + sq.get("s_sz399001", {}).get("amount", 0)

    print(f"  [总览] 上涨{up} 下跌{down} 平盘{flat} 涨停{limit_up} 跌停{limit_down} 成交额{total_amount:.0f}亿 (共{total_stocks}只)")

    return {
        "up": up, "down": down, "flat": flat,
        "limit_up": limit_up, "limit_down": limit_down,
        "total_amount": round(total_amount, 2),
        "total_stocks": total_stocks,
    }

def get_index_data():
    """指数行情 — 腾讯批量获取 (1次API调用)。"""
    print("  [2/5] 获取指数行情 (腾讯批量)...")
    # 腾讯用 sh/sz 前缀，去掉 sina 的 s_ 前缀
    tencent_symbols = [idx["sina"].replace("s_", "") for idx in INDICES]
    tq = fetch_tencent_quotes(tencent_symbols)

    # 如果腾讯全部失败，用Sina兜底
    if not tq:
        print("  [WARN] 腾讯行情全部失败，尝试Sina...")
        sina_codes = [idx["sina"] for idx in INDICES]
        sq = fetch_sina_quotes(sina_codes)
    else:
        sq = {}

    result = []
    for idx in INDICES:
        sym = idx["sina"].replace("s_", "")
        q = tq.get(sym, {})
        s = sq.get(idx["sina"], {})

        price = q.get("price", 0) or s.get("price", 0)
        pct = q.get("pct", 0) or s.get("pct", 0)
        change = q.get("change", 0) or s.get("change", 0)
        amount = q.get("amount", 0) or s.get("amount", 0)
        volume = q.get("volume_hand", 0) or s.get("volume", 0)

        result.append({
            "name": idx["name"],
            "code": idx["code"],
            "secid": idx["secid"],
            "sina": idx["sina"],
            "price": price,
            "pct": pct,
            "change": change,
            "volume": volume,
            "amount": amount,
        })
    return result

def _parse_sina_boards(url, var_name):
    """解析 Sina 板块数据 (行业/概念通用)。
    返回 [{"name","code","pct","price","rise_count","fall_count","flat_count"}, ...]
    """
    resp = SESSION.get(url, timeout=15)
    resp.encoding = "utf-8"
    text = resp.text
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= 0:
        return []
    import json as _json
    raw = _json.loads(text[start:end])

    boards = []
    for k, v in raw.items():
        parts = v.split(",")
        if len(parts) < 5:
            continue
        boards.append({
            "name": parts[1],
            "code": parts[0],
            "pct": safe_float(parts[4]) if parts[4] else 0,
            "price": safe_float(parts[3]) if parts[3] else 0,
            "rise_count": 0,
            "fall_count": 0,
            "flat_count": 0,
        })
    boards.sort(key=lambda x: x["pct"], reverse=True)
    return boards

def get_industry_boards():
    """行业板块 — EM优先, Sina备用 (确保本地/云端都有数据)。"""
    print("  [3/5] 获取行业板块...")
    # 尝试 EM clist (申万一级行业, GitHub Actions可用)
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:90+t:2",
        "fields": "f2,f3,f12,f14,f104,f105,f106",
    }
    data = em_fetch_json(url, params)
    items = (data.get("data") or {}).get("diff", [])

    if items:
        boards = []
        for it in items:
            boards.append({
                "name": it.get("f14", ""),
                "code": it.get("f12", ""),
                "pct": safe_float(it.get("f3")),
                "price": safe_float(it.get("f2")),
                "rise_count": safe_int(it.get("f104")),
                "fall_count": safe_int(it.get("f105")),
                "flat_count": safe_int(it.get("f106")),
            })
        return boards

    # EM 失败 → Sina 备用 (本地代理/EM封锁时)
    print("  [INFO] EM板块API不可用, 切换Sina备用源...")
    try:
        boards = _parse_sina_boards(
            "https://money.finance.sina.com.cn/q/view/newSinaHy.php",
            "S_Finance_bankuai_sinaindustry"
        )
        if boards:
            print(f"  [INFO] Sina行业板块: {len(boards)}个")
        return boards
    except Exception as e:
        print(f"  [WARN] Sina行业板块也失败: {e}")
        return []

def get_concept_boards():
    """概念板块 — EM优先, Sina备用。"""
    print("  [4/5] 获取概念板块...")
    # 尝试 EM clist
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "500", "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:90+t:3",
        "fields": "f2,f3,f12,f14",
    }
    data = em_fetch_json(url, params)
    items = (data.get("data") or {}).get("diff", [])

    if items:
        boards = []
        for it in items:
            boards.append({
                "name": it.get("f14", ""),
                "code": it.get("f12", ""),
                "pct": safe_float(it.get("f3")),
                "price": safe_float(it.get("f2")),
            })
        boards.sort(key=lambda x: x["pct"], reverse=True)
        return boards

    # EM 失败 → Sina 备用
    print("  [INFO] EM概念板块API不可用, 切换Sina备用源...")
    try:
        boards = _parse_sina_boards(
            "https://money.finance.sina.com.cn/q/view/newFLJK.php?param=class",
            "S_Finance_bankuai_class"
        )
        if boards:
            print(f"  [INFO] Sina概念板块: {len(boards)}个")
        return boards
    except Exception as e:
        print(f"  [WARN] Sina概念板块也失败: {e}")
        return []

def get_industry_fund_flow():
    """行业资金流向 — EM专属 (Sina无替代, 失败时返回空列表)。"""
    print("  [5/5] 获取资金流向...")
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f62",
        "fs": "m:90+t:2",
        "fields": "f2,f3,f12,f14,f62,f184,f66,f72,f78,f84",
    }
    data = em_fetch_json(url, params)
    items = (data.get("data") or {}).get("diff", [])

    if not items:
        print("  [INFO] EM资金流向API不可用 (本地代理/云端封锁)")

    flows = []
    for it in items:
        main_net = safe_float(it.get("f62"))
        flows.append({
            "name": it.get("f14", ""),
            "pct": safe_float(it.get("f3")),
            "main_net": main_net / 1e8,
            "super_large_net": safe_float(it.get("f66")) / 1e8,
            "large_net": safe_float(it.get("f72")) / 1e8,
            "medium_net": safe_float(it.get("f78")) / 1e8,
            "small_net": safe_float(it.get("f84")) / 1e8,
            "main_pct": safe_float(it.get("f184")),
        })
    flows.sort(key=lambda x: x["main_net"], reverse=True)
    return flows

def get_watchlist_data():
    """自选股行情 — 腾讯批量获取 (1次API调用获取全部含换手率/量比/成交额)。
    腾讯API支持A股和港股，sina格式代码(sh601899/hk00883)直接复用。
    """
    print("    获取自选股行情 (腾讯批量)...")
    # 腾讯和Sina共用相同的代码格式 (sh601899, sz000426, hk00883)
    all_symbols = [s["sina"] for s in WATCHLIST]
    tq = fetch_tencent_quotes(all_symbols)

    # Sina兜底（如果腾讯失败）
    if not tq:
        print("  [WARN] 腾讯行情失败，尝试Sina...")
        sq = fetch_sina_quotes(all_symbols)
    else:
        sq = {}

    result = []
    for s in WATCHLIST:
        q = tq.get(s["sina"], {})
        sin = sq.get(s["sina"], {})

        price = q.get("price", 0) or sin.get("price", 0)
        pct = q.get("pct", 0) or sin.get("pct", 0)
        change = q.get("change", 0) or sin.get("change", 0)
        high = q.get("high", 0) or sin.get("high", 0)
        low = q.get("low", 0) or sin.get("low", 0)
        volume = q.get("volume_hand", 0) or sin.get("volume_hand", 0)
        amount = q.get("amount", 0) or sin.get("amount", 0)
        turnover = q.get("turnover", 0)
        vol_ratio = q.get("volume_ratio", 0)
        amplitude = q.get("amplitude", 0)

        result.append({
            "name": s["name"],
            "code": s["code"],
            "market": s["market"],
            "secid": s["secid"],
            "sina": s["sina"],
            "price": price,
            "pct": pct,
            "change": change,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": amount,
            "turnover": turnover,
            "volume_ratio": vol_ratio,
            "amplitude": amplitude,
        })

    return result

# ============================================================
# 新闻获取 — 多源备用
# ============================================================
def get_market_news():
    """获取市场重要新闻，多源备用。"""
    print("    获取要闻...")

    # 方案 A: 东方财富市场要闻
    try:
        url = "https://np-listapi.eastmoney.com/comm/web/getNewsList"
        params = {
            "client": "web", "bizid": "1",
            "last_score": "0", "page_size": "10",
        }
        resp = SESSION.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        items = (data.get("data") or {}).get("list", [])
        if items:
            print(f"  [新闻] 东方财富来源: {len(items)} 条")
            return [{"title": it.get("title", ""),
                     "time": it.get("showTime", ""),
                     "source": it.get("source", "")} for it in items[:10]]
    except Exception as e:
        print(f"  [新闻] 东方财富来源失败: {e}")

    # 方案 B: Sina 财经滚动新闻 (lid=2510 股票频道)
    for lid_name, lid in [("股票", "2510"), ("财经", "1686"), ("7x24", "1687")]:
        try:
            url = "https://feed.mix.sina.com.cn/api/roll/get"
            params = {
                "pageid": "153", "lid": lid,
                "k": "", "num": "10", "page": "1",
                "r": str(time.time())[:13],
            }
            resp = SESSION.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("result", {}).get("data", [])
            if items:
                print(f"  [新闻] Sina[{lid_name}] 来源: {len(items)} 条")
                news = []
                for it in items[:10]:
                    ctime = it.get("ctime", "")
                    if ctime and ctime.isdigit():
                        try:
                            ctime = datetime.fromtimestamp(int(ctime)).strftime("%H:%M")
                        except Exception:
                            pass
                    news.append({
                        "title": it.get("title", ""),
                        "time": ctime,
                        "source": it.get("media_name", ""),
                    })
                return news
        except Exception as e:
            print(f"  [新闻] Sina[{lid_name}] 来源失败: {e}")

    print("  [新闻] 所有来源均失败")
    return []

# ============================================================
# 技术分析汇总
# ============================================================
def analyze_technicals(kline_data):
    """从 K 线数据计算全部技术指标，返回当前值 + 信号。"""
    if len(kline_data) < 30:
        return {}

    closes = [k["close"] for k in kline_data]
    highs = [k["high"] for k in kline_data]
    lows = [k["low"] for k in kline_data]
    volumes = [k["volume"] for k in kline_data]

    # MACD
    dif, dea, macd_hist = calc_macd(closes)
    macd_signal = ""
    if macd_hist[-1] is not None and macd_hist[-2] is not None and dea[-1] is not None:
        if macd_hist[-2] <= 0 and macd_hist[-1] > 0:
            macd_signal = "金叉 ↗"
        elif macd_hist[-2] >= 0 and macd_hist[-1] < 0:
            macd_signal = "死叉 ↘"
        elif macd_hist[-1] > macd_hist[-2]:
            macd_signal = "多头增强"
        elif macd_hist[-1] < macd_hist[-2]:
            macd_signal = "空头增强"

    # KDJ
    k, d, j = calc_kdj(highs, lows, closes)
    kdj_signal = ""
    if j[-1] is not None:
        if j[-1] > 100:
            kdj_signal = "超买 ⚠"
        elif j[-1] < 0:
            kdj_signal = "超卖"
        elif k[-1] is not None and d[-1] is not None:
            if k[-2] is not None and d[-2] is not None:
                if k[-2] <= d[-2] and k[-1] > d[-1]:
                    kdj_signal = "金叉 ↗"
                elif k[-2] >= d[-2] and k[-1] < d[-1]:
                    kdj_signal = "死叉 ↘"

    # RSI
    rsi6 = calc_rsi(closes, 6)
    rsi14 = calc_rsi(closes, 14)
    rsi_signal = ""
    if rsi6[-1] is not None:
        if rsi6[-1] > 80:
            rsi_signal = "超买 ⚠"
        elif rsi6[-1] < 20:
            rsi_signal = "超卖"
        elif rsi6[-1] > 50:
            rsi_signal = "偏强"
        else:
            rsi_signal = "偏弱"

    # BOLL
    boll_upper, boll_mid, boll_lower = calc_boll(closes)
    boll_position = ""
    if boll_upper[-1] is not None and closes[-1] > 0:
        boll_width = (boll_upper[-1] - boll_lower[-1]) / boll_mid[-1] * 100
        if closes[-1] > boll_upper[-1]:
            boll_position = f"突破上轨 ↑"
        elif closes[-1] < boll_lower[-1]:
            boll_position = "跌破下轨 ↓"
        else:
            pos_pct = (closes[-1] - boll_lower[-1]) / (boll_upper[-1] - boll_lower[-1]) * 100
            if pos_pct > 80:
                boll_position = "上轨附近"
            elif pos_pct < 20:
                boll_position = "下轨附近"
            else:
                boll_position = "中轨附近"
            boll_position += f" (带宽 {boll_width:.1f}%)"

    # MA
    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    ma_status = ""
    if ma5[-1] and ma10[-1] and ma20[-1]:
        if ma5[-1] > ma10[-1] > ma20[-1]:
            ma_status = "多头排列 ↑"
        elif ma5[-1] < ma10[-1] < ma20[-1]:
            ma_status = "空头排列 ↓"
        elif closes[-1] > ma20[-1]:
            ma_status = "站上 MA20"
        else:
            ma_status = "跌破 MA20"

    # 成交量分析
    vol_ma5 = calc_ma(volumes, 5)
    vol_ratio = ""
    if vol_ma5[-1] and volumes[-1] > 0:
        ratio = volumes[-1] / vol_ma5[-1]
        if ratio > 1.5:
            vol_ratio = f"放量 {ratio:.1f}x"
        elif ratio < 0.5:
            vol_ratio = f"缩量 {ratio:.1f}x"
        else:
            vol_ratio = f"正常 {ratio:.1f}x"

    # 近期涨跌幅
    chg_5d = None
    if len(closes) >= 6:
        chg_5d = round((closes[-1] / closes[-6] - 1) * 100, 2)
    chg_20d = None
    if len(closes) >= 21:
        chg_20d = round((closes[-1] / closes[-21] - 1) * 100, 2)

    return {
        "macd_dif": macd_hist[-1] if macd_hist[-1] is not None else None,
        "macd_signal": macd_signal,
        "kdj_k": k[-1],
        "kdj_d": d[-1],
        "kdj_j": j[-1],
        "kdj_signal": kdj_signal,
        "rsi6": round(rsi6[-1], 1) if rsi6[-1] is not None else None,
        "rsi14": round(rsi14[-1], 1) if rsi14[-1] is not None else None,
        "rsi_signal": rsi_signal,
        "boll_upper": boll_upper[-1],
        "boll_mid": boll_mid[-1],
        "boll_lower": boll_lower[-1],
        "boll_signal": boll_position,
        "ma5": ma5[-1],
        "ma10": ma10[-1],
        "ma20": ma20[-1],
        "ma_status": ma_status,
        "vol_ratio": vol_ratio,
        "chg_5d": chg_5d,
        "chg_20d": chg_20d,
    }

def fetch_all_data():
    print("=" * 50)
    print("开始数据采集 v7 (纯腾讯+Sina架构, 不依赖东方财富)...")
    print("=" * 50)

    overview = get_market_overview()
    indices = get_index_data()
    industries = get_industry_boards()
    concepts = get_concept_boards()
    fund_flows = get_industry_fund_flow()
    watchlist = get_watchlist_data()
    news = get_market_news()

    if not industries:
        print("  [WARN] 行业板块数据为空 (EM API可能被封锁)")
    if not concepts:
        print("  [WARN] 概念板块数据为空 (EM API可能被封锁)")
    if not fund_flows:
        print("  [WARN] 资金流向数据为空 (EM API可能被封锁)")

    # === 技术分析: 指数 (用腾讯K线) ===
    print("\n--- 技术分析: 指数 ---")
    indices_tech = {}
    indices_kline = {}
    for idx in INDICES:
        tencent_sym = idx["sina"].replace("s_", "")
        klines = get_kline(tencent_sym, days=60, secid=idx["secid"], sina_code=tencent_sym)
        if len(klines) >= 30:
            tech = analyze_technicals(klines)
            indices_tech[idx["code"]] = tech
            indices_kline[idx["code"]] = klines
            print(f"  {idx['name']}: MA5={tech.get('ma5')} RSI6={tech.get('rsi6')} | {tech.get('ma_status')} | {tech.get('macd_signal')}")
        else:
            print(f"  {idx['name']}: K线数据不足 ({len(klines)}条)")

    # === 技术分析: 自选股 (用腾讯K线) ===
    print("\n--- 技术分析: 自选股 ---")
    watchlist_tech = {}
    watchlist_kline = {}
    for s in WATCHLIST:
        if s["market"] == "HK":
            continue  # 港股K线API不同，暂跳过
        klines = get_kline(s["sina"], days=60, secid=s["secid"], sina_code=s["sina"])
        if len(klines) >= 30:
            tech = analyze_technicals(klines)
            watchlist_tech[s["code"]] = tech
            watchlist_kline[s["code"]] = klines
            print(f"  {s['name']}: {tech.get('ma_status','')} | {tech.get('macd_signal','')} | {tech.get('boll_signal','')}")
        else:
            print(f"  {s['name']}: K线数据不足 ({len(klines)}条)")

    print(f"\n数据采集完成:")
    print(f"  市场总览: {'OK' if overview else 'EMPTY'}")
    print(f"  指数: {len(indices)} 个 | 技术分析: {len(indices_tech)} 个")
    print(f"  行业板块: {len(industries)} 个")
    print(f"  概念板块: {len(concepts)} 个")
    print(f"  资金流向: {len(fund_flows)} 个")
    print(f"  自选股: {len(watchlist)} 只 | 技术分析: {len(watchlist_tech)} 只")
    print(f"  新闻: {len(news)} 条")

    return {
        "overview": overview,
        "indices": indices,
        "industries": industries,
        "concepts": concepts,
        "fund_flows": fund_flows,
        "watchlist": watchlist,
        "news": news,
        "indices_tech": indices_tech,
        "watchlist_tech": watchlist_tech,
        "indices_kline": indices_kline,
        "watchlist_kline": watchlist_kline,
    }

# ============================================================
# HTML 报告生成
# ============================================================
_CHART_ID_COUNTER = [0]

def _next_chart_id():
    _CHART_ID_COUNTER[0] += 1
    return f"chart_{_CHART_ID_COUNTER[0]}"

def build_kline_chart_placeholder(chart_id, name, klines):
    """生成 K 线 + BOLL + 成交量图表的 div + JS。"""
    if not klines or len(klines) < 20:
        return f'<div class="chart-box" id="{chart_id}" style="height:300px"></div>\n'

    dates = [k["date"][-5:] for k in klines]  # MM-DD
    ohlc = [[k["open"], k["close"], k["low"], k["high"]] for k in klines]
    volumes = [k["volume"] for k in klines]
    closes = [k["close"] for k in klines]

    _, boll_mid, boll_lower = calc_boll(closes)
    boll_upper, _, _ = calc_boll(closes)
    ma5 = calc_ma(closes, 5)
    ma20 = calc_ma(closes, 20)

    # 过滤有效数据点
    mid_vals = [round(v, 2) if v is not None else "-" for v in boll_mid]
    upper_vals = [round(v, 2) if v is not None else "-" for v in boll_upper]
    lower_vals = [round(v, 2) if v is not None else "-" for v in boll_lower]
    ma5_vals = [round(v, 2) if v is not None else "-" for v in ma5]
    ma20_vals = [round(v, 2) if v is not None else "-" for v in ma20]

    # 成交量颜色
    vol_colors = []
    for i, k in enumerate(klines):
        if k["close"] >= k["open"]:
            vol_colors.append(["#c0392b", "#c0392b"])
        else:
            vol_colors.append(["#27ae60", "#27ae60"])

    return f'''<div class="chart-box" id="{chart_id}" style="height:420px"></div>
<script>
(function() {{
  var dom = document.getElementById('{chart_id}');
  if (!dom) return;
  var chart = echarts.init(dom);
  var dates = {json.dumps(dates)};
  var ohlc = {json.dumps(ohlc)};
  var volumes = {json.dumps(volumes)};
  var mid_vals = {json.dumps(mid_vals)};
  var upper_vals = {json.dumps(upper_vals)};
  var lower_vals = {json.dumps(lower_vals)};
  var ma5_vals = {json.dumps(ma5_vals)};
  var ma20_vals = {json.dumps(ma20_vals)};
  var vol_colors = {json.dumps(vol_colors)};
  chart.setOption({{
    animation: false,
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'cross' }} }},
    grid: [{{ left: '10%', right: '5%', top: '5%', height: '55%' }},
           {{ left: '10%', right: '5%', top: '72%', height: '18%' }}],
    xAxis: [{{ type: 'category', data: dates, boundaryGap: true, axisLine: {{ onZero: false }},
              axisLabel: {{ interval: Math.floor(dates.length/8), rotate: 0, fontSize: 11 }} }},
            {{ type: 'category', gridIndex: 1, data: dates, boundaryGap: true,
              axisLabel: {{ show: false }} }}],
    yAxis: [{{ type: 'value', scale: true, splitArea: {{ show: true }},
              axisLabel: {{ fontSize: 11 }} }},
            {{ type: 'value', gridIndex: 1, scale: true,
              axisLabel: {{ fontSize: 10, formatter: function(v) {{ return (v/10000).toFixed(0)+'万'; }} }} }}],
    series: [
      {{ name: '{name}', type: 'candlestick', data: ohlc,
         itemStyle: {{ color: '#c0392b', color0: '#27ae60', borderColor: '#c0392b', borderColor0: '#27ae60' }} }},
      {{ name: 'BOLL上轨', type: 'line', data: upper_vals, symbol: 'none',
         lineStyle: {{ color: '#ffa726', width: 0.8, type: 'dashed' }} }},
      {{ name: 'BOLL中轨', type: 'line', data: mid_vals, symbol: 'none',
         lineStyle: {{ color: '#ffa726', width: 1 }} }},
      {{ name: 'BOLL下轨', type: 'line', data: lower_vals, symbol: 'none',
         lineStyle: {{ color: '#ffa726', width: 0.8, type: 'dashed' }} }},
      {{ name: 'MA5', type: 'line', data: ma5_vals, symbol: 'none',
         lineStyle: {{ color: '#5c6bc0', width: 1 }} }},
      {{ name: 'MA20', type: 'line', data: ma20_vals, symbol: 'none',
         lineStyle: {{ color: '#ef5350', width: 1 }} }},
      {{ name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes,
         itemStyle: {{ color: function(p) {{ return vol_colors[p.dataIndex] ? vol_colors[p.dataIndex][0] : '#999'; }} }} }}
    ]
  }});
  window.addEventListener('resize', function() {{ chart.resize(); }});
}})();
</script>
'''

def build_tech_table_section(title, items, tech_data):
    """生成技术指标表格 HTML。items: [{name, code, pct, price}]。"""
    if not items or not tech_data:
        return ""

    html = f'''<div class="section">
  <div class="section-title">{title}</div>
  <table>
    <thead><tr>
      <th>名称</th><th class="num">最新价</th><th class="num">涨跌幅</th>
      <th class="num">RSI(6)</th><th class="num">MACD信号</th>
      <th class="num">KDJ(J)</th><th class="num">布林带</th>
      <th class="num" style="width:90px">均线</th><th class="num" style="width:70px">成交量</th>
      <th class="num">5日</th><th class="num">20日</th>
    </tr></thead><tbody>
'''
    for it in items:
        t = tech_data.get(it.get("code", ""), {})
        if not t:
            continue
        pct = it.get("pct", 0)
        pct_cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")

        # RSI 颜色
        rsi6 = t.get("rsi6")
        rsi_cls = ""
        if rsi6 is not None:
            if rsi6 > 70: rsi_cls = "up"
            elif rsi6 < 30: rsi_cls = "down"

        # MACD 信号颜色
        macd_sig = t.get("macd_signal", "")
        macd_cls = "up" if "金叉" in macd_sig or "多头" in macd_sig else ("down" if "死叉" in macd_sig or "空头" in macd_sig else "")

        # KDJ 信号
        kdj_sig = t.get("kdj_signal", "")
        kdj_cls = "up" if "金叉" in kdj_sig else ("down" if "死叉" in kdj_sig else "")

        # 均线状态
        ma_status = t.get("ma_status", "")
        ma_cls = "up" if "多头" in ma_status or "站上" in ma_status else ("down" if "空头" in ma_status or "跌破" in ma_status else "")

        chg5 = t.get("chg_5d")
        chg5_cls = "up" if (chg5 or 0) > 0 else "down"
        chg20 = t.get("chg_20d")
        chg20_cls = "up" if (chg20 or 0) > 0 else "down"

        html += f'''      <tr>
        <td>{it["name"]}</td>
        <td class="num">{it.get("price",0):.2f}</td>
        <td class="num {pct_cls}">{pct:+.2f}%</td>
        <td class="num {rsi_cls}">{rsi6 if rsi6 is not None else "-"}</td>
        <td class="num {macd_cls}">{macd_sig or "-"}</td>
        <td class="num {kdj_cls}">{t.get("kdj_j","-") if t.get("kdj_j") is not None else "-"}</td>
        <td class="num" style="font-size:11px">{t.get("boll_signal","-")}</td>
        <td class="num {ma_cls}" style="font-size:11px">{ma_status or "-"}</td>
        <td class="num" style="font-size:11px">{t.get("vol_ratio","-")}</td>
        <td class="num {chg5_cls}">{chg5:+.1f}%</td>
        <td class="num {chg20_cls}">{chg20:+.1f}%</td>
      </tr>
'''
    html += "    </tbody></table></div>\n"
    return html

def build_html_report(all_data, date_str):
    _CHART_ID_COUNTER[0] = 0
    beijing_tz = timezone(timedelta(hours=8))
    now = datetime.now(beijing_tz)
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    display_date = f"{now.strftime('%Y年%m月%d日')}（周{weekdays[now.weekday()]}）"

    overview = all_data.get("overview", {})
    indices = all_data.get("indices", [])
    industries = all_data.get("industries", [])
    concepts = all_data.get("concepts", [])
    fund_flows = all_data.get("fund_flows", [])
    watchlist = all_data.get("watchlist", [])
    news = all_data.get("news", [])
    indices_tech = all_data.get("indices_tech", {})
    watchlist_tech = all_data.get("watchlist_tech", {})
    indices_kline = all_data.get("indices_kline", {})
    watchlist_kline = all_data.get("watchlist_kline", {})

    up_count = overview.get("up", 0)
    down_count = overview.get("down", 0)
    total_stocks = overview.get("total_stocks", 0)
    limit_up = overview.get("limit_up", 0)
    limit_down = overview.get("limit_down", 0)
    total_amount = overview.get("total_amount", 0)

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股收盘复盘 | {display_date}</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:#f0f2f5;color:#1a1a2e;line-height:1.7;font-size:14px}}
.container{{max-width:1200px;margin:0 auto;padding:20px}}
.hero{{background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);color:#fff;border-radius:16px;padding:36px 40px;margin-bottom:20px}}
.hero h1{{font-size:26px;margin-bottom:8px}}
.hero .date{{font-size:14px;color:#a0a0b8;margin-bottom:20px}}
.hero .summary{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}}
.hero .summary-item{{text-align:center;padding:12px;background:rgba(255,255,255,.08);border-radius:10px}}
.hero .summary-item .num{{font-size:28px;font-weight:700}}
.hero .summary-item .num.up{{color:#ff6b6b}}
.hero .summary-item .num.down{{color:#51cf66}}
.hero .summary-item .label{{font-size:12px;color:#a0a0b8;margin-top:4px}}
.section{{background:#fff;border-radius:12px;padding:28px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.section-title{{font-size:20px;font-weight:700;color:#1a1a2e;margin-bottom:20px;padding-left:12px;border-left:4px solid #c0392b}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:#f8f9fa;padding:10px 8px;text-align:left;font-weight:600;color:#555;border-bottom:2px solid #e0e0e0;white-space:nowrap}}
td{{padding:9px 8px;border-bottom:1px solid #f0f0f0}}
tr:hover td{{background:#fafbfc}}
.num{{font-family:"SF Mono","Fira Code",monospace;text-align:right}}
.up{{color:#c0392b}}
.down{{color:#27ae60}}
.flat{{color:#999}}
.chart-box{{width:100%;border-radius:8px;overflow:hidden;margin:10px 0}}
.chart-row{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
@media(max-width:768px){{.chart-row{{grid-template-columns:1fr}}.hero .summary{{grid-template-columns:repeat(2,1fr)}}}}
.board-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.board-col h4{{font-size:15px;margin-bottom:12px;color:#555}}
.board-item{{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #f5f5f5;font-size:13px}}
.board-item .name{{flex:1}}
.board-item .pct{{font-weight:600}}
.news-list{{list-style:none;padding:0}}
.news-list li{{padding:8px 0;border-bottom:1px solid #f5f5f5;font-size:13px}}
.news-list li .time{{color:#999;font-size:11px;margin-right:8px}}
.news-list li .source-tag{{display:inline-block;background:#f0f0f0;color:#888;padding:1px 6px;border-radius:3px;font-size:10px;margin-left:6px}}
.source{{font-size:11px;color:#aaa;text-align:right;margin-top:40px;padding:10px 0}}
</style>
</head>
<body>
<div class="container">
<div class="hero">
  <h1>A股收盘复盘报告</h1>
  <div class="date">{display_date} | 数据来源：腾讯财经 & Sina财经 | 含技术指标分析</div>
  <div class="summary">
    <div class="summary-item">
      <div class="num up">{up_count}</div>
      <div class="label">上涨 / 总数 {total_stocks}</div>
    </div>
    <div class="summary-item">
      <div class="num down">{down_count}</div>
      <div class="label">下跌 / 平盘 {overview.get("flat",0)}</div>
    </div>
    <div class="summary-item">
      <div class="num">{total_amount/10000:.2f}万亿</div>
      <div class="label">两市成交额</div>
    </div>
    <div class="summary-item">
      <div class="num up">{limit_up}</div>
      <div class="label">涨停 / 跌停 <span class="down">{limit_down}</span></div>
    </div>
  </div>
</div>
<div class="section">
  <div class="section-title">一、主要指数表现</div>
  <table>
    <thead><tr><th>指数</th><th class="num">收盘价</th><th class="num">涨跌幅</th><th class="num">涨跌额</th><th class="num">成交额(亿)</th></tr></thead>
    <tbody>
'''
    for idx in indices:
        pct = idx.get("pct", 0)
        cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")
        amt = idx.get("amount", 0)
        chg = idx.get("change", 0)
        html += f'      <tr><td>{idx["name"]}</td><td class="num">{idx.get("price",0):.2f}</td><td class="num {cls}">{pct:+.2f}%</td><td class="num {cls}">{chg:+.2f}</td><td class="num">{amt:.2f}</td></tr>\n'

    html += '''    </tbody></table></div>

<div class="chart-row">
'''
    # 行业板块图表
    industry_names = [b["name"] for b in industries[:10]]
    industry_pcts = [b["pct"] for b in industries[:10]]

    if industries:
        html += f'''  <div class="section" style="margin-bottom:0">
    <div class="section-title">二、行业板块 Top 10</div>
    <div class="chart-box" id="chart-industry" style="height:350px"></div>
    <div style="margin-top:12px;font-size:12px;color:#999">* 申万一级行业，按涨跌幅排序</div>
  </div>
'''
    else:
        html += '''  <div class="section" style="margin-bottom:0">
    <div class="section-title">二、行业板块 Top 10</div>
    <p style="color:#999;padding:40px 0;text-align:center">板块数据暂不可用 (东方财富API在云端受限)</p>
  </div>
'''
    if fund_flows:
        html += f'''  <div class="section" style="margin-bottom:0">
    <div class="section-title">三、行业资金流向</div>
    <div class="chart-box" id="chart-fundflow" style="height:350px"></div>
    <div style="margin-top:12px;font-size:12px;color:#999">* 主力净流入 TOP/BOTTOM 5（亿元）</div>
  </div>
'''
    else:
        html += '''  <div class="section" style="margin-bottom:0">
    <div class="section-title">三、行业资金流向</div>
    <p style="color:#999;padding:40px 0;text-align:center">资金流向数据暂不可用 (东方财富API在云端受限)</p>
  </div>
'''

    html += '''</div>

<div class="section">
  <div class="section-title">四、概念板块热力图</div>
'''
    if concepts:
        html += '''  <div class="board-grid">
    <div class="board-col">
      <h4>涨幅 Top 10</h4>
'''
        for b in concepts[:10]:
            cls = "up" if b["pct"] > 0 else "down"
            html += f'      <div class="board-item"><span class="name">{b["name"]}</span><span class="pct {cls}">{b["pct"]:+.2f}%</span></div>\n'

        html += '''    </div>
    <div class="board-col">
      <h4>表现最弱 Top 10</h4>
'''
        weakest = sorted(concepts[-10:], key=lambda x: x["pct"])
        for b in weakest:
            cls = "up" if b["pct"] > 0 else "down"
            html += f'      <div class="board-item"><span class="name">{b["name"]}</span><span class="pct {cls}">{b["pct"]:+.2f}%</span></div>\n'

        html += '''    </div></div></div>
'''
    else:
        html += '''  <p style="color:#999;padding:40px 0;text-align:center">概念板块数据暂不可用 (东方财富API在云端受限)</p>
</div>
'''

    html += '''<div class="section">
  <div class="section-title">五、自选股表现</div>
  <table>
    <thead><tr><th>股票</th><th class="num">收盘价</th><th class="num">涨跌幅</th><th class="num">换手率</th><th class="num">量比</th><th class="num">成交额(亿)</th></tr></thead>
    <tbody>
'''
    for s in watchlist:
        pct = s.get("pct", 0)
        cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")
        amt = s.get("amount", 0)
        html += f'''      <tr><td>{s["name"]} <span style="color:#999;font-size:11px">({s["code"]})</span></td>
        <td class="num">{s.get("price",0):.2f}</td>
        <td class="num {cls}">{pct:+.2f}%</td>
        <td class="num">{s.get("turnover",0):.2f}%</td>
        <td class="num">{s.get("volume_ratio",0):.2f}</td>
        <td class="num">{amt:.2f}</td></tr>
'''

    html += '''    </tbody></table></div>
'''

    # === 六、上证指数K线（大盘参考）===
    html += '''<div class="section">
  <div class="section-title">六、上证指数 K线 + 布林带 + 成交量（大盘参考）</div>
'''
    sh_kline = indices_kline.get("000001", [])
    if sh_kline:
        html += build_kline_chart_placeholder("chart-sh-kline", "上证指数", sh_kline)
    else:
        html += '<p style="color:#999">K线数据获取失败</p>'
    html += '</div>\n'

    # 指数技术指标表格
    html += build_tech_table_section("七、指数技术指标", indices, indices_tech)

    # === 八、自选股 K线图（全部A股）===
    html += '<div class="section"><div class="section-title">八、自选股 K线图（技术分析）</div>\n'
    html += '<p style="color:#999;font-size:12px;margin-bottom:16px">每只自选股60日K线，含BOLL上/中/下轨 + MA5/MA20均线 + 成交量，MACD/KDJ信号标注在标题中</p>\n'

    # 构建自选股列表，港股跳过（K线API不同）
    stock_kline_items = []
    for s in watchlist:
        klines = watchlist_kline.get(s["code"], [])
        t = watchlist_tech.get(s["code"], {})
        if klines and len(klines) >= 20:
            sig_parts = []
            if t.get("macd_signal"): sig_parts.append(t["macd_signal"])
            if t.get("kdj_signal"): sig_parts.append(t["kdj_signal"])
            sig_str = " ".join(sig_parts)
            title = f'{s["name"]} ({sig_str})' if sig_str else s["name"]
            stock_kline_items.append((title, klines))

    if stock_kline_items:
        # 两列网格排列
        html += '<div class="chart-row">\n'
        for title, klines in stock_kline_items:
            cid = _next_chart_id()
            html += f'<div>{build_kline_chart_placeholder(cid, title, klines)}</div>\n'
        html += '</div>\n'
    else:
        html += '<p style="color:#999">自选股K线数据获取失败</p>\n'
    html += '</div>\n'

    # === 自选股技术指标表格 ===
    html += build_tech_table_section("九、自选股技术指标扫描", watchlist, watchlist_tech)

    # === 要闻 ===
    html += '''<div class="section">
  <div class="section-title">十、今日要闻</div>
  <ul class="news-list">
'''
    for n in news:
        time_str = n.get("time", "")
        if time_str and len(time_str) >= 16:
            time_str = time_str[11:16]  # HH:MM
        source_str = n.get("source", "")
        html += f'    <li><span class="time">{time_str}</span>{n["title"]}<span class="source-tag">{source_str}</span></li>\n'
    if not news:
        html += '    <li style="color:#999">暂无重要新闻（API 限流，建议晚间刷新查看）</li>\n'

    html += f'''  </ul></div>
<div class="source">
  以上数据由云端自动化生成，仅供参考，不构成投资建议 | 生成时间：{now.strftime("%Y-%m-%d %H:%M:%S")} | 含 K线/BOLL/MACD/KDJ/RSI 技术指标
</div></div>

<script>
// 行业板块图表
(function() {{
  var dom = document.getElementById('chart-industry');
  if (!dom) return;
  var chart = echarts.init(dom);
  chart.setOption({{
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }},
    grid: {{ left: '12%', right: '5%', top: '3%', bottom: '3%', containLabel: true }},
    xAxis: {{ type: 'value', axisLabel: {{ formatter: '{{value}}%' }} }},
    yAxis: {{ type: 'category', data: {json.dumps(industry_names[::-1])}, axisLabel: {{ fontSize: 12 }} }},
    series: [{{ type: 'bar', data: {json.dumps([round(p,2) for p in industry_pcts[::-1]])},
      itemStyle: {{ color: function(p) {{ return p.value > 0 ? '#c0392b' : '#27ae60'; }} }},
      label: {{ show: true, position: 'right', formatter: '{{c}}%', fontSize: 11 }}
    }}]
  }});
  window.addEventListener('resize', function() {{ chart.resize(); }});
}})();

// 资金流向图表
(function() {{
  var dom = document.getElementById('chart-fundflow');
  if (!dom) return;
  var chart = echarts.init(dom);
  var flowData = {json.dumps([{"name": f["name"], "value": round(f["main_net"], 2)} for f in (fund_flows[:5] + fund_flows[-5:])])};
  var names = flowData.map(function(d) {{ return d.name; }});
  var values = flowData.map(function(d) {{ return d.value; }});
  chart.setOption({{
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }},
    grid: {{ left: '12%', right: '5%', top: '3%', bottom: '3%', containLabel: true }},
    xAxis: {{ type: 'value', axisLabel: {{ formatter: '{{value}}亿' }} }},
    yAxis: {{ type: 'category', data: names.reverse(), axisLabel: {{ fontSize: 11 }} }},
    series: [{{ type: 'bar', data: values.reverse(),
      itemStyle: {{ color: function(p) {{ return p.value > 0 ? '#c0392b' : '#27ae60'; }} }},
      label: {{ show: true, position: 'right', formatter: '{{c}}亿', fontSize: 11 }}
    }}]
  }});
  window.addEventListener('resize', function() {{ chart.resize(); }});
}})();
</script>
</body></html>'''
    return html

def generate_report(all_data, output_dir="reports"):
    os.makedirs(output_dir, exist_ok=True)
    beijing_tz = timezone(timedelta(hours=8))
    date_str = datetime.now(beijing_tz).strftime("%Y-%m-%d")
    filename = f"{output_dir}/{date_str}.html"
    html = build_html_report(all_data, date_str)
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  报告已生成: {filename} ({len(html)} bytes)")
    return filename

def get_report_url(date_str=None):
    if date_str is None:
        beijing_tz = timezone(timedelta(hours=8))
        date_str = datetime.now(beijing_tz).strftime("%Y-%m-%d")
    return f"{GITHUB_PAGES_BASE}/reports/{date_str}.html"

def build_summary_md(all_data):
    overview = all_data.get("overview", {})
    indices = all_data.get("indices", [])
    watchlist = all_data.get("watchlist", [])
    indices_tech = all_data.get("indices_tech", {})
    watchlist_tech = all_data.get("watchlist_tech", {})
    news = all_data.get("news", [])

    up = overview.get("up", 0)
    down = overview.get("down", 0)
    limit_up = overview.get("limit_up", 0)
    limit_down = overview.get("limit_down", 0)
    total_amount = overview.get("total_amount", 0)

    beijing_tz = timezone(timedelta(hours=8))
    now = datetime.now(beijing_tz)
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    display_date = f"{now.strftime('%Y-%m-%d')}（周{weekdays[now.weekday()]}）"

    md = f"### A股收盘复盘 v7\n**{display_date}**\n\n---\n\n"
    md += f"**市场概况：** 上涨 **{up}** 家 / 下跌 **{down}** 家 | 涨停 **{limit_up}** / 跌停 **{limit_down}** | 成交 **{total_amount:.0f}** 亿\n\n"

    md += "**主要指数：**\n"
    for idx in indices:
        pct = idx.get("pct", 0)
        emoji = "🔴" if pct > 0 else ("🟢" if pct < 0 else "⚪")
        t = indices_tech.get(idx.get("code", ""), {})
        rsi_str = f" RSI6:{t.get('rsi6','-')}" if t else ""
        md += f"- {emoji} **{idx['name']}**: {idx.get('price',0):.2f} ({pct:+.2f}%){rsi_str}\n"

    # 上证指数技术信号
    sh_tech = indices_tech.get("000001", {})
    if sh_tech:
        md += f"\n**上证指数技术信号：** {sh_tech.get('ma_status','')} | {sh_tech.get('macd_signal','')} | {sh_tech.get('boll_signal','')}\n"

    # 自选股涨跌幅前三
    a_stocks = [s for s in watchlist if s["market"] == "A"]
    a_stocks.sort(key=lambda x: x.get("pct", 0), reverse=True)
    if a_stocks:
        md += f"\n**自选股涨幅前三：**\n"
        for s in a_stocks[:3]:
            st = watchlist_tech.get(s["code"], {})
            sig = f" — {st.get('macd_signal','')}" if st.get("macd_signal") else ""
            md += f"- {s['name']}: {s.get('pct',0):+.2f}%{sig}\n"
        md += f"\n**自选股跌幅前三：**\n"
        for s in a_stocks[-3:]:
            st = watchlist_tech.get(s["code"], {})
            sig = f" — {st.get('macd_signal','')}" if st.get("macd_signal") else ""
            md += f"- {s['name']}: {s.get('pct',0):+.2f}%{sig}\n"

    # 技术预警
    alerts = []
    for s in a_stocks:
        st = watchlist_tech.get(s["code"], {})
        if st:
            macd = st.get("macd_signal", "")
            kdj = st.get("kdj_signal", "")
            if "金叉" in macd or "死叉" in macd or "超买" in kdj or "超卖" in kdj:
                alerts.append(f"{s['name']}: {macd} {kdj}".strip())
    if alerts:
        md += f"\n**技术信号预警：**\n"
        for a in alerts[:5]:
            md += f"- ⚡ {a}\n"

    # 要闻
    if news:
        md += "\n**今日要闻：**\n"
        for n in news[:3]:
            md += f"- {n['title']}\n"

    report_url = get_report_url()
    md += f"\n[查看完整复盘报告]({report_url})\n"

    return md