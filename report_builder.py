"""
收盘复盘报告生成器 v3 — Sina 财经 + 东方财富混合方案，带 ECharts 的 HTML 报告
"""
import json
import time
import re
import requests
from datetime import datetime, timezone, timedelta

# ============================================================
# 配置
# ============================================================
INDICES = [
    {"code": "000001", "name": "上证指数", "sina": "s_sh000001"},
    {"code": "399001", "name": "深证成指", "sina": "s_sz399001"},
    {"code": "399006", "name": "创业板指", "sina": "s_sz399006"},
    {"code": "000688", "name": "科创50",   "sina": "s_sh000688"},
    {"code": "000300", "name": "沪深300",  "sina": "s_sh000300"},
    {"code": "000905", "name": "中证500",  "sina": "s_sh000905"},
    {"code": "000852", "name": "中证1000", "sina": "s_sh000852"},
]

WATCHLIST = [
    {"code": "601899", "name": "紫金矿业", "sina": "sh601899", "market": "A"},
    {"code": "000426", "name": "兴业银锡", "sina": "sz000426", "market": "A"},
    {"code": "600489", "name": "中金黄金", "sina": "sh600489", "market": "A"},
    {"code": "000408", "name": "藏格矿业", "sina": "sz000408", "market": "A"},
    {"code": "600331", "name": "宏达股份", "sina": "sh600331", "market": "A"},
    {"code": "002240", "name": "盛新锂能", "sina": "sz002240", "market": "A"},
    {"code": "588170", "name": "科创半导体ETF华夏", "sina": "sh588170", "market": "A"},
    {"code": "600988", "name": "赤峰黄金", "sina": "sh600988", "market": "A"},
    {"code": "000807", "name": "云铝股份", "sina": "sz000807", "market": "A"},
    {"code": "000933", "name": "神火股份", "sina": "sz000933", "market": "A"},
    {"code": "00883", "name": "中国海洋石油", "sina": "hk00883", "market": "HK"},
    {"code": "09992", "name": "泡泡玛特", "sina": "hk09992", "market": "HK"},
    {"code": "02259", "name": "紫金黄金国际", "sina": "hk02259", "market": "HK"},
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
# Sina 财经 API — 批量获取个股/指数行情
# ============================================================
SINA_QUOTE_URL = "https://hq.sinajs.cn/list="

def fetch_sina_quotes(sina_codes):
    """批量获取 Sina 行情数据。返回 dict: {sina_code: parsed_dict}。
    
    返回格式（个股）:
    name, open, prev_close, price, high, low, volume(手), amount(万), ...
    
    返回格式（指数）:
    name, price, change, pct, volume(手), amount(万), ...
    """
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
        # 匹配: var hq_str_XXX="..."
        m = re.match(r'var hq_str_(\w+)="(.+)"', line)
        if not m:
            continue
        code = m.group(1)
        data = m.group(2).split(",")
        if len(data) < 5:
            continue
        
        try:
            # 判断是指数还是个股：指数第1行是名字，第2行是价格；个股第1行是名字，第2行是今开
            name = data[0]
            # 指数格式: name, price, change, pct, volume_hand, amount_wan
            # 个股格式: name, open, prev_close, price, high, low, ...
            is_index = code.startswith("s_")
            
            if is_index:
                price = safe_float(data[1])
                change = safe_float(data[2])
                pct = safe_float(data[3])
                volume = safe_float(data[4])
                amount = safe_float(data[5]) / 1e4  # 万→亿
                result[code] = {
                    "name": name, "price": price, "change": change,
                    "pct": pct, "volume": volume, "amount": amount,
                    "is_index": True,
                }
            else:
                # 个股: name, open, prev_close, price, high, low, ...
                price = safe_float(data[3])  # 当前价
                open_p = safe_float(data[1])
                prev_close = safe_float(data[2])
                high = safe_float(data[4])
                low = safe_float(data[5])
                volume_hand = safe_float(data[8])  # 成交量(手)
                amount = safe_float(data[9]) / 1e4  # 万→亿
                
                if prev_close > 0 and price > 0:
                    pct = round((price - prev_close) / prev_close * 100, 2)
                    change = round(price - prev_close, 2)
                else:
                    pct = 0
                    change = 0
                
                # 换手率 (字段37), 量比 (因Sina不直接提供，后面用东方财富补)
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
# 东方财富 API — 板块/资金流向/换手率
# ============================================================
EM_SESSION = requests.Session()
EM_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
})

