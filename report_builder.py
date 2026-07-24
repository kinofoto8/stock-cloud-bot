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
        resp.encoding = resp.apparent_encoding or 'gbk'
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

def get_kline_tencent_hk(symbol, datalen=60):
    """获取腾讯港股日K线数据。
    港股K线API返回额外字段: index[7]=换手率(%), index[8]=成交额(万港元)
    返回: [{date, open, close, high, low, volume, amount, pct, turnover}, ...]
    """
    url = f"https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?param={symbol},day,,,{datalen},qfq"
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
            # [date, open, close, high, low, volume, {}, turnover, amount_wan]
            close = safe_float(k[2]) if len(k) > 2 else 0
            open_p = safe_float(k[1]) if len(k) > 1 else 0
            high = safe_float(k[3]) if len(k) > 3 else 0
            low = safe_float(k[4]) if len(k) > 4 else 0
            volume = safe_float(k[5]) if len(k) > 5 else 0
            dt = k[0] if len(k) > 0 else ""
            turnover = safe_float(k[7]) if len(k) > 7 else 0  # 换手率(%)
            amount_wan = safe_float(k[8]) if len(k) > 8 else 0  # 成交额(万港元)
            amount_yi = amount_wan / 1e4  # 万港元 → 亿港元

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
                "amount": amount_yi if amount_yi > 0 else volume * close * 1e-8,  # 亿港元
                "amplitude": round((high - low) / prev_close * 100, 2) if prev_close and prev_close > 0 else 0,
                "pct": pct,
                "change": round(close - prev_close, 4) if prev_close else 0,
                "turnover": turnover,
            })
            prev_close = close
        return result
    except Exception as e:
        print(f"  [WARN] 腾讯港股K线请求异常 {symbol}: {e}")
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
            resp.encoding = 'utf-8'
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
        resp.encoding = 'utf-8'
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
            resp.encoding = 'utf-8'
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
            resp.encoding = 'utf-8'
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
    tencent_symbol: 腾讯格式代码 (sh000001, sz000426, hk00883 等)
    自动识别港股(hk前缀)并使用港股专用K线API。
    """
    is_hk = tencent_symbol.startswith("hk")
    # 腾讯 K线 (港股用专用API)
    if is_hk:
        klines = get_kline_tencent_hk(tencent_symbol, days)
    else:
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
    resp.encoding = resp.apparent_encoding or 'gbk'
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

# ============================================================
# 腾讯看板数据 (三级回退：EM → 腾讯 → Sina)
# ============================================================
_TENCENT_BOARD_CACHE = None

def _get_tencent_board_data():
    """获取腾讯看板数据 (带缓存, 一次请求获取行业/概念/资金流向)。"""
    global _TENCENT_BOARD_CACHE
    if _TENCENT_BOARD_CACHE is not None:
        return _TENCENT_BOARD_CACHE

    url = "https://web.ifzq.gtimg.cn/appstock/app/board/index?board=all"
    try:
        resp = SESSION.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        _TENCENT_BOARD_CACHE = data.get("data", {})
        return _TENCENT_BOARD_CACHE
    except Exception as e:
        print(f"  [WARN] 腾讯看板API失败: {e}")
        _TENCENT_BOARD_CACHE = {}
        return _TENCENT_BOARD_CACHE

def _parse_tencent_boards(rank_key="plate"):
    """从腾讯看板 rank 数据解析行业/概念板块。
    rank_key: "plate" (行业) 或 "concept" (概念)
    返回 [{"name","code","pct","price","rise_count","fall_count"}, ...]
    腾讯只返回 top 6, 按涨跌幅排序。
    """
    data = _get_tencent_board_data()
    rank = data.get("rank", {})
    items = rank.get(rank_key, [])

    boards = []
    for it in items:
        pct_val = it.get("bd_zdf", "0")
        try:
            pct_val = float(pct_val)
        except (ValueError, TypeError):
            pct_val = 0

        price_val = it.get("bd_zxj", "0")
        try:
            price_val = float(price_val)
        except (ValueError, TypeError):
            price_val = 0

        boards.append({
            "name": it.get("bd_name", ""),
            "code": it.get("bd_code", ""),
            "pct": pct_val,
            "price": price_val,
            "rise_count": 0,
            "fall_count": 0,
            "flat_count": 0,
        })
    return boards


def get_industry_boards():
    """行业板块 — EM(申万一级) > 腾讯(申万二级top6) > Sina(不推荐)
    m:90+t:1 = 申万一级行业(31个), m:90+t:2 = 申万二级(100+含子类)"""
    print("  [3/5] 获取行业板块...")
    # 尝试 EM clist (申万一级行业, GitHub Actions可用)
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:90+t:1",  # 申万一级行业 (t:2是二级, 会混入"铜""铝"等子类)
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
        # 检测地区板块污染: EM m:90+t:1 有时返回"青海板块""宁夏板块"等地区板块
        regional_count = sum(1 for b in boards if "板块" in b["name"])
        if regional_count > 3:
            print(f"  [WARN] EM返回地区板块({regional_count}个含'板块'), 回退腾讯/Sina...")
            bad_names = [b["name"] for b in boards if "板块" in b["name"]]
            print(f"  [WARN] 地区板块示例: {bad_names[:5]}")
        else:
            print(f"  [OK] EM申万一级行业: {len(boards)}个")
            return boards

    # EM 失败 → 腾讯看板 (申万二级, top 6)
    print("  [INFO] EM行业API不可用, 尝试腾讯看板...")
    tencent_boards = _parse_tencent_boards("plate")
    if tencent_boards:
        print(f"  [INFO] 腾讯行业板块(申万二级): {len(tencent_boards)}个")
        # 腾讯回退只有 top 6, 不足15个时追加Sina补充
        if len(tencent_boards) < 15:
            print(f"  [INFO] 腾讯行业不足15个({len(tencent_boards)}), 尝试Sina补充...")
            try:
                sina_boards = _parse_sina_boards(
                    "https://money.finance.sina.com.cn/q/view/newSinaHy.php",
                    "S_Finance_bankuai_sinaindustry"
                )
                if sina_boards:
                    existing_names = {b["name"] for b in tencent_boards}
                    new_from_sina = [b for b in sina_boards if b["name"] not in existing_names]
                    print(f"  [INFO] Sina补充行业: {len(sina_boards)}个 → 去重后新增 {len(new_from_sina)}个")
                    tencent_boards.extend(new_from_sina)
            except Exception as e:
                print(f"  [WARN] Sina行业补充失败: {e}")
        return tencent_boards

    # 腾讯失败 → Sina (最不可靠, 数值大小和分类都不对)
    print("  [INFO] 腾讯行业也失败, 切换Sina备用源...")
    try:
        boards = _parse_sina_boards(
            "https://money.finance.sina.com.cn/q/view/newSinaHy.php",
            "S_Finance_bankuai_sinaindustry"
        )
        if boards:
            print(f"  [INFO] Sina行业板块: {len(boards)}个 (注意: 分类和数值可能与申万不同)")
        return boards
    except Exception as e:
        print(f"  [WARN] Sina行业板块也失败: {e}")
        return []

# 概念板块黑名单: 技术形态/风格/指数类, 不属于真正"概念板块"
_CONCEPT_BLACKLIST = [
    "连板", "打板", "涨停", "首板", "多板", "二板",
    "高换手", "高振幅", "热股",
    "题材股", "反转股",
    "中盘股", "大盘股", "小盘股", "龙头股", "价值股", "成长股",
    "上证50", "沪深300", "MSCI", "标准普尔", "央视",
    "历史新高", "新股", "次新股",
    "TMT", "中特估",
]

_CONCEPT_NO_LEADING = [
    "昨日", "最近", "今日",
]

def _is_valid_concept(name):
    """判断是否为有效的概念板块(排除技术形态/风格/指数类)。"""
    if not name:
        return False
    for banned in _CONCEPT_NO_LEADING:
        if name.startswith(banned):
            return False
    for banned in _CONCEPT_BLACKLIST:
        if banned in name:
            return False
    return True


def get_concept_boards():
    """概念板块 — EM(过滤后) > 腾讯 > Sina
    过滤掉连板/打板/涨停/高换手等技术形态, 仅保留真正概念板块。"""
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
        all_boards = []
        filtered = []
        for it in items:
            name = it.get("f14", "")
            pct = safe_float(it.get("f3"))
            board = {
                "name": name,
                "code": it.get("f12", ""),
                "pct": pct,
                "price": safe_float(it.get("f2")),
            }
            all_boards.append(board)
            if _is_valid_concept(name):
                filtered.append(board)
        filtered.sort(key=lambda x: x["pct"], reverse=True)
        filtered_count = len(all_boards) - len(filtered)
        print(f"  [OK] EM概念板块: {len(all_boards)}个 → 过滤后 {len(filtered)}个 (排除{filtered_count}个技术形态/风格类)")
        return filtered

    # EM 失败 → 腾讯看板 (也经过概念过滤)
    print("  [INFO] EM概念API不可用, 尝试腾讯看板...")
    tencent_boards = _parse_tencent_boards("concept")
    sina_boards = []  # 用于补充腾讯数据不足时
    if tencent_boards:
        tencent_boards.sort(key=lambda x: x["pct"], reverse=True)
        # 对腾讯回退数据也应用概念过滤
        filtered_t = [b for b in tencent_boards if _is_valid_concept(b.get("name", ""))]
        filtered_t_count = len(tencent_boards) - len(filtered_t)
        print(f"  [INFO] 腾讯概念板块: {len(tencent_boards)}个 → 过滤后 {len(filtered_t)}个 (排除{filtered_t_count}个)")

        # 腾讯回退只有 top 6, 过滤后可能更少; 不足15个时追加Sina补充
        if len(filtered_t) < 15:
            print(f"  [INFO] 腾讯概念不足15个({len(filtered_t)}), 尝试Sina补充...")
            try:
                sina_boards = _parse_sina_boards(
                    "https://money.finance.sina.com.cn/q/view/newFLJK.php?param=class",
                    "S_Finance_bankuai_class"
                )
                if sina_boards:
                    filtered_s = [b for b in sina_boards if _is_valid_concept(b.get("name", ""))]
                    # 去重: 已存在于腾讯数据中的不再追加
                    existing_names = {b["name"] for b in filtered_t}
                    new_from_sina = [b for b in filtered_s if b["name"] not in existing_names]
                    print(f"  [INFO] Sina补充概念: {len(filtered_s)}个 → 去重后新增 {len(new_from_sina)}个")
                    filtered_t.extend(new_from_sina)
                    filtered_t.sort(key=lambda x: x["pct"], reverse=True)
                else:
                    print("  [INFO] Sina概念也无数据")
            except Exception as e:
                print(f"  [WARN] Sina概念补充失败: {e}")
        print(f"  [INFO] 最终概念板块: {len(filtered_t)}个")
        return filtered_t

    # 腾讯也失败 → Sina (也经过概念过滤)
    print("  [INFO] 腾讯概念也失败, 切换Sina备用源...")
    try:
        boards = _parse_sina_boards(
            "https://money.finance.sina.com.cn/q/view/newFLJK.php?param=class",
            "S_Finance_bankuai_class"
        )
        if boards:
            filtered_s = [b for b in boards if _is_valid_concept(b.get("name", ""))]
            filtered_s_count = len(boards) - len(filtered_s)
            print(f"  [INFO] Sina概念板块: {len(boards)}个 → 过滤后 {len(filtered_s)}个 (排除{filtered_s_count}个)")
            return filtered_s
        return []
    except Exception as e:
        print(f"  [WARN] Sina概念板块也失败: {e}")
        return []

def get_industry_fund_flow():
    """行业资金流向 — EM(申万一级) > 腾讯 fundflow
    m:90+t:1 确保不会出现"铜""铝"等二级子类独立显示。"""
    print("  [5/5] 获取资金流向...")
    # 尝试 EM (最全)
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fid": "f3",  # 用涨跌幅排序(非f62), 否则t:1会返回地区板块!
        "fs": "m:90+t:1",  # 申万一级 (t:2会混入"铜""铝"等子类)
        "fields": "f2,f3,f12,f14,f62,f184,f66,f72,f78,f84",
    }
    data = em_fetch_json(url, params)
    items = (data.get("data") or {}).get("diff", [])

    if items:
        # 安全验证: EM有时会用fid=f62返回地区板块(福建板块/北京板块)而非申万一级
        # 如果名称含"板块"且数量约31个(省份数), 则是地区数据, 需要回退
        first_name = items[0].get("f14", "")
        regional_like = [it for it in items[:5] if "板块" in it.get("f14", "")]
        if len(regional_like) >= 3:
            print(f"  [WARN] EM返回地区板块({first_name}等)而非申万一级行业, 切换腾讯备用源")
            items = []

    if items:
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
        print(f"  [OK] EM资金流向: {len(flows)}个行业")
        return flows

    # EM 失败 → 腾讯 fundflow (top/bottom 各3个)
    print("  [INFO] EM资金流向不可用, 尝试腾讯 fundflow...")
    tencent_data = _get_tencent_board_data()
    ff_plate = tencent_data.get("fundflow", {}).get("plate", {})

    inflow = ff_plate.get("top", [])
    outflow = ff_plate.get("bottom", [])

    flows = []
    for it in inflow:
        zljlr = safe_float(it.get("zljlr")) / 1e4  # 万 → 亿
        flows.append({
            "name": it.get("name", ""),
            "pct": safe_float(it.get("zdf")),
            "main_net": zljlr,
            "super_large_net": 0,
            "large_net": 0,
            "medium_net": 0,
            "small_net": 0,
            "main_pct": 0,
        })
    for it in outflow:
        zljlr = safe_float(it.get("zljlr")) / 1e4
        flows.append({
            "name": it.get("name", ""),
            "pct": safe_float(it.get("zdf")),
            "main_net": zljlr,
            "super_large_net": 0,
            "large_net": 0,
            "medium_net": 0,
            "small_net": 0,
            "main_pct": 0,
        })

    if flows:
        flows.sort(key=lambda x: x["main_net"], reverse=True)
        print(f"  [INFO] 腾讯资金流向: {len(flows)}个 (top/bottom 各3)")
    else:
        print("  [INFO] 腾讯资金流向也无数据")
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
# 新闻获取 — 多源备用 (同花顺优先, 东方财富/Sina备用)
# ============================================================
def get_market_news():
    """获取24小时内市场重要新闻，多源备用。
    优先使用同花顺实时新闻API（返回24小时内的滚动新闻），
    其次东方财富要闻，最后Sina财经滚动。
    """
    print("    获取要闻...")

    # 方案 A: 同花顺实时财经新闻 (24小时滚动)
    try:
        resp = SESSION.get(
            "https://news.10jqka.com.cn/tapp/news/push/stock",
            params={"page": "1", "tag": "", "track": "website", "num": "20"},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://news.10jqka.com.cn/"}
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", {}).get("list", [])
        if items:
            print(f"  [新闻] 同花顺来源: {len(items)} 条")
            news = []
            for it in items[:12]:
                ctime = it.get("ctime", "")
                if ctime:
                    try:
                        ctime = datetime.fromtimestamp(int(ctime)).strftime("%m-%d %H:%M")
                    except Exception:
                        pass
                news.append({
                    "title": it.get("title", ""),
                    "time": ctime,
                    "source": "同花顺",
                })
            return news
    except Exception as e:
        print(f"  [新闻] 同花顺来源失败: {e}")

    # 方案 B: 东方财富市场要闻
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

    # 方案 C: Sina 财经滚动新闻 (lid=2510 股票频道)
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
# 市场估值 — PE数据
# ============================================================
def get_market_pe():
    """获取上证指数PE(TTM)，EM API获取。失败时返回None。"""
    try:
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {"secid": "1.000001", "fields": "f162,f167,f170"}
        resp = SESSION.get(url, params=params, timeout=10)
        data = resp.json().get("data", {})
        pe_ttm = safe_float(data.get("f167"))
        if pe_ttm and pe_ttm > 0:
            print(f"  [PE] 上证PE(TTM): {pe_ttm:.2f}")
            return round(pe_ttm, 2)
        pe_dyn = safe_float(data.get("f162"))
        if pe_dyn and pe_dyn > 0:
            print(f"  [PE] 上证PE(动态): {pe_dyn:.2f}")
            return round(pe_dyn, 2)
    except Exception as e:
        print(f"  [PE] 获取失败: {e}")
    return None


# ============================================================
# 板块逻辑分析引擎 — 自动生成涨跌原因
# ============================================================
SECTOR_LOGIC_MAP = {
    "贵金属": "金价走强+避险情绪升温，资金涌入黄金股",
    "黄金": "金价走强+避险情绪升温，资金涌入黄金股",
    "油服工程": "国际油价上涨+中东局势支撑油服景气",
    "工业金属": "铜铝等基本金属反弹，供需紧平衡预期",
    "有色金属": "铜铝等基本金属反弹，供需紧平衡预期",
    "炼化及贸易": "原油产业链联动+炼化盈利改善",
    "农化制品": "化肥农药旺季需求+粮食安全主题",
    "种植业": "粮食安全主题+农产品涨价预期",
    "养殖业": "猪周期回暖+养殖盈利改善",
    "煤炭开采": "能源保供政策+季节性需求支撑",
    "电力": "高股息防御属性+夏季用电高峰+电力改革",
    "保险": "险企集体出手+估值修复+利率企稳",
    "银行": "高股息防御+资产质量改善+估值修复",
    "证券": "市场活跃度提升+IPO回暖+自营弹性",
    "白酒": "消费复苏预期+中秋备货行情+估值修复",
    "汽车整车": "新能源车销量超预期+政策刺激+出海加速",
    "半导体": "AI算力需求+国产替代加速+周期复苏",
    "通信设备": "AI算力硬件+5G-A升级+光通信景气",
    "元件": "AI服务器PCB+消费电子备货+国产替代",
    "光学光电子": "AR/VR新品周期+面板涨价+车载光学",
    "电子化学品": "半导体材料国产化+先进封装需求",
    "玻璃玻纤": "地产链需求疲弱+行业产能过剩担忧",
    "装修建材": "地产竣工低迷+需求不振+成本压力",
    "房地产开发": "销售数据低迷+融资压力+政策效果待观察",
    "医药商业": "集采政策影响+渠道库存去化+行业整合",
    "医疗器械": "集采扩面压力+出口不确定性+估值消化",
    "光伏设备": "产能过剩担忧+价格战持续+海外贸易摩擦",
    "风电设备": "招标价格下行+并网消纳问题+补贴退坡",
    "电池": "锂电产能过剩+价格竞争激烈+增速放缓",
    "能源金属": "锂盐价格低迷+供过于求+需求增速放缓",
    "航空机场": "暑期出行旺季+国际航线恢复+油价利好",
    "旅游酒店": "暑期旅游旺季+入境游增长+消费券刺激",
    "军工": "国防预算增长+装备列装加速+地缘催化",
    "钢铁": "基建投资发力+限产预期+铁矿石成本支撑",
    "航运港口": "运费反弹+全球贸易回暖+港口吞吐量增长",
    "建筑装饰": "基建投资拉动+新型城镇化+一带一路",
    "环保": "化债政策受益+估值低位+政策催化",
    "计算机": "信创推进+AI应用落地+数据要素政策",
    "传媒": "AI赋能内容+游戏版号常态化+短剧出海",
    "食品饮料": "消费复苏+成本下降+股息率吸引力",
    "纺织服饰": "出口订单回暖+品牌出海+原材料降价",
    "家用电器": "出海加速+以旧换新政策+成本改善",
    "美容护理": "消费升级+国货替代+渠道变革",
    "社会服务": "服务消费回暖+职业教育政策+灵活用工",
    "公用事业": "高股息防御+夏季用电高峰+水价改革",
    "石油石化": "油价高位+炼化盈利+央企估值修复",
    "基础化工": "化工品涨价+供给收缩+新能源材料需求",
    "机械设备": "设备更新政策+自动化升级+出海逻辑",
    "国防军工": "国防预算增长+装备列装加速+地缘催化",
    "商贸零售": "消费券刺激+线下回暖+即时零售增长",
    "交通运输": "出行数据向好+物流复苏+高速公路车流增长",
    "农林牧渔": "猪周期反转+粮价上涨+种业振兴",
    "非银金融": "资本市场活跃+险企投资改善+券商弹性",
    "钢铁行业": "基建投资发力+限产预期+铁矿石成本支撑",
    "有色金属行业": "铜铝等基本金属反弹，供需紧平衡预期",
    # === 申万一级补充 ===
    "电子": "AI算力需求+国产替代+半导体周期复苏",
    "通信": "AI算力硬件+5G-A升级+光通信景气",
    "医药生物": "创新药出海+医疗器械国产化+估值修复",
    "建筑材料": "地产链拖累+水泥需求疲弱+玻璃产能过剩",
    "电力设备": "光伏产能过剩+锂电价格战+电网投资支撑",
    "汽车": "新能源车销量超预期+智能驾驶落地+出海加速",
    "房地产": "销售数据低迷+融资压力+政策效果待观察",
    "轻工制造": "出口订单回暖+原材料价格下降+内需恢复",
    "综合": "多业务布局+主题催化+资金轮动",
}

SECTOR_WEAK_MAP = {
    "玻璃玻纤": "地产链需求疲弱+行业产能过剩担忧",
    "通信设备": "AI算力硬件高位回调，资金获利了结",
    "元件": "电子元器件跟随科技板块调整",
    "光学光电子": "消费电子需求不确定+估值消化",
    "电子化学品": "半导体材料板块获利回吐",
    "半导体": "前期涨幅过大，短线获利回吐",
    "电池": "锂电产能过剩+价格竞争激烈+下游需求放缓",
    "能源金属": "锂盐价格低迷+供过于求+新能源车增速放缓",
    "光伏设备": "产能过剩担忧+价格战加剧+海外贸易壁垒",
    "风电设备": "招标价格下行+并网消纳困难+补贴退坡",
    "房地产开发": "销售数据低迷+资金链紧张+市场信心不足",
    "装修建材": "地产竣工低迷+需求不振+成本压力",
    "医药商业": "集采政策持续影响+渠道库存高企",
    "医疗器械": "集采扩面+出口不确定性+估值消化",
    "食品饮料": "消费复苏不及预期+竞争加剧",
    "汽车整车": "价格战持续+需求透支+出口不确定性",
    "计算机": "估值偏高+业绩兑现压力+资金获利了结",
    "传媒": "前期涨幅过大+AI概念降温+监管趋严",
    "家用电器": "海外需求走弱+原材料涨价+汇率波动",
    "纺织服饰": "消费降级+库存压力+品牌竞争加剧",
    "钢铁": "需求季节性走弱+成本支撑减弱+限产不确定性",
    "煤炭开采": "煤价回落+季节性需求转淡+新能源替代",
    "社会服务": "消费复苏低于预期+估值偏高",
    "商贸零售": "消费疲弱+电商分流+线下客流低迷",
    # === 申万一级补充 ===
    "电子": "前期涨幅过大+科技股获利回吐+估值消化",
    "通信": "AI算力硬件回调+资金获利了结+5G投资节奏放缓",
    "医药生物": "集采政策持续影响+创新药研发不确定性+估值承压",
    "建筑材料": "地产链持续低迷+水泥需求疲弱+行业产能过剩",
    "电力设备": "光伏锂电产能过剩+价格战持续+海外贸易壁垒",
    "房地产": "销售数据低迷+融资压力大+市场信心不足",
    "轻工制造": "出口不确定性+内需疲弱+成本上升",
    "综合": "缺乏主线+资金分散+市场风格轮动",
}


def _get_sector_reason(name, pct):
    """根据板块名称和涨跌幅生成逻辑说明。"""
    # 精确匹配
    if pct > 0 and name in SECTOR_LOGIC_MAP:
        return SECTOR_LOGIC_MAP[name]
    if pct < 0 and name in SECTOR_WEAK_MAP:
        return SECTOR_WEAK_MAP[name]
    # 模糊匹配
    for key, logic in (SECTOR_LOGIC_MAP if pct > 0 else SECTOR_WEAK_MAP).items():
        if key in name:
            return logic
    # 通用描述
    if pct > 0:
        return "资金流入+行业景气度提升"
    return "行业调整+资金流出"


# ============================================================
# 自选股备注生成 + 中期展望 + 明日关注引擎
# ============================================================
STOCK_SECTOR = {
    "601899": "有色金属龙头大涨",
    "000426": "白银概念强势",
    "600489": "黄金股普涨",
    "000408": "锂矿反弹",
    "600331": "有色资源强势",
    "002240": "锂能小幅收涨/承压",
    "588170": "科技股回调/反弹",
    "600988": "黄金股领涨自选",
    "000807": "铝业小幅跟涨",
    "000933": "煤炭铝业双轮驱动",
    "00883": "港股油服强势",
    "09992": "港股消费回调/反弹",
    "02259": "港股黄金暴涨",
}

STOCK_SECTOR_LABEL = {
    "601899": "工业金属/有色金属",
    "000426": "工业金属/白银",
    "600489": "贵金属/黄金",
    "000408": "能源金属/锂矿",
    "600331": "工业金属/有色金属",
    "002240": "能源金属/锂电",
    "588170": "半导体/科技",
    "600988": "贵金属/黄金",
    "000807": "工业金属/铝",
    "000933": "煤炭/铝业",
    "00883": "油服工程/能源",
    "09992": "消费/港股",
    "02259": "贵金属/黄金",
}

MIDTERM_SECTOR_TEMPLATES = {
    # 申万一级: 有色金属 (覆盖贵金属、工业金属等二级子类)
    "有色金属": {
        "recent": "有色金属板块整体走强，贵金属和工业金属领涨",
        "logic": "美联储降息预期升温推升金价，地缘风险提供避险需求；铜铝供给端约束支撑价格，资源股估值修复空间大",
        "stocks": ["赤峰黄金", "中金黄金", "紫金矿业", "兴业银锡", "紫金黄金国际"],
        "aliases": ["贵金属", "工业金属"],
    },
    # 兼容旧版腾讯回退时的申万二级名称
    "贵金属": {
        "recent": "贵金属连续领涨，资金持续流入",
        "logic": "美联储降息预期升温推升金价，地缘风险提供避险需求，黄金股业绩弹性大",
        "stocks": ["赤峰黄金", "中金黄金", "紫金矿业", "紫金黄金国际"],
    },
    "工业金属": {
        "recent": "工业金属轮番活跃，铜铝铅锌表现强势",
        "logic": "国内稳增长政策托底需求，供给端约束支撑价格，资源股估值修复",
        "stocks": ["紫金矿业", "云铝股份", "神火股份", "兴业银锡"],
    },
    # 申万一级: 石油石化 (覆盖油服工程等)
    "石油石化": {
        "recent": "石油石化持续走强，能源股受资金青睐",
        "logic": "国际油价维持高位，中东局势反复，油服景气周期向上",
        "stocks": ["中国海洋石油"],
        "aliases": ["油服工程"],
    },
    "油服工程": {
        "recent": "油服工程持续走强，能源股受资金青睐",
        "logic": "国际油价维持高位，中东局势反复，油服景气周期向上",
        "stocks": ["中国海洋石油"],
    },
    # 申万一级: 公用事业 (覆盖电力等)
    "公用事业": {
        "recent": "公用事业板块持续受资金青睐，表现稳健",
        "logic": "高股息防御属性+夏季用电高峰+电力改革政策",
        "stocks": [],
        "aliases": ["电力"],
    },
    "电力": {
        "recent": "电力板块持续受资金青睐，表现稳健",
        "logic": "高股息防御属性+夏季用电高峰+电力改革政策",
        "stocks": [],
    },
    # 申万一级: 电子 (覆盖半导体等)
    "电子": {
        "recent": "电子板块关注度下降，短期分化回调",
        "logic": "前期涨幅过大，短期资金获利了结；中长期国产替代和AI算力需求逻辑不变",
        "stocks": ["科创半导体ETF华夏"],
        "aliases": ["半导体"],
    },
    "半导体": {
        "recent": "半导体板块关注度下降，短期分化回调",
        "logic": "前期涨幅过大，短期资金获利了结；中长期国产替代和AI算力需求逻辑不变",
        "stocks": ["科创半导体ETF华夏"],
    },
}


def _generate_stock_note(s, tech=None):
    """根据自选股涨跌幅和技术面生成备注。"""
    pct = s.get("pct", 0)
    code = s.get("code", "")
    name = s.get("name", "")
    if tech is None:
        tech = s.get("_tech", {})

    # 涨停/接近涨停
    if pct >= 9.5:
        return "涨停" if s.get("market") != "HK" else "暴涨"
    if pct >= 7:
        return "强势领涨"
    if pct >= 5:
        base = STOCK_SECTOR.get(code, "").split("/")[0]
        return base if base else "大幅上涨"
    if pct >= 2:
        base = STOCK_SECTOR.get(code, "").split("/")[0]
        return base if base else "温和上涨"
    if pct >= -1:
        # 平盘附近，查技术面
        if tech.get("macd_signal", "").startswith("死叉"):
            return "弱势震荡"
        if tech.get("kdj_signal", "").startswith("超买"):
            return "高位整理"
        return "窄幅震荡"
    if pct >= -3:
        return "小幅回调"
    if pct >= -5:
        base = STOCK_SECTOR.get(code, "")
        if "/" in base:
            return base.split("/")[0]
        return "回调"
    return "大幅下跌"


def _mid_term_outlook(industries, fund_flows):
    """根据当日板块和资金流向生成中期展望。"""
    if not industries:
        return "（板块数据暂不可用，中期展望待更新）"

    sorted_inds = sorted(industries, key=lambda x: x["pct"], reverse=True)

    # 找出强势板块（涨幅前5且与自选股相关的）
    strong_names = [s["name"] for s in sorted_inds[:10]]
    weak_names = [s["name"] for s in sorted_inds[-5:]]

    # 资金流向强的板块
    flow_map = {}
    if fund_flows:
        for f in fund_flows:
            flow_map[f["name"]] = f["main_net"]

    # 筛选出与模板匹配的板块
    outlook_parts = []
    used_templates = set()
    idx = 1

    for ind in sorted_inds[:15]:
        name = ind["name"]
        for key, tmpl in MIDTERM_SECTOR_TEMPLATES.items():
            if key in name and key not in used_templates:
                used_templates.add(key)
                # 同时标记别名(避免一级/二级重复匹配)
                for alias in tmpl.get("aliases", []):
                    used_templates.add(alias)
                flow = flow_map.get(name)
                flow_str = f"，主力净流入{flow:+.1f}亿" if flow and flow > 0 else ""
                outlook_parts.append(
                    f"{idx}. **{name}板块**\n"
                    f"* 近期表现：{tmpl['recent']}{flow_str}\n"
                    f"* 看好逻辑：{tmpl['logic']}\n"
                    f"* 代表标的：{'、'.join(tmpl['stocks'])}"
                )
                idx += 1
                break
        if len(outlook_parts) >= 4:
            break

    # 补充通用强势板块
    if len(outlook_parts) < 4:
        for ind in sorted_inds[:15]:
            name = ind["name"]
            if any(name in p for p in outlook_parts):
                continue
            flow = flow_map.get(name)
            flow_str = f"，主力净流入{flow:+.1f}亿" if flow and flow > 0 else ""
            outlook_parts.append(
                f"{idx}. **{name}板块**\n"
                f"* 近期表现：今日涨{ind['pct']:+.2f}%{flow_str}\n"
                f"* 看好逻辑：板块资金关注度高，短期趋势向好\n"
                f"* 代表标的：关注板块龙头"
            )
            idx += 1
            if len(outlook_parts) >= 4:
                break

    # 需回避板块
    avoid_parts = []
    avoid_count = 0
    for ind in reversed(sorted_inds[-10:]):  # 跌幅最大排第一
        name = ind["name"]
        pct = ind["pct"]
        if pct >= -1:
            continue
        reason = _get_sector_reason(name, pct)
        flow = flow_map.get(name)
        flow_str = f"，主力净流出{abs(flow):.1f}亿" if flow and flow < 0 else ""
        avoid_parts.append(f"* **{name}**：{reason}{flow_str}")
        avoid_count += 1
        if avoid_count >= 3:
            break

    result = "\n\n".join(outlook_parts)
    if avoid_parts:
        result += "\n\n**需回避板块：**\n" + "\n".join(avoid_parts)

    return result if result else "（数据不足，中期展望待更新）"


def _tomorrow_watch(indices_kline, indices_tech, overview, news):
    """生成明日关注内容。"""
    parts = []

    # 关键点位（上证指数）
    sh_klines = indices_kline.get("000001", [])
    if len(sh_klines) >= 3:
        recent_high = max(k["high"] for k in sh_klines[-5:])
        recent_low = min(k["low"] for k in sh_klines[-5:])
        today_low = sh_klines[-1]["low"]
        today_high = sh_klines[-1]["high"]
        today_close = sh_klines[-1]["close"]
        parts.append(
            f"* **关键点位**：上证支撑 {today_low:.0f}/压力 {today_high:.0f}，"
            f"5日区间 {recent_low:.0f}-{recent_high:.0f}"
        )

    # 需关注事件
    events = []
    for n in (news or [])[:10]:
        title = n.get("title", "")
        for kw in ["美联储", "央行", "MLF", "LPR", "降息", "降准", "政策", "会议",
                     "数据", "GDP", "CPI", "PMI", "非农", "地缘", "中东",
                     "俄乌", "关税", "制裁", "减持", "解禁"]:
            if kw in title:
                events.append(title)
                break
    events = list(dict.fromkeys(events))[:3]  # 去重取前3
    if events:
        parts.append(f"* **需关注事件**：{'；'.join(events)}")

    # 操作建议
    up = overview.get("up", 0)
    down = overview.get("down", 0)
    total = up + down
    up_ratio = up / total if total > 0 else 0.5

    sh_tech = indices_tech.get("000001", {})
    macd_sig = sh_tech.get("macd_signal", "")
    kdj_sig = sh_tech.get("kdj_signal", "")

    advice_parts = []
    if up_ratio > 0.6:
        advice_parts.append("市场情绪偏暖，可适度参与强势板块低吸机会")
    elif up_ratio < 0.3:
        advice_parts.append("市场情绪偏弱，控制仓位，等待企稳信号")
    else:
        advice_parts.append("市场分化明显，精选个股，控制仓位")

    if "超买" in kdj_sig:
        advice_parts.append("上证KDJ超买，短线追高需谨慎")
    if "死叉" in macd_sig:
        advice_parts.append("上证MACD死叉，注意回调风险")
    if "金叉" in macd_sig:
        advice_parts.append("上证MACD金叉，中线趋势转好")

    parts.append(f"* **操作建议**：{'；'.join(advice_parts)}")

    return "\n".join(parts)


def _gen_midterm_with_ai_hint(industries, fund_flows):
    """如果板块数据不可用，给一个简要说明。"""
    outlook = _mid_term_outlook(industries, fund_flows)
    return outlook


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

def _calc_amount_change_pct(klines):
    """从K线数据计算成交额/成交量环比变化率(%)。
    比较最后两天: klines[-1] vs klines[-2]。
    返回: 变化百分比(正=放大, 负=缩小), 或None如果数据不足。
    """
    if not klines or len(klines) < 2:
        return None
    today_vol = klines[-1].get("volume", 0)
    yest_vol = klines[-2].get("volume", 0)
    if yest_vol > 0 and today_vol > 0:
        return round((today_vol / yest_vol - 1) * 100, 1)
    return None

def generate_tech_summary(klines, tech_data, name=""):
    """生成技术分析文字总结。
    包含: K线形态、布林带位置、成交量变化、均线状态、MACD/KDJ信号。
    """
    if not klines or len(klines) < 5 or not tech_data:
        return ""

    parts = []
    last = klines[-1]
    prev = klines[-2] if len(klines) >= 2 else {}

    # K线形态
    open_p = last.get("open", 0)
    close = last.get("close", 0)
    high = last.get("high", 0)
    low = last.get("low", 0)
    pct = last.get("pct", 0)

    body = close - open_p
    upper_shadow = high - max(open_p, close)
    lower_shadow = min(open_p, close) - low

    if abs(body) < (high - low) * 0.1:
        kline_desc = "十字星"
    elif body > 0:
        if lower_shadow > body * 2:
            kline_desc = "长下影阳线"
        elif upper_shadow > body * 2:
            kline_desc = "长上影阳线"
        elif close > prev.get("close", 0):
            kline_desc = "放量阳线" if pct > 2 else "小阳线"
        else:
            kline_desc = "高开阳线"
    else:
        if lower_shadow > abs(body) * 2:
            kline_desc = "长下影阴线"
        elif upper_shadow > abs(body) * 2:
            kline_desc = "长上影阴线"
        elif pct < -2:
            kline_desc = "大阴线"
        else:
            kline_desc = "小阴线"

    parts.append(f"K线形态为{kline_desc}，涨跌幅{pct:+.2f}%")

    # 布林带
    boll_sig = tech_data.get("boll_signal", "")
    boll_upper = tech_data.get("boll_upper")
    boll_lower = tech_data.get("boll_lower")
    boll_mid = tech_data.get("boll_mid")
    if boll_sig:
        parts.append(f"布林带方面，价格处于「{boll_sig}」")
        if boll_upper and boll_lower and boll_mid and close > 0:
            band_width = (boll_upper - boll_lower) / boll_mid * 100 if boll_mid > 0 else 0
            parts.append(f"带宽{band_width:.1f}%")
    elif boll_upper and boll_lower and close > 0:
        pos = (close - boll_lower) / (boll_upper - boll_lower) * 100 if (boll_upper - boll_lower) > 0 else 50
        parts.append(f"布林带中价格位置约{pos:.0f}%")

    # 成交量
    vol_ratio = tech_data.get("vol_ratio", "")
    if vol_ratio:
        parts.append(f"成交量{vol_ratio}")
    # 环比变化
    amt_chg = _calc_amount_change_pct(klines)
    if amt_chg is not None:
        direction = "放大" if amt_chg > 0 else "缩小"
        parts.append(f"较前一交易日{direction}{abs(amt_chg):.1f}%")

    # 均线
    ma_status = tech_data.get("ma_status", "")
    if ma_status:
        parts.append(f"均线{ma_status}")

    # MACD
    macd_sig = tech_data.get("macd_signal", "")
    if macd_sig:
        parts.append(f"MACD{macd_sig}")

    # KDJ
    kdj_sig = tech_data.get("kdj_signal", "")
    if kdj_sig:
        parts.append(f"KDJ{kdj_sig}")

    # RSI
    rsi_sig = tech_data.get("rsi_signal", "")
    rsi6 = tech_data.get("rsi6")
    if rsi_sig and rsi6 is not None:
        parts.append(f"RSI6={rsi6:.1f}({rsi_sig})")

    # 近期涨跌
    chg5 = tech_data.get("chg_5d")
    chg20 = tech_data.get("chg_20d")
    if chg5 is not None:
        parts.append(f"5日{chg5:+.1f}%")
    if chg20 is not None:
        parts.append(f"20日{chg20:+.1f}%")

    summary = "，".join(parts) + "。"
    return summary

def fetch_all_data():
    print("=" * 50)
    print("开始数据采集 v7.2 (纯腾讯+Sina架构)...")
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
            # 计算成交额环比变化
            tech["amount_change"] = _calc_amount_change_pct(klines)
            indices_tech[idx["code"]] = tech
            indices_kline[idx["code"]] = klines
            print(f"  {idx['name']}: MA5={tech.get('ma5')} RSI6={tech.get('rsi6')} | {tech.get('ma_status')} | {tech.get('macd_signal')}")
        else:
            print(f"  {idx['name']}: K线数据不足 ({len(klines)}条)")

    # 为指数列表添加成交额变化
    for idx in indices:
        tech = indices_tech.get(idx["code"], {})
        idx["amount_change"] = tech.get("amount_change")

    # === 计算两市成交额环比变化 (用上证+深证K线成交量) ===
    sh_klines = indices_kline.get("000001", [])
    sz_klines = indices_kline.get("399001", [])
    if len(sh_klines) >= 2 and len(sz_klines) >= 2:
        today_vol = sh_klines[-1].get("volume", 0) + sz_klines[-1].get("volume", 0)
        yest_vol = sh_klines[-2].get("volume", 0) + sz_klines[-2].get("volume", 0)
        if yest_vol > 0 and today_vol > 0:
            ratio = today_vol / yest_vol
            total_amt = overview.get("total_amount", 0)
            yest_total = total_amt / ratio if ratio > 0 else 0
            abs_change = total_amt - yest_total
            overview["total_amount_change_pct"] = round((ratio - 1) * 100, 1)
            overview["total_amount_change_abs"] = round(abs_change, 2)
            print(f"  [两市成交额] 今日:{total_amt:.0f}亿 昨日估算:{yest_total:.0f}亿 变化:{(ratio-1)*100:+.1f}%")

    # === 技术分析: 自选股 (含港股，用腾讯K线) ===
    print("\n--- 技术分析: 自选股 (含港股) ---")
    watchlist_tech = {}
    watchlist_kline = {}
    for s in WATCHLIST:
        klines = get_kline(s["sina"], days=60, secid=s["secid"], sina_code=s["sina"])
        if len(klines) >= 30:
            tech = analyze_technicals(klines)
            # 计算成交额环比变化
            tech["amount_change"] = _calc_amount_change_pct(klines)
            watchlist_tech[s["code"]] = tech
            watchlist_kline[s["code"]] = klines
            # 港股: 从K线最后一天提取换手率
            if s["market"] == "HK" and klines:
                hk_turnover = klines[-1].get("turnover", 0)
                if hk_turnover > 0:
                    # 更新watchlist中对应股票的换手率
                    for w in watchlist:
                        if w["code"] == s["code"]:
                            w["turnover"] = hk_turnover
                            break
                    print(f"  {s['name']}: 换手率={hk_turnover:.2f}% | {tech.get('ma_status','')} | {tech.get('macd_signal','')}")
                else:
                    print(f"  {s['name']}: {tech.get('ma_status','')} | {tech.get('macd_signal','')} | {tech.get('boll_signal','')}")
            else:
                print(f"  {s['name']}: {tech.get('ma_status','')} | {tech.get('macd_signal','')} | {tech.get('boll_signal','')}")
        else:
            print(f"  {s['name']}: K线数据不足 ({len(klines)}条)")

    # 为自选股列表添加成交额变化
    for w in watchlist:
        tech = watchlist_tech.get(w["code"], {})
        w["amount_change"] = tech.get("amount_change")

    print(f"\n数据采集完成:")
    print(f"  市场总览: {'OK' if overview else 'EMPTY'}")
    print(f"  指数: {len(indices)} 个 | 技术分析: {len(indices_tech)} 个")
    print(f"  行业板块: {len(industries)} 个")
    print(f"  概念板块: {len(concepts)} 个")
    print(f"  资金流向: {len(fund_flows)} 个")
    print(f"  自选股: {len(watchlist)} 只 | 技术分析: {len(watchlist_tech)} 只")
    print(f"  新闻: {len(news)} 条")

    # === 生成技术分析文字总结 ===
    indices_summary = {}
    for idx in INDICES:
        klines = indices_kline.get(idx["code"], [])
        tech = indices_tech.get(idx["code"], {})
        if klines and tech:
            indices_summary[idx["code"]] = generate_tech_summary(klines, tech, idx["name"])

    watchlist_summary = {}
    for s in WATCHLIST:
        klines = watchlist_kline.get(s["code"], [])
        tech = watchlist_tech.get(s["code"], {})
        if klines and tech:
            watchlist_summary[s["code"]] = generate_tech_summary(klines, tech, s["name"])

    # === PE估值 ===
    print("\n--- 市场估值 ---")
    market_pe = get_market_pe()

    # 将tech数据附加到watchlist（供备注生成用）
    for w in watchlist:
        tech = watchlist_tech.get(w["code"], {})
        w["_tech"] = tech

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
        "indices_summary": indices_summary,
        "watchlist_summary": watchlist_summary,
        "market_pe": market_pe,
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

def _gen_stock_analysis_text(stock, tech, klines):
    """Generate per-stock technical analysis text and trend prediction.
    Returns: (analysis_text, prediction_text, bullish_count, bearish_count)
    """
    pct = stock.get("pct", 0)
    price = stock.get("price", 0)

    macd_sig = tech.get("macd_signal", "")
    kdj_sig = tech.get("kdj_signal", "")
    rsi6 = tech.get("rsi6")
    rsi_sig = tech.get("rsi_signal", "")
    boll_sig = tech.get("boll_signal", "")
    boll_upper = tech.get("boll_upper")
    boll_mid = tech.get("boll_mid")
    boll_lower = tech.get("boll_lower")
    ma5 = tech.get("ma5")
    ma10 = tech.get("ma10")
    ma20 = tech.get("ma20")
    ma_status = tech.get("ma_status", "")
    vol_ratio = tech.get("vol_ratio", "")
    kdj_j = tech.get("kdj_j")
    kdj_k = tech.get("kdj_k")
    chg5 = tech.get("chg_5d")
    chg20 = tech.get("chg_20d")

    analysis_parts = []

    # MACD
    if "金叉" in macd_sig:
        analysis_parts.append(f"MACD{macd_sig}，多头动能增强")
    elif "死叉" in macd_sig:
        analysis_parts.append(f"MACD{macd_sig}，空头动能增强")
    elif "多头" in macd_sig:
        analysis_parts.append("MACD多头延续")
    elif "空头" in macd_sig:
        analysis_parts.append("MACD空头延续")

    # KDJ
    if kdj_j is not None:
        if kdj_j > 100:
            analysis_parts.append(f"KDJ-J={kdj_j:.0f}超买，短线回调压力大")
        elif kdj_j < 0:
            analysis_parts.append(f"KDJ-J={kdj_j:.0f}超卖，可能超跌反弹")
        elif "金叉" in kdj_sig:
            analysis_parts.append(f"KDJ金叉(K={kdj_k:.1f}/J={kdj_j:.1f})")
        elif "死叉" in kdj_sig:
            analysis_parts.append(f"KDJ死叉(K={kdj_k:.1f}/J={kdj_j:.1f})")

    # RSI
    if rsi6 is not None:
        if rsi6 > 70:
            analysis_parts.append(f"RSI6={rsi6:.1f}偏高")
        elif rsi6 < 30:
            analysis_parts.append(f"RSI6={rsi6:.1f}超卖")
        elif rsi6 > 50:
            analysis_parts.append(f"RSI6={rsi6:.1f}偏强")
        else:
            analysis_parts.append(f"RSI6={rsi6:.1f}偏弱")

    # BOLL
    if boll_sig:
        analysis_parts.append(f"布林带{boll_sig}")

    # MA
    if ma_status:
        analysis_parts.append(f"均线{ma_status}")

    # Volume
    if vol_ratio:
        analysis_parts.append(f"量能{vol_ratio}")

    # Recent trend
    if chg5 is not None:
        analysis_parts.append(f"5日{chg5:+.1f}%")
    if chg20 is not None:
        analysis_parts.append(f"20日{chg20:+.1f}%")

    analysis_text = "，".join(analysis_parts) + "。" if analysis_parts else "技术指标数据不足。"

    # Count bullish/bearish signals
    bullish = 0
    bearish = 0
    if "金叉" in macd_sig or "多头" in macd_sig:
        bullish += 1
    if "死叉" in macd_sig or "空头" in macd_sig:
        bearish += 1
    if kdj_j is not None:
        if kdj_j > 100:
            bearish += 1
        elif kdj_j < 0:
            bullish += 1
        elif "金叉" in kdj_sig:
            bullish += 1
        elif "死叉" in kdj_sig:
            bearish += 1
    if rsi6 is not None:
        if rsi6 > 70:
            bearish += 1
        elif rsi6 < 30:
            bullish += 1
        elif rsi6 > 50:
            bullish += 1
        else:
            bearish += 1
    if "多头" in ma_status or "站上" in ma_status:
        bullish += 1
    if "空头" in ma_status or "跌破" in ma_status:
        bearish += 1
    if chg20 is not None:
        if chg20 > 5:
            bullish += 1
        elif chg20 < -5:
            bearish += 1

    # Support/Resistance
    resistance = None
    support = None
    if boll_upper and price > 0:
        resistance = boll_upper
    if boll_lower and price > 0:
        support = boll_lower
    # Use recent high/low
    if klines and len(klines) >= 5:
        recent_high = max(k["high"] for k in klines[-5:])
        recent_low = min(k["low"] for k in klines[-5:])
        if resistance is None or recent_high > resistance:
            resistance = recent_high
        if support is None or recent_low < support:
            support = recent_low
    # MA as fallback
    if ma20 and price > 0:
        if ma20 > price and (resistance is None or ma20 < resistance):
            resistance = ma20 if resistance is None else min(resistance, ma20)
        if ma20 < price and (support is None or ma20 > support):
            support = ma20 if support is None else max(support, ma20)

    # Trend determination
    if bullish > bearish + 1:
        short_term = "偏多"
        mid_term = "看多"
    elif bearish > bullish + 1:
        short_term = "偏空"
        mid_term = "看空"
    else:
        short_term = "震荡"
        mid_term = "中性"

    pred_parts = []
    pred_parts.append(f"短期{short_term}")
    pred_parts.append(f"中期{mid_term}")
    if resistance is not None and price > 0:
        pred_parts.append(f"阻力位{resistance:.2f}")
    if support is not None and price > 0:
        pred_parts.append(f"支撑位{support:.2f}")
    prediction = "，".join(pred_parts) + "。"

    return analysis_text, prediction, bullish, bearish


def build_stock_analysis_section(watchlist, watchlist_tech, watchlist_kline):
    """Build the per-stock technical analysis and trend prediction section."""
    if not watchlist or not watchlist_tech:
        return ""

    html = '''<div class="section">
  <div class="section-title">九、自选股技术分析与走势判断</div>
  <p style="font-size:13px;color:#666;margin-bottom:14px">基于MACD/KDJ/RSI/BOLL/均线/量能等多维技术指标的综合分析，给出短期及中期走势判断。</p>
'''

    # Summary table
    html += '''  <table style="margin-bottom:20px">
    <thead><tr>
      <th>股票</th><th class="num">涨跌幅</th><th class="num">MACD</th>
      <th class="num">KDJ-J</th><th class="num">RSI6</th><th class="num">布林位置</th>
      <th class="num">综合信号</th>
    </tr></thead>
    <tbody>
'''
    for s in watchlist:
        code = s.get("code", "")
        t = watchlist_tech.get(code, {})
        if not t:
            continue
        pct = s.get("pct", 0)
        pct_cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")
        macd = t.get("macd_signal", "-")
        macd_cls = "up" if "金叉" in macd or "多头" in macd else ("down" if "死叉" in macd or "空头" in macd else "")
        kdj_j = t.get("kdj_j")
        kdj_str = f"{kdj_j:.1f}" if kdj_j is not None else "-"
        rsi6 = t.get("rsi6")
        rsi_str = f"{rsi6:.1f}" if rsi6 is not None else "-"
        boll = t.get("boll_signal", "-")

        _, _, bull, bear = _gen_stock_analysis_text(s, t, watchlist_kline.get(code, []))
        if bull > bear + 1:
            sig = '<span class="up">偏多</span>'
        elif bear > bull + 1:
            sig = '<span class="down">偏空</span>'
        else:
            sig = '<span class="flat">震荡</span>'

        html += f'      <tr><td>{s["name"]}</td><td class="num {pct_cls}">{pct:+.2f}%</td><td class="num {macd_cls}">{macd}</td><td class="num">{kdj_str}</td><td class="num">{rsi_str}</td><td class="num" style="font-size:11px">{boll}</td><td class="num">{sig}</td></tr>\n'

    html += '    </tbody></table>\n'

    # Per-stock analysis cards
    html += '  <div class="stock-analysis-grid">\n'

    for s in watchlist:
        code = s.get("code", "")
        t = watchlist_tech.get(code, {})
        if not t:
            continue

        klines = watchlist_kline.get(code, [])
        analysis, prediction, bull, bear = _gen_stock_analysis_text(s, t, klines)

        macd = t.get("macd_signal", "")
        kdj_sig = t.get("kdj_signal", "")
        rsi_sig = t.get("rsi_signal", "")
        boll_sig = t.get("boll_signal", "")

        tags = []
        if macd:
            cls = "tag-up" if "金叉" in macd or "多头" in macd else ("tag-down" if "死叉" in macd or "空头" in macd else "tag-neutral")
            tags.append(f'<span class="signal-tag {cls}">MACD {macd}</span>')
        if kdj_sig:
            cls = "tag-up" if "金叉" in kdj_sig else ("tag-down" if "死叉" in kdj_sig else ("tag-warn" if "超买" in kdj_sig else "tag-info"))
            tags.append(f'<span class="signal-tag {cls}">KDJ {kdj_sig}</span>')
        if rsi_sig:
            cls = "tag-warn" if "超买" in rsi_sig else ("tag-info" if "超卖" in rsi_sig else ("tag-up" if "偏强" in rsi_sig else "tag-down"))
            tags.append(f'<span class="signal-tag {cls}">RSI {rsi_sig}</span>')
        if boll_sig:
            tags.append(f'<span class="signal-tag tag-neutral">BOLL {boll_sig}</span>')

        tags_html = "".join(tags)

        if bull > bear + 1:
            trend_badge = '<span class="trend-badge trend-up">偏多</span>'
        elif bear > bull + 1:
            trend_badge = '<span class="trend-badge trend-down">偏空</span>'
        else:
            trend_badge = '<span class="trend-badge trend-neutral">震荡</span>'

        market_tag = " [港股]" if s.get("market") == "HK" else ""
        html += f'''    <div class="analysis-card">
      <div class="analysis-card-header">
        <span class="analysis-card-title">{s["name"]}{market_tag} <span style="color:#999;font-size:11px">({code})</span></span>
        {trend_badge}
      </div>
      <div class="analysis-tags">{tags_html}</div>
      <div class="analysis-text">{analysis}</div>
      <div class="analysis-prediction"><strong>走势判断：</strong>{prediction}</div>
    </div>
'''

    html += '  </div>\n</div>\n'

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
    indices_tech = all_data.get("indices_tech", {})
    watchlist_tech = all_data.get("watchlist_tech", {})
    indices_kline = all_data.get("indices_kline", {})
    watchlist_kline = all_data.get("watchlist_kline", {})
    indices_summary = all_data.get("indices_summary", {})
    watchlist_summary = all_data.get("watchlist_summary", {})
    news = all_data.get("news", [])

    up_count = overview.get("up", 0)
    down_count = overview.get("down", 0)
    total_stocks = overview.get("total_stocks", 0)
    limit_up = overview.get("limit_up", 0)
    limit_down = overview.get("limit_down", 0)
    total_amount = overview.get("total_amount", 0)
    total_amount_change_pct = overview.get("total_amount_change_pct")
    total_amount_change_abs = overview.get("total_amount_change_abs", 0)
    if total_amount_change_pct is not None:
        amt_chg_cls = "up" if total_amount_change_pct > 0 else "down"
        if total_amount_change_abs > 0:
            amt_chg_str = f"较前日+{total_amount_change_abs:.0f}亿({total_amount_change_pct:+.1f}%)"
        else:
            amt_chg_str = f"较前日{total_amount_change_abs:.0f}亿({total_amount_change_pct:+.1f}%)"
        total_amt_change_html = f' <span class="{amt_chg_cls}">{amt_chg_str}</span>'
    else:
        total_amt_change_html = ""

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
.hero .summary-item .label .up,.hero .summary-item .label .down{{font-size:11px;font-weight:600}}
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
.tech-summary{{background:#f8f9fa;border-left:3px solid #c0392b;padding:12px 16px;margin:12px 0;border-radius:6px;font-size:13px;line-height:1.8;color:#444}}
.stock-chart-block{{margin-bottom:24px}}
.stock-chart-title{{font-size:15px;font-weight:700;color:#1a1a2e;margin-bottom:8px;padding-left:8px;border-left:3px solid #c0392b}}
.stock-analysis-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}}
.analysis-card{{background:#f8f9fa;border-radius:10px;padding:16px;border-left:3px solid #c0392b}}
.analysis-card-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}}
.analysis-card-title{{font-size:15px;font-weight:700;color:#1a1a2e}}
.analysis-tags{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}}
.signal-tag{{display:inline-block;font-size:11px;padding:2px 8px;border-radius:4px;font-weight:600}}
.tag-up{{background:#fdecea;color:#c0392b}}
.tag-down{{background:#e8f5e9;color:#27ae60}}
.tag-warn{{background:#fff3e0;color:#e65100}}
.tag-info{{background:#e3f2fd;color:#1565c0}}
.tag-neutral{{background:#f5f5f5;color:#666}}
.trend-badge{{font-size:12px;padding:3px 10px;border-radius:12px;font-weight:700}}
.trend-up{{background:#fdecea;color:#c0392b}}
.trend-down{{background:#e8f5e9;color:#27ae60}}
.trend-neutral{{background:#f5f5f5;color:#888}}
.analysis-text{{font-size:13px;line-height:1.8;color:#444;margin-bottom:8px}}
.analysis-prediction{{font-size:13px;line-height:1.8;color:#333;background:#fff;border-radius:6px;padding:8px 12px;border-left:2px solid #5c6bc0}}
.source{{font-size:11px;color:#aaa;text-align:right;margin-top:40px;padding:10px 0}}
.news-list{{display:flex;flex-direction:column;gap:8px}}
.news-item{{font-size:13px;line-height:1.6;padding:8px 12px;background:#f8f9fa;border-radius:6px;border-left:3px solid #3498db}}
.news-time{{display:inline-block;color:#999;font-size:11px;margin-right:8px;min-width:60px}}
</style>
</head>
<body>
<div class="container">
<div class="hero">
  <h1>A股收盘复盘报告</h1>
  <div class="date">{display_date} | 数据来源：腾讯财经 & Sina财经 & 同花顺 | 含技术指标分析</div>
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
      <div class="label">两市成交额{total_amt_change_html}</div>
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
    <thead><tr><th>指数</th><th class="num">收盘价</th><th class="num">涨跌幅</th><th class="num">涨跌额</th><th class="num">成交额(亿)</th><th class="num">成交额变化</th></tr></thead>
    <tbody>
'''
    for idx in indices:
        pct = idx.get("pct", 0)
        cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")
        amt = idx.get("amount", 0)
        chg = idx.get("change", 0)
        amt_chg = idx.get("amount_change")
        if amt_chg is not None:
            amt_chg_cls = "up" if amt_chg > 0 else "down"
            amt_chg_str = f'<td class="num {amt_chg_cls}">{amt_chg:+.1f}%</td>'
        else:
            amt_chg_str = '<td class="num">-</td>'
        html += f'      <tr><td>{idx["name"]}</td><td class="num">{idx.get("price",0):.2f}</td><td class="num {cls}">{pct:+.2f}%</td><td class="num {cls}">{chg:+.2f}</td><td class="num">{amt:.2f}</td>{amt_chg_str}</tr>\n'

    html += '''    </tbody></table></div>

<div class="chart-row">
'''
    # 行业板块图表 — 确保按涨跌幅排序并取合适的数量
    industries_sorted = sorted(industries, key=lambda x: x["pct"], reverse=True) if industries else []
    industry_names = [b["name"] for b in industries_sorted[:10]]
    industry_pcts = [b["pct"] for b in industries_sorted[:10]]

    if industries:
        top_n = min(len(industries_sorted), 10)
        ind_label = f"行业板块 Top {top_n}"
        ind_level = "申万一级" if len(industries) > 10 else "申万二级"
        ind_source = "东方财富" if len(industries) > 10 else "腾讯自选股"
        html += f'''  <div class="section" style="margin-bottom:0">
    <div class="section-title">二、{ind_label}</div>
    <div class="chart-box" id="chart-industry" style="height:350px"></div>
    <div style="margin-top:12px;font-size:12px;color:#999">* {ind_level}行业，按涨跌幅排序 | 数据来源：{ind_source}</div>
  </div>
'''
    else:
        html += '''  <div class="section" style="margin-bottom:0">
    <div class="section-title">二、行业板块 Top 10</div>
    <p style="color:#999;padding:40px 0;text-align:center">板块数据暂不可用 (东方财富API在云端受限)</p>
  </div>
'''
    if fund_flows:
        # 资金流入前五: 只取真正净流入(正数)
        inflow_list = [f for f in fund_flows if f["main_net"] > 0]
        inflow_top5 = sorted(inflow_list, key=lambda x: x["main_net"], reverse=True)[:5]
        # 流出前五：只取真正净流出(负数)的行业，按净流出金额从大到小排序
        outflow_list = [f for f in fund_flows if f["main_net"] < 0]
        outflow_top5 = sorted(outflow_list, key=lambda x: x["main_net"])[:5]

        # 动态标签: 根据实际数据量显示
        in_label = f"资金流入{'前三' if len(inflow_top5) <= 3 else '前五'}"
        out_label = f"资金流出{'前三' if len(outflow_top5) <= 3 else '前五'}"
        data_note = "数据来源：东方财富" if len(fund_flows) > 10 else f"数据来源：腾讯自选股 (仅{len(inflow_top5)}流入/{len(outflow_top5)}流出)"

        html += f'''  <div class="section" style="margin-bottom:0">
    <div class="section-title">三、行业资金流向</div>
    <div class="board-grid">
      <div class="board-col">
        <h4 style="color:#c0392b">{in_label}</h4>
'''
        for f in inflow_top5:
            net = f["main_net"]
            mp = f.get("main_pct", 0)
            pct_note = f' <span style="color:#999;font-size:11px">(占比{mp:+.2f}%)</span>' if mp != 0 else ""
            html += f'        <div class="board-item"><span class="name">{f["name"]}{pct_note}</span><span class="pct up">{net:+.2f}亿</span></div>\n'
        html += f'''      </div>
      <div class="board-col">
        <h4 style="color:#27ae60">{out_label}</h4>
'''
        if outflow_top5:
            for f in outflow_top5:
                net = f["main_net"]
                mp = f.get("main_pct", 0)
                pct_note = f' <span style="color:#999;font-size:11px">(占比{mp:+.2f}%)</span>' if mp != 0 else ""
                html += f'        <div class="board-item"><span class="name">{f["name"]}{pct_note}</span><span class="pct down">{net:+.2f}亿</span></div>\n'
        else:
            html += '        <div class="board-item" style="color:#999;padding:20px 0;text-align:center">今日全行业资金净流入，无净流出行业</div>\n'
        html += f'''      </div>
    </div>
    <div style="margin-top:12px;font-size:12px;color:#999">* 主力净流入/流出（亿元），{data_note}</div>
  </div>
</div>
'''
    else:
        # 资金流向数据不可用，关闭 chart-row，独立显示为全宽 section
        html += '''</div>

<div class="section">
  <div class="section-title">三、行业资金流向</div>
  <p style="color:#999;padding:40px 0;text-align:center">东方财富API在GitHub Actions环境受限，暂无实时资金流向数据。<br>建议通过本地环境运行获取完整数据。</p>
</div>
'''

    html += '''
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
      <h4>表现最弱</h4>
'''
        # 取末尾概念(涨跌幅最小): 取倒数10个，但避免与涨幅Top10重复
        weakest_candidates = concepts[-10:]  # 已按pct降序排列，末尾是最弱
        # 如果概念总数 > 10，则取末尾最多10个最弱的
        # 如果概念总数 <= 10，取后一半（避免全部重复），但至少取3个
        if len(concepts) > 10:
            weakest = sorted(weakest_candidates, key=lambda x: x["pct"])
        else:
            mid = max(3, len(concepts) // 2)
            weakest = sorted(concepts[-mid:], key=lambda x: x["pct"])

        # 检查是否所有概念板块全线飘红(全为正涨幅)
        all_positive = all(b["pct"] > 0 for b in concepts)
        has_negative = any(b["pct"] < 0 for b in concepts)

        if weakest:
            if all_positive and not has_negative:
                # 全线上涨：显示涨幅最小的几个，附带说明
                html += '      <div class="board-item" style="color:#27ae60;font-size:12px;padding:6px 0">今日概念板块全线上涨</div>\n'
                for b in weakest:
                    cls = "up" if b["pct"] > 0 else "down"
                    html += f'      <div class="board-item"><span class="name">{b["name"]}</span><span class="pct {cls}">{b["pct"]:+.2f}%</span></div>\n'
            else:
                # 有涨有跌：正常显示最弱板块
                for b in weakest:
                    cls = "up" if b["pct"] > 0 else "down"
                    html += f'      <div class="board-item"><span class="name">{b["name"]}</span><span class="pct {cls}">{b["pct"]:+.2f}%</span></div>\n'
        else:
            html += '      <div class="board-item" style="color:#999;padding:20px 0;text-align:center">今日概念板块全线上涨</div>\n'

        html += '''    </div></div></div>
'''
    else:
        html += '''  <p style="color:#999;padding:40px 0;text-align:center">概念板块数据暂不可用 (东方财富API在云端受限)</p>
</div>
'''

    html += '''<div class="section">
  <div class="section-title">五、自选股表现</div>
  <table>
    <thead><tr><th>股票</th><th class="num">收盘价</th><th class="num">涨跌幅</th><th class="num">换手率</th><th class="num">量比</th><th class="num">成交额(亿)</th><th class="num">成交额变化</th></tr></thead>
    <tbody>
'''
    for s in watchlist:
        pct = s.get("pct", 0)
        cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")
        amt = s.get("amount", 0)
        amt_chg = s.get("amount_change")
        if amt_chg is not None:
            amt_chg_cls = "up" if amt_chg > 0 else "down"
            amt_chg_str = f'<td class="num {amt_chg_cls}">{amt_chg:+.1f}%</td>'
        else:
            amt_chg_str = '<td class="num">-</td>'
        html += f'''      <tr><td>{s["name"]} <span style="color:#999;font-size:11px">({s["code"]})</span></td>
        <td class="num">{s.get("price",0):.2f}</td>
        <td class="num {cls}">{pct:+.2f}%</td>
        <td class="num">{s.get("turnover",0):.2f}%</td>
        <td class="num">{s.get("volume_ratio",0):.2f}</td>
        <td class="num">{amt:.2f}</td>{amt_chg_str}</tr>
'''

    html += '''    </tbody></table></div>
'''

    # === 六、上证指数K线（大盘参考）===
    html += '''<div class="section">
  <div class="section-title">六、上证指数 K线 + 布林带 + 成交量（大盘参考）</div>
'''
    sh_kline = indices_kline.get("000001", [])
    sh_tech = indices_tech.get("000001", {})
    if sh_kline:
        html += build_kline_chart_placeholder("chart-sh-kline", "上证指数", sh_kline)
        # 技术分析文字总结
        sh_summary = indices_summary.get("000001", "")
        if sh_summary:
            html += f'<div class="tech-summary"><strong>技术分析：</strong>{sh_summary}</div>\n'
    else:
        html += '<p style="color:#999">K线数据获取失败</p>'
    html += '</div>\n'

    # 指数技术指标表格
    html += build_tech_table_section("七、指数技术指标", indices, indices_tech)

    # === 八、自选股 K线图（含港股，技术分析）===
    html += '<div class="section"><div class="section-title">八、自选股 K线图（技术分析）</div>\n'
    html += '<p style="color:#999;font-size:12px;margin-bottom:16px">每只自选股60日K线，含BOLL上/中/下轨 + MA5/MA20均线 + 成交量，下方附技术分析总结</p>\n'

    # 构建自选股列表（含港股）
    stock_kline_items = []
    for s in watchlist:
        klines = watchlist_kline.get(s["code"], [])
        t = watchlist_tech.get(s["code"], {})
        summary = watchlist_summary.get(s["code"], "")
        if klines and len(klines) >= 20:
            sig_parts = []
            if t.get("macd_signal"): sig_parts.append(t["macd_signal"])
            if t.get("kdj_signal"): sig_parts.append(t["kdj_signal"])
            sig_str = " ".join(sig_parts)
            stock_kline_items.append((s, klines, t, summary, sig_str))

    if stock_kline_items:
        # 两列网格排列
        html += '<div class="chart-row">\n'
        for s, klines, t, summary, sig_str in stock_kline_items:
            cid = _next_chart_id()
            market_tag = " [港股]" if s["market"] == "HK" else ""
            title = f'{s["name"]}{market_tag} ({s["code"]})'
            if sig_str:
                title += f' — {sig_str}'
            html += f'<div class="stock-chart-block">\n'
            html += f'<div class="stock-chart-title">{title}</div>\n'
            html += build_kline_chart_placeholder(cid, s["name"], klines)
            if summary:
                html += f'<div class="tech-summary" style="font-size:12px">{summary}</div>\n'
            html += '</div>\n'
        html += '</div>\n'
    else:
        html += '<p style="color:#999">自选股K线数据获取失败</p>\n'
    html += '</div>\n'

    # === 九、自选股技术分析与走势判断 ===
    html += build_stock_analysis_section(watchlist, watchlist_tech, watchlist_kline)

    # === 十、今日要闻 ===
    html += '''<div class="section">
  <div class="section-title">十、今日要闻</div>
'''
    if news:
        html += '<div class="news-list">\n'
        for n in news[:8]:
            src = n.get("source", "")
            src_str = f' <span style="color:#999;font-size:11px">({src})</span>' if src else ""
            html += f'  <div class="news-item"><span class="news-time">{n.get("time","")}</span> {n["title"]}{src_str}</div>\n'
        html += '</div>\n'
    else:
        html += '<p style="color:#999">今日暂无要闻数据</p>\n'
    html += '</div>\n'

    # === 十一、中期板块关注 ===
    html += '''<div class="section">
  <div class="section-title">十一、中期板块关注</div>
  <p style="font-size:13px;color:#666;margin-bottom:14px">基于近期走势、资金动向、政策面和基本面，分析中期（1-3个月）值得关注的板块。</p>
'''
    outlook_html = _mid_term_outlook(industries, fund_flows)
    # Convert markdown to HTML
    outlook_html = outlook_html.replace("**", "").replace("\n\n", "</p><p>").replace("\n", "<br>")
    html += f'  <div style="line-height:1.8;font-size:13px"><p>{outlook_html}</p></div>\n'
    html += '</div>\n'

    # === 十二、明日关注 ===
    html += '''<div class="section">
  <div class="section-title">十二、明日关注</div>
'''
    tw = _tomorrow_watch(indices_kline, indices_tech, overview, news)
    tw_html = tw.replace("**", "").replace("* ", "").replace("\n", "<br>")
    html += f'  <div style="line-height:1.8;font-size:13px">{tw_html}</div>\n'
    html += '</div>\n'

    html += f'''<div class="source">
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
    """构建钉钉推送摘要 — 六段式完整复盘 v8。"""
    overview = all_data.get("overview", {})
    indices = all_data.get("indices", [])
    watchlist = all_data.get("watchlist", [])
    industries = all_data.get("industries", [])
    concepts = all_data.get("concepts", [])
    fund_flows = all_data.get("fund_flows", [])
    news = all_data.get("news", [])
    indices_tech = all_data.get("indices_tech", {})
    indices_kline = all_data.get("indices_kline", {})
    indices_summary = all_data.get("indices_summary", {})
    watchlist_tech = all_data.get("watchlist_tech", {})
    watchlist_summary = all_data.get("watchlist_summary", {})
    market_pe = all_data.get("market_pe")

    up = overview.get("up", 0)
    down = overview.get("down", 0)
    total_amount = overview.get("total_amount", 0)
    total_amt_chg_pct = overview.get("total_amount_change_pct")
    total_amt_chg_abs = overview.get("total_amount_change_abs", 0)
    if total_amt_chg_pct is not None:
        direction = "放量" if total_amt_chg_abs > 0 else "缩量"
        amt_chg_md = f"（较昨日{direction}约{abs(total_amt_chg_pct):.0f}%）"
    else:
        amt_chg_md = ""

    beijing_tz = timezone(timedelta(hours=8))
    now = datetime.now(beijing_tz)
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    display_date = f"{now.strftime('%Y-%m-%d')}（周{weekdays[now.weekday()]}）"

    md = f"### A股每日收盘复盘\n**日期： {display_date}**\n\n---\n\n"

    # ================================================================
    # 一、大盘概览 — 指数表格 + 成交额 + PE
    # ================================================================
    md += "**一、大盘概览**\n\n"
    md += "| 指数 | 收盘 | 涨跌幅 | 成交额 |\n"
    md += "|------|------|--------|--------|\n"
    for idx in indices[:5]:  # 前5个主要指数
        pct = idx.get("pct", 0)
        price = idx.get("price", 0)
        amt = idx.get("amount", 0)
        if idx["code"] in ("399006", "000688"):
            amt_str = f"{amt:.0f}亿" if amt > 0 else "-"
        else:
            amt_str = f"{amt:.0f}亿" if amt > 0 else "-"
        md += f"| {idx['name']} | {price:.2f} | {pct:+.2f}% | {amt_str} |\n"

    md += f"* 全市场：约**{up}**家上涨 / 超**{down}**家下跌\n"
    md += f"* 两市成交：约**{total_amount:.0f}**亿{amt_chg_md}\n"
    if market_pe:
        md += f"* 市场估值：上证PE **{market_pe:.2f}**\n"

    # 上证技术分析
    sh_summary = indices_summary.get("000001", "")
    if sh_summary:
        md += f"\n> 上证技术：{sh_summary}\n"

    md += "\n---\n\n"

    # ================================================================
    # 二、板块表现 — 涨幅/跌幅前5 + 逻辑 + 热门概念
    # ================================================================
    md += "**二、板块表现**\n\n"

    if industries:
        sorted_inds = sorted(industries, key=lambda x: x["pct"], reverse=True)
        level = "申万一级" if len(industries) > 10 else "申万二级"

        md += f"**涨幅前5（{level}）：**\n"
        for i, ind in enumerate(sorted_inds[:5], 1):
            reason = _get_sector_reason(ind["name"], ind["pct"])
            md += f"{i}.  **{ind['name']}** {ind['pct']:+.2f}%  — {reason}\n"

        # 分离下跌和上涨板块
        decliners = [ind for ind in sorted_inds if ind["pct"] < 0]
        if decliners:
            md += f"\n**跌幅前5（{level}）：**\n"
            bottom5 = list(reversed(decliners[-5:]))  # 跌幅最大排第一
            for i, ind in enumerate(bottom5, 1):
                reason = _get_sector_reason(ind["name"], ind["pct"])
                md += f"{i}.  **{ind['name']}** {ind['pct']:+.2f}%  — {reason}\n"
        else:
            # 全线上涨，没有下跌板块——不显示"跌幅前5"误导性标题
            bottom = list(reversed(sorted_inds[-5:]))
            md += f"\n**今日全行业上涨**（{level}，涨幅最小前5）\n"
            for i, ind in enumerate(bottom, 1):
                md += f"{i}.  **{ind['name']}** {ind['pct']:+.2f}%\n"
    else:
        md += "（板块数据暂不可用，将在后续更新中补充）\n"

    # 热门概念前5
    if concepts:
        md += "\n**热门概念板块：**\n"
        for i, c in enumerate(concepts[:5], 1):
            md += f"{i}.  **{c['name']}** {c['pct']:+.2f}%\n"

    md += "\n---\n\n"

    # ================================================================
    # 三、自选股表现 — 价格表 + 技术面速览 + 技术信号预警
    # ================================================================
    md += "**三、自选股表现**\n\n"

    all_stocks = sorted(watchlist, key=lambda x: x.get("pct", 0), reverse=True)

    # --- 3a. 基本行情表 ---
    md += "| 股票 | 收盘价 | 涨跌幅 | 成交额 | 换手率 | 备注 |\n"
    md += "|------|--------|--------|--------|--------|------|\n"
    for s in all_stocks:
        pct = s.get("pct", 0)
        price = s.get("price", 0)
        amount = s.get("amount", 0)
        turnover = s.get("turnover", 0)
        is_hk = s.get("market") == "HK"
        code = s.get("code", "")
        price_sym = "HK$" if is_hk else "¥"
        amt_unit = "亿港元" if is_hk else "亿"
        t = watchlist_tech.get(code, {})
        note = _generate_stock_note(s, t)
        amt_str = f"{amount:.2f}{amt_unit}" if amount > 0 else "-"
        to_str = f"{turnover:.2f}%" if turnover > 0 else "-"
        md += f"| {s['name']}{'(HK)' if is_hk else ''} | {price_sym}{price:.2f} | {pct:+.2f}% | {amt_str} | {to_str} | {note} |\n"

    # --- 3b. 技术指标速览 ---
    if watchlist_tech:
        md += "\n**技术指标速览：**\n\n"
        md += "| 股票 | MACD | KDJ-J | RSI6 | 布林带 | 均线 | 量能 | 5日 | 20日 |\n"
        md += "|------|------|-------|------|--------|------|------|-----|-----|\n"
        for s in all_stocks:
            code = s.get("code", "")
            t = watchlist_tech.get(code, {})
            if not t:
                continue
            macd = t.get("macd_signal", "-")
            kdj_j = t.get("kdj_j")
            kdj_str = f"{kdj_j:.1f}" if kdj_j is not None else "-"
            rsi6 = t.get("rsi6")
            rsi_str = f"{rsi6:.1f}" if rsi6 is not None else "-"
            boll = t.get("boll_signal", "-")
            ma = t.get("ma_status", "-")
            vol = t.get("vol_ratio", "-")
            chg5 = t.get("chg_5d")
            chg5_str = f"{chg5:+.1f}%" if chg5 is not None else "-"
            chg20 = t.get("chg_20d")
            chg20_str = f"{chg20:+.1f}%" if chg20 is not None else "-"
            md += f"| {s['name']} | {macd} | {kdj_str} | {rsi_str} | {boll} | {ma} | {vol} | {chg5_str} | {chg20_str} |\n"

    # --- 3c. 技术信号预警 ---
    alerts = []
    for s in all_stocks:
        code = s.get("code", "")
        t = watchlist_tech.get(code, {})
        if not t:
            continue
        name = s["name"]
        macd = t.get("macd_signal", "")
        kdj = t.get("kdj_signal", "")
        rsi = t.get("rsi_signal", "")
        boll = t.get("boll_signal", "")
        if "金叉" in macd:
            alerts.append(f"🔴 **{name}**：MACD金叉，多头信号")
        if "死叉" in macd:
            alerts.append(f"🟢 **{name}**：MACD死叉，空头信号")
        if "超买" in kdj:
            alerts.append(f"🟠 **{name}**：KDJ超买（J={t.get('kdj_j','-'):.1f}），短线偏热")
        if "超卖" in kdj:
            alerts.append(f"🔵 **{name}**：KDJ超卖，可能超跌反弹")
        if "超买" in rsi:
            alerts.append(f"🟠 **{name}**：RSI6={t.get('rsi6','-'):.1f}超买")
        if "超卖" in rsi:
            alerts.append(f"🔵 **{name}**：RSI6超卖")
        if "突破布林上轨" in boll:
            alerts.append(f"🟡 **{name}**：突破布林上轨，短线偏强")
        if "跌破布林下轨" in boll:
            alerts.append(f"🟡 **{name}**：跌破布林下轨，超跌信号")

    if alerts:
        md += "\n**技术信号预警：**\n"
        for a in alerts[:8]:
            md += f"- {a}\n"

    # --- 3d. 个股技术分析文字总结（选取信号最强的3-5只）---
    sig_stocks = []
    for s in all_stocks:
        code = s.get("code", "")
        summary = watchlist_summary.get(code, "")
        if summary:
            sig_stocks.append((s, summary))
    if sig_stocks:
        md += "\n**重点个股技术分析：**\n"
        for s, summary in sig_stocks[:5]:
            pct = s.get("pct", 0)
            emoji = "🔴" if pct > 3 else ("🟢" if pct < -3 else "⚪")
            md += f"- {emoji} **{s['name']}**（{pct:+.2f}%）：{summary}\n"

    # --- 3e. 走势预测 ---
    if watchlist_tech:
        wl_kline = all_data.get("watchlist_kline", {})
        md += "\n**走势预测：**\n"
        for s in all_stocks:
            code = s.get("code", "")
            t = watchlist_tech.get(code, {})
            if not t:
                continue
            _, prediction, bull, bear = _gen_stock_analysis_text(s, t, wl_kline.get(code, []))
            # Shorten for DingTalk
            pred_short = prediction.replace("短期", "短").replace("中期", "中")
            md += f"- **{s['name']}**：{pred_short}\n"

    md += "\n---\n\n"

    # ================================================================
    # 四、今日要闻
    # ================================================================
    md += "**四、今日要闻**\n\n"
    if news:
        for n in news[:6]:
            src = n.get("source", "")
            src_str = f"（{src}）" if src else ""
            md += f"- {n['title']}{src_str}\n"
    else:
        md += "（今日暂无要闻数据）\n"

    md += "\n---\n\n"

    # ================================================================
    # 五、中期板块展望
    # ================================================================
    md += "**五、中期板块展望**\n\n"
    md += "基于近期走势、资金动向、政策面和基本面，分析中期（1-3个月）最看好的板块：\n\n"
    md += _mid_term_outlook(industries, fund_flows)
    md += "\n\n---\n\n"

    # ================================================================
    # 六、明日关注
    # ================================================================
    md += "**六、明日关注**\n\n"
    md += _tomorrow_watch(indices_kline, indices_tech, overview, news)

    report_url = get_report_url()
    md += f"\n\n[查看完整复盘报告]({report_url})\n"

    # 钉钉 markdown 不支持 emoji 和特殊 Unicode 符号，做清理
    md = _sanitize_dingtalk(md)

    return md


def _sanitize_dingtalk(text):
    """替换钉钉 markdown 不支持的 emoji 和特殊符号为纯文本。"""
    # 第一步: 先清理变体选择器 (让后续 emoji 匹配不受干扰)
    text = text.replace('\ufe0f', '')
    text = text.replace('\ufe0e', '')

    replacements = {
        # 彩色圆点 emoji (钉钉不渲染)
        '\U0001f534': '[涨]',   # 🔴
        '\U0001f7e2': '[跌]',   # 🟢
        '\U0001f7e0': '[注意]', # 🟠
        '\U0001f7e1': '[信号]', # 🟡
        '\U0001f535': '[超跌]', # 🔵
        '\u26aa': '',            # ⚪ → 删除
        # 其他 emoji
        '\U0001f4ca': '',        # 📊
        '\U0001f4cc': '',        # 📌
        '\u2705': '',            # ✅
        # 箭头符号
        '\u2191': '升',          # ↑
        '\u2193': '降',          # ↓
        '\u2197': '+',           # ↗
        '\u2198': '-',           # ↘
        # 警告符号
        '\u26a0': '',            # ⚠
        # 三角符号
        '\u25b2': '',            # ▲ → 删除
        '\u25bc': '',            # ▼ → 删除
        # 标点
        '\u00d7': 'x',           # ×
        '\u2014': '--',          # — (em dash)
        '\u201c': '"',           # "
        '\u201d': '"',           # "
        '\u300c': '[',           # 「
        '\u300d': ']',           # 」
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text