# 东方财富防爬令牌
EM_UT = "fa5fd1943c7b386f172d6893dbfba10b"

def em_fetch_json(url, params):
    """请求东方财富 JSON 接口，自动添加 ut 令牌。"""
    params["ut"] = EM_UT
    for attempt in range(3):
        try:
            resp = EM_SESSION.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("data") is not None:
                return data
            # 打印调试信息
            if attempt == 2:
                print(f"  [DEBUG] EM API returned null data for: {params.get('fs','?')[:60]}")
        except Exception as e:
            if attempt == 2:
                print(f"  [WARN] EM 请求失败: {e}")
            time.sleep(1 * (attempt + 1))
    return {}

def get_market_overview():
    """市场总览：涨跌家数、成交额。"""
    print("  [1/5] 获取市场总览...")
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    # 使用和之前一样但带 ut 的方式
    params = {
        "pn": "1", "pz": "100", "po": "0", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f2,f3,f12,f14,f15,f16,f17,f18,f20",
    }
    data = em_fetch_json(url, params)
    items = (data.get("data") or {}).get("diff", [])
    print(f"  [DEBUG] overview: {len(items)} items")
    
    if not items:
        return {}
    
    up = down = flat = limit_up = limit_down = total_vol = 0
    for it in items:
        pct = safe_float(it.get("f3"))
        vol = safe_float(it.get("f20"))
        total_vol += vol
        if pct > 0: up += 1
        elif pct < 0: down += 1
        else: flat += 1
        if pct >= 9.8: limit_up += 1
        if pct <= -9.8: limit_down += 1
    
    return {
        "up": up, "down": down, "flat": flat,
        "limit_up": limit_up, "limit_down": limit_down,
        "total_volume": round(total_vol / 1e8, 2),
        "total_stocks": len(items),
    }

def get_index_data():
    """指数行情 — 用 Sina API。"""
    print("  [2/5] 获取指数行情...")
    sina_codes = [idx["sina"] for idx in INDICES]
    quotes = fetch_sina_quotes(sina_codes)
    
    result = []
    for idx in INDICES:
        q = quotes.get(idx["sina"], {})
        result.append({
            "name": idx["name"],
            "code": idx["code"],
            "price": q.get("price", 0),
            "pct": q.get("pct", 0),
            "change": q.get("change", 0),
            "volume": q.get("volume", 0),
            "amount": q.get("amount", 0),
        })
    return result

def get_industry_boards():
    """行业板块 — 用东方财富 clist/get + ut。"""
    print("  [3/5] 获取行业板块...")
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:90+t:2",
        "fields": "f2,f3,f12,f14,f104,f105,f106",
    }
    data = em_fetch_json(url, params)
    items = (data.get("data") or {}).get("diff", [])
    print(f"  [DEBUG] industry boards: {len(items)} items")
    
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

def get_concept_boards():
    """概念板块。"""
    print("  [4/5] 获取概念板块...")
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "500", "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:90+t:3",
        "fields": "f2,f3,f12,f14",
    }
    data = em_fetch_json(url, params)
    items = (data.get("data") or {}).get("diff", [])
    print(f"  [DEBUG] concept boards: {len(items)} items")
    
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

def get_industry_fund_flow():
    """行业资金流向。"""
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
    print(f"  [DEBUG] fund flows: {len(items)} items")
    
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
    """自选股行情 — 用 Sina API 批量获取。"""
    print("    获取自选股行情...")
    
    # 用 Sina API 批量获取 A 股和港股
    all_sina = [s["sina"] for s in WATCHLIST]
    quotes = fetch_sina_quotes(all_sina)
    
    result = []
    for s in WATCHLIST:
        q = quotes.get(s["sina"], {})
        if not q:
            result.append({
                "name": s["name"], "code": s["code"], "market": s["market"],
                "price": 0, "pct": 0, "change": 0,
                "high": 0, "low": 0, "volume": 0, "amount": 0,
                "turnover": 0, "volume_ratio": 0,
            })
            continue
        
        # 成交额从手转亿
        amount_yi = q.get("amount", 0) if s["market"] == "A" else q.get("amount", 0)
        
        result.append({
            "name": s["name"],
            "code": s["code"],
            "market": s["market"],
            "price": q.get("price", 0),
            "pct": q.get("pct", 0),
            "change": q.get("change", 0),
            "high": q.get("high", 0),
            "low": q.get("low", 0),
            "volume": q.get("volume_hand", 0),
            "amount": amount_yi,
            "turnover": 0,
            "volume_ratio": 0,
        })
    
    # 用东方财富补充换手率和量比
    _enrich_with_turnover(result)
    
    return result

def _enrich_with_turnover(result):
    """用东方财富 API 补充 A股的换手率和量比。"""
    # 构建 secid 列表
    secid_map = {}
    for s in WATCHLIST:
        if s["market"] == "A":
            prefix = "1" if s["code"].startswith(("6", "5")) else "0"
            secid_map[f"{prefix}.{s['code']}"] = s["code"]
    
    if not secid_map:
        return
    
    # 逐个查询（东方财富 stock/get 可能对批量有不同限制）
    for secid, code in secid_map.items():
        try:
            params = {
                "secid": secid,
                "fields": "f168,f50,f51",
                "ut": EM_UT,
                "fltt": "2",
                "invt": "2",
            }
            resp = EM_SESSION.get("https://push2.eastmoney.com/api/qt/stock/get",
                                   params=params, timeout=10)
            d = (resp.json().get("data") or {})
            turnover = safe_float(d.get("f168")) / 100  # 换手率
            amplitude = safe_float(d.get("f50")) / 100   # 振幅
            vol_ratio = safe_float(d.get("f51")) / 100   # 量比
            
            # 更新结果
            for r in result:
                if r["code"] == code:
                    r["turnover"] = turnover
                    r["amplitude"] = amplitude if "amplitude" in r else 0
                    r["volume_ratio"] = vol_ratio
                    break
        except Exception:
            continue

def get_market_news():
    """获取市场新闻。"""
    try:
        url = "https://np-listapi.eastmoney.com/comm/web/getNewsList"
        params = {
            "client": "web", "bizid": "1",
            "last_score": "0", "page_size": "10",
        }
        resp = SESSION.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items = (data.get("data") or {}).get("list", [])
        return [{"title": it.get("title",""), "time": it.get("showTime",""),
                 "source": it.get("source","")} for it in items[:10]]
    except Exception as e:
        print(f"  [WARN] 新闻获取失败: {e}")
        return []

# ============================================================
# 入口
# ============================================================
def fetch_all_data():
    print("=" * 50)
    print("开始数据采集 (Sina + 东方财富)...")
    print("=" * 50)
    
    # 并行获取
    overview = get_market_overview()
    indices = get_index_data()
    industries = get_industry_boards()
    concepts = get_concept_boards()
    fund_flows = get_industry_fund_flow()
    watchlist = get_watchlist_data()
    news = get_market_news()
    
    print(f"\n数据采集完成:")
    print(f"  市场总览: {overview}")
    print(f"  指数: {len(indices)} 个")
    print(f"  行业板块: {len(industries)} 个")
    print(f"  概念板块: {len(concepts)} 个")
    print(f"  资金流向: {len(fund_flows)} 个")
    print(f"  自选股: {len(watchlist)} 只")
    print(f"  新闻: {len(news)} 条")
    
    # 打印自选股样本
    for s in watchlist[:3]:
        print(f"  [{s['name']}] price={s['price']} pct={s['pct']}% amount={s['amount']:.2f}亿")
    
    return {
        "overview": overview,
        "indices": indices,
        "industries": industries,
        "concepts": concepts,
        "fund_flows": fund_flows,
        "watchlist": watchlist,
        "news": news,
    }

# ============================================================
# HTML 报告生成
# ============================================================
def build_html_report(all_data, date_str):
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

    up_count = overview.get("up", 0)
    down_count = overview.get("down", 0)
    total_stocks = overview.get("total_stocks", 0)
    limit_up = overview.get("limit_up", 0)
    limit_down = overview.get("limit_down", 0)
    total_vol = overview.get("total_volume", 0)
    
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
.container{{max-width:1100px;margin:0 auto;padding:20px}}
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
th{{background:#f8f9fa;padding:10px 12px;text-align:left;font-weight:600;color:#555;border-bottom:2px solid #e0e0e0}}
td{{padding:9px 12px;border-bottom:1px solid #f0f0f0}}
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
.source{{font-size:11px;color:#aaa;text-align:right;margin-top:40px;padding:10px 0}}
</style>
</head>
<body>
<div class="container">
<div class="hero">
  <h1>A股收盘复盘报告</h1>
  <div class="date">{display_date} | 数据来源：Sina财经 & 东方财富</div>
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
      <div class="num">{(total_vol/10000):.2f}万</div>
      <div class="label">两市成交（亿）</div>
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
    industry_names = [b["name"] for b in industries[:10]]
    industry_pcts = [b["pct"] for b in industries[:10]]
    
    html += f'''  <div class="section" style="margin-bottom:0">
    <div class="section-title">二、行业板块 Top 10</div>
    <div class="chart-box" id="chart-industry" style="height:350px"></div>
    <div style="margin-top:12px;font-size:12px;color:#999">* 申万一级行业，按涨跌幅排序</div>
  </div>
  <div class="section" style="margin-bottom:0">
    <div class="section-title">三、行业资金流向</div>
    <div class="chart-box" id="chart-fundflow" style="height:350px"></div>
    <div style="margin-top:12px;font-size:12px;color:#999">* 主力净流入 TOP/BOTTOM 5（亿元）</div>
  </div>
</div>

<div class="section">
  <div class="section-title">四、概念板块热力图</div>
  <div class="board-grid">
    <div class="board-col">
      <h4>&#x1f525; 涨幅 Top 10</h4>
'''
    for b in concepts[:10]:
        cls = "up" if b["pct"] > 0 else "down"
        html += f'      <div class="board-item"><span class="name">{b["name"]}</span><span class="pct {cls}">{b["pct"]:+.2f}%</span></div>\n'
    
    html += '''    </div>
    <div class="board-col">
      <h4>&#x2744; 跌幅 Top 10</h4>
'''
    for b in concepts[-10:]:
        cls = "up" if b["pct"] > 0 else "down"
        html += f'      <div class="board-item"><span class="name">{b["name"]}</span><span class="pct {cls}">{b["pct"]:+.2f}%</span></div>\n'
    
    html += '''    </div></div></div>

<div class="section">
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

<div class="section">
  <div class="section-title">六、今日要闻</div>
  <ul class="news-list">
'''
    for n in news:
        html += f'    <li><span class="time">{n.get("time","")}</span>{n["title"]}</li>\n'
    if not news:
        html += '    <li style="color:#999">暂无重要新闻</li>\n'
    
    html += f'''  </ul></div>
<div class="source">
  以上数据由云端自动化生成，仅供参考，不构成投资建议 | 生成时间：{now.strftime("%Y-%m-%d %H:%M:%S")}
</div></div>

<script>
(function() {{
  var chart = echarts.init(document.getElementById('chart-industry'));
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

(function() {{
  var chart = echarts.init(document.getElementById('chart-fundflow'));
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
    import os
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
    
    up = overview.get("up", 0)
    down = overview.get("down", 0)
    limit_up = overview.get("limit_up", 0)
    limit_down = overview.get("limit_down", 0)
    total_vol = overview.get("total_volume", 0)
    
    beijing_tz = timezone(timedelta(hours=8))
    now = datetime.now(beijing_tz)
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    display_date = f"{now.strftime('%Y-%m-%d')}（周{weekdays[now.weekday()]}）"
    
    md = f"### 📊 A股收盘复盘\n**{display_date}**\n\n---\n\n"
    md += f"**市场概况：** 上涨 **{up}** 家 / 下跌 **{down}** 家 | 涨停 **{limit_up}** / 跌停 **{limit_down}** | 成交 **{total_vol:.0f}** 亿\n\n"
    
    md += "**主要指数：**\n"
    for idx in indices:
        pct = idx.get("pct", 0)
        emoji = "🔴" if pct > 0 else ("🟢" if pct < 0 else "⚪")
        md += f"- {emoji} **{idx['name']}**: {idx.get('price',0):.2f} ({pct:+.2f}%)\n"
    
    # 自选股涨跌幅前三
    a_stocks = [s for s in watchlist if s["market"] == "A"]
    a_stocks.sort(key=lambda x: x.get("pct", 0), reverse=True)
    if a_stocks:
        md += f"\n**自选股涨幅前三：**\n"
        for s in a_stocks[:3]:
            md += f"- {s['name']}: {s.get('pct',0):+.2f}%\n"
        md += f"\n**自选股跌幅前三：**\n"
        for s in a_stocks[-3:]:
            md += f"- {s['name']}: {s.get('pct',0):+.2f}%\n"
    
    report_url = get_report_url()
    md += f"\n📄 [查看完整复盘报告]({report_url})\n"
    
    return md
