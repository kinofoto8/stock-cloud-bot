"""
收盘复盘报告生成器 - 直连东方财富 JSON API，生成带 ECharts 的 HTML 报告
"""
import json
import time
import requests
from datetime import datetime, timezone, timedelta

# ============================================================
# 配置
# ============================================================
WATCH_INDICES = [
    {"code": "000001", "name": "上证指数", "secid": "1.000001"},
    {"code": "399001", "name": "深证成指", "secid": "0.399001"},
    {"code": "399006", "name": "创业板指", "secid": "0.399006"},
    {"code": "000688", "name": "科创50",   "secid": "1.000688"},
    {"code": "000300", "name": "沪深300",  "secid": "1.000300"},
    {"code": "000905", "name": "中证500",  "secid": "1.000905"},
    {"code": "000852", "name": "中证1000", "secid": "1.000852"},
]

WATCHLIST_A = [
    {"code": "601899", "name": "紫金矿业", "secid": "1.601899"},
    {"code": "000426", "name": "兴业银锡", "secid": "0.000426"},
    {"code": "600489", "name": "中金黄金", "secid": "1.600489"},
    {"code": "000408", "name": "藏格矿业", "secid": "0.000408"},
    {"code": "600331", "name": "宏达股份", "secid": "1.600331"},
    {"code": "002240", "name": "盛新锂能", "secid": "0.002240"},
    {"code": "588170", "name": "科创半导体ETF华夏", "secid": "1.588170"},
    {"code": "600988", "name": "赤峰黄金", "secid": "1.600988"},
    {"code": "000807", "name": "云铝股份", "secid": "0.000807"},
    {"code": "000933", "name": "神火股份", "secid": "0.000933"},
]

WATCHLIST_HK = [
    {"code": "00883", "name": "中国海洋石油", "secid": "116.00883"},
    {"code": "09992", "name": "泡泡玛特", "secid": "116.09992"},
    {"code": "02259", "name": "紫金黄金国际", "secid": "116.02259"},
]

GITHUB_PAGES_BASE = "https://kinofoto8.github.io/stock-cloud-bot"

# ============================================================
# 类型安全转换工具
# ============================================================
def safe_float(val, default=0.0):
    """安全转换为 float，处理 None/空字符串/数字字符串。"""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default

def safe_int(val, default=0):
    """安全转换为 int。"""
    if val is None:
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default

# ============================================================
# HTTP 请求工具
# ============================================================
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
})

def fetch_json(url, params=None, retries=2):
    """请求东方财富 JSON 接口，带重试。"""
    for attempt in range(retries + 1):
        try:
            resp = SESSION.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("data") is not None or data.get("result") is not None:
                return data
        except Exception as e:
            if attempt < retries:
                time.sleep(1 * (attempt + 1))
            else:
                print(f"  [WARN] 请求失败 ({url[-50:]}): {e}")
                return {}
    return {}

# ============================================================
# 1. 市场总览
# ============================================================
def get_market_overview():
    """涨跌家数、涨停跌停、总成交额。"""
    print("  [1/7] 获取市场总览...")
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "6000", "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:0+t:6+f:!2,m:0+t:13+f:!2",
        "fields": "f2,f3,f8,f12,f14,f15,f16,f17,f18,f20",
    }
    data = fetch_json(url, params)
    if not data or "data" not in data or data["data"] is None:
        return {}
    
    items = data["data"].get("diff", [])
    if not items:
        return {}
    
    up = 0; down = 0; flat = 0; limit_up = 0; limit_down = 0; total_vol = 0
    for it in items:
        pct = safe_float(it.get("f3"))
        vol = safe_float(it.get("f20"))
        total_vol += vol
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
        if pct >= 9.8:
            limit_up += 1
        if pct <= -9.8:
            limit_down += 1
    
    return {
        "up": up, "down": down, "flat": flat,
        "limit_up": limit_up, "limit_down": limit_down,
        "total_volume": round(total_vol / 1e8, 2),  # 亿
        "total_stocks": len(items),
    }

# ============================================================
# 2. 指数行情
# ============================================================
def get_index_data():
    """获取各大指数收盘数据。"""
    print("  [2/7] 获取指数行情...")
    result = []
    for ind in WATCH_INDICES:
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": ind["secid"],
            "fields": "f43,f44,f45,f48,f169,f170",
        }
        data = fetch_json(url, params)
        d = data.get("data", {}) or {}
        result.append({
            "name": ind["name"],
            "code": ind["code"],
            "price": safe_float(d.get("f43")) / 100,
            "change": safe_float(d.get("f169")) / 100,
            "pct": safe_float(d.get("f170")) / 100,
            "high": safe_float(d.get("f44")) / 100,
            "low": safe_float(d.get("f45")) / 100,
            "volume": safe_float(d.get("f48")),
            "amount": safe_float(d.get("f48")) / 1e8 if d.get("f48") else 0,
        })
    return result

# ============================================================
# 3. 行业板块
# ============================================================
def get_industry_boards():
    """申万一级行业板块。"""
    print("  [3/7] 获取行业板块...")
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:90+t:2",
        "fields": "f2,f3,f12,f14",
    }
    data = fetch_json(url, params)
    items = (data.get("data") or {}).get("diff", [])
    print(f"  [DEBUG] industry: got {len(items)} items, data keys: {list(data.keys())[:5]}")
    boards = []
    for it in items:
        boards.append({
            "name": it.get("f14", ""),
            "code": it.get("f12", ""),
            "pct": safe_float(it.get("f3")),
            "price": safe_float(it.get("f2")),
            "rise_count": 0,
            "fall_count": 0,
            "flat_count": 0,
        })
    return boards

# ============================================================
# 4. 概念板块
# ============================================================
def get_concept_boards():
    """概念板块 Top 涨跌幅。"""
    print("  [4/7] 获取概念板块...")
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "500", "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:90+t:3",
        "fields": "f2,f3,f12,f14",
    }
    data = fetch_json(url, params)
    items = (data.get("data") or {}).get("diff", [])
    print(f"  [DEBUG] concepts: got {len(items)} items")
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

# ============================================================
# 5. 行业资金流向
# ============================================================
def get_industry_fund_flow():
    """行业板块资金流向。"""
    print("  [5/7] 获取资金流向...")
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f62",
        "fs": "m:90+t:2",
        "fields": "f2,f3,f12,f14,f62",
    }
    data = fetch_json(url, params)
    items = (data.get("data") or {}).get("diff", [])
    print(f"  [DEBUG] fund_flows: got {len(items)} items")
    flows = []
    for it in items:
        main_net = safe_float(it.get("f62"))
        flows.append({
            "name": it.get("f14", ""),
            "pct": safe_float(it.get("f3")),
            "main_net": main_net / 1e8,
            "super_large_net": 0,
            "large_net": 0,
            "medium_net": 0,
            "small_net": 0,
            "main_pct": 0,
        })
    flows.sort(key=lambda x: x["main_net"], reverse=True)
    return flows

# ============================================================
# 6. 自选股收盘
# ============================================================
def get_watchlist_data():
    """自选股收盘行情。"""
    print("  [6/7] 获取自选股行情...")
    result = []  # A 股
    debug_printed = False
    for s in WATCHLIST_A:
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": s["secid"],
            "fields": "f43,f44,f45,f48,f169,f170",
        }
        data = fetch_json(url, params)
        d = data.get("data", {}) or {}
        if not debug_printed:
            print(f"  [DEBUG] {s['name']}({s['code']}) secid={s['secid']} raw: {json.dumps(data, ensure_ascii=False)[:300]}")
            debug_printed = True
        result.append({
            "name": s["name"],
            "code": s["code"],
            "market": "A",
            "price": safe_float(d.get("f43")) / 100,
            "pct": safe_float(d.get("f170")) / 100,
            "change": safe_float(d.get("f169")) / 100,
            "high": safe_float(d.get("f44")) / 100,
            "low": safe_float(d.get("f45")) / 100,
            "volume": safe_float(d.get("f48")) / 1e8 if d.get("f48") else 0,
            "amount": safe_float(d.get("f48")) / 1e8 if d.get("f48") else 0,
            "turnover": 0,
            "amplitude": 0,
            "volume_ratio": 0,
        })
    
    # 港股
    for s in WATCHLIST_HK:
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": s["secid"],
            "fields": "f43,f44,f45,f48,f169,f170",
        }
        data = fetch_json(url, params)
        d = data.get("data", {}) or {}
        result.append({
            "name": s["name"],
            "code": s["code"],
            "market": "HK",
            "price": safe_float(d.get("f43")) / 1000,
            "pct": safe_float(d.get("f170")) / 100,
            "change": safe_float(d.get("f169")) / 1000,
            "high": safe_float(d.get("f44")) / 1000,
            "low": safe_float(d.get("f45")) / 1000,
            "volume": safe_float(d.get("f48")) / 1e8 if d.get("f48") else 0,
            "amount": safe_float(d.get("f48")) / 1e8 if d.get("f48") else 0,
            "turnover": 0,
            "amplitude": 0,
            "volume_ratio": 0,
        })
    return result

# ============================================================
# 7. 市场新闻
# ============================================================
def get_market_news():
    """获取市场重要新闻。"""
    print("  [7/7] 获取市场新闻...")
    try:
        # 尝试多个新闻源
        news = _try_news_api_1()
        if not news:
            news = _try_news_api_2()
        return news
    except Exception as e:
        print(f"  [WARN] 新闻获取失败: {e}")
        return []

def _try_news_api_1():
    """新闻源1：东方财富快讯 7x24。"""
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "10", "po": "1", "np": "1",
            "fltt": "2", "invt": "2", "fid": "f3",
            "fs": "m:0+t:1+f:!2",
            "fields": "f2,f3,f12,f14,f15,f16,f17,f18",
        }
        data = fetch_json(url, params)
        # 这不是真正的新闻接口，跳过
        return []
    except Exception:
        return []

def _try_news_api_2():
    """新闻源2：东方财富要闻列表。"""
    try:
        url = "https://np-listapi.eastmoney.com/comm/web/getNewsList"
        params = {
            "client": "web",
            "bizid": "1",
            "last_score": "0",
            "page_size": "10",
        }
        resp = SESSION.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items = (data.get("data") or {}).get("list", [])
        news = []
        for it in items[:10]:
            news.append({
                "title": it.get("title", ""),
                "time": it.get("showTime", ""),
                "source": it.get("source", ""),
            })
        return news
    except Exception as e:
        print(f"  [WARN] 新闻接口1失败: {e}")
        return []

# ============================================================
# HTML 报告生成
# ============================================================
def build_html_report(all_data, date_str):
    """生成完整 HTML 报告。"""
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
    
    # 涨跌统计
    up_count = overview.get("up", 0)
    down_count = overview.get("down", 0)
    flat_count = overview.get("flat", 0)
    total_stocks = overview.get("total_stocks", 0)
    limit_up = overview.get("limit_up", 0)
    limit_down = overview.get("limit_down", 0)
    total_vol = overview.get("total_volume", 0)
    
    # 资金流向总计
    total_main_flow = sum(f["main_net"] for f in fund_flows) if fund_flows else 0
    
    # ---- 构建 HTML ----
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

/* 首屏 */
.hero{{background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);color:#fff;border-radius:16px;padding:36px 40px;margin-bottom:20px}}
.hero h1{{font-size:26px;margin-bottom:8px}}
.hero .date{{font-size:14px;color:#a0a0b8;margin-bottom:20px}}
.hero .summary{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}}
.hero .summary-item{{text-align:center;padding:12px;background:rgba(255,255,255,.08);border-radius:10px}}
.hero .summary-item .num{{font-size:28px;font-weight:700}}
.hero .summary-item .num.up{{color:#ff6b6b}}
.hero .summary-item .num.down{{color:#51cf66}}
.hero .summary-item .label{{font-size:12px;color:#a0a0b8;margin-top:4px}}

/* 通用 section */
.section{{background:#fff;border-radius:12px;padding:28px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.section-title{{font-size:20px;font-weight:700;color:#1a1a2e;margin-bottom:20px;padding-left:12px;border-left:4px solid #c0392b}}

/* 表格 */
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:#f8f9fa;padding:10px 12px;text-align:left;font-weight:600;color:#555;border-bottom:2px solid #e0e0e0}}
td{{padding:9px 12px;border-bottom:1px solid #f0f0f0}}
tr:hover td{{background:#fafbfc}}
.num{{font-family:"SF Mono","Fira Code",monospace;text-align:right}}
.up{{color:#c0392b}}
.down{{color:#27ae60}}
.flat{{color:#999}}

/* 图表 */
.chart-box{{width:100%;border-radius:8px;overflow:hidden;margin:10px 0}}
.chart-row{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
@media(max-width:768px){{.chart-row{{grid-template-columns:1fr}}.hero .summary{{grid-template-columns:repeat(2,1fr)}}}}

/* 板块网格 */
.board-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.board-col h4{{font-size:15px;margin-bottom:12px;color:#555}}
.board-item{{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #f5f5f5;font-size:13px}}
.board-item .name{{flex:1}}
.board-item .pct{{font-weight:600}}
.board-item .count{{font-size:11px;color:#999;margin-left:8px}}

/* 资金流向条 */
.flow-item{{display:flex;align-items:center;gap:8px;margin:5px 0;font-size:12px}}
.flow-name{{width:90px;text-align:right;color:#555;flex-shrink:0}}
.flow-track{{flex:1;height:20px;background:#f0f0f0;border-radius:4px;position:relative;overflow:hidden}}
.flow-fill{{height:100%;border-radius:4px;position:absolute}}
.flow-pos{{background:linear-gradient(90deg,#e74c3c,#ff7676)}}
.flow-neg{{background:linear-gradient(90deg,#2ecc71,#27ae60)}}
.flow-val{{width:80px;font-family:monospace;font-size:11px;flex-shrink:0}}
.flow-center{{position:absolute;left:50%;top:0;bottom:0;width:1px;background:#ccc;z-index:2}}

/* 新闻 */
.news-list{{list-style:none;padding:0}}
.news-list li{{padding:8px 0;border-bottom:1px solid #f5f5f5;font-size:13px}}
.news-list li .time{{color:#999;font-size:11px;margin-right:8px}}

.source{{font-size:11px;color:#aaa;text-align:right;margin-top:40px;padding:10px 0}}
</style>
</head>
<body>
<div class="container">

<!-- 首屏 -->
<div class="hero">
  <h1>A股收盘复盘报告</h1>
  <div class="date">{display_date} | 数据来源：东方财富</div>
  <div class="summary">
    <div class="summary-item">
      <div class="num up">{up_count}</div>
      <div class="label">上涨 / 总数 {total_stocks}</div>
    </div>
    <div class="summary-item">
      <div class="num down">{down_count}</div>
      <div class="label">下跌</div>
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

<!-- 一、指数行情 -->
<div class="section">
  <div class="section-title">一、主要指数表现</div>
  <table>
    <thead><tr><th>指数</th><th>收盘价</th><th class="num">涨跌幅</th><th class="num">成交额(亿)</th></tr></thead>
    <tbody>
'''
    for idx in indices:
        pct = idx.get("pct", 0)
        cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")
        amt = idx.get("amount", 0)
        html += f'      <tr><td>{idx["name"]}</td><td class="num">{idx.get("price",0):.2f}</td><td class="num {cls}">{pct:+.2f}%</td><td class="num">{amt:.2f}</td></tr>\n'
    
    html += '''    </tbody>
  </table>
</div>

<!-- 二、板块表现 + 资金流向 -->
<div class="chart-row">
'''
    # 行业板块涨跌图表
    industry_names = [b["name"] for b in industries[:10]]
    industry_pcts = [b["pct"] for b in industries[:10]]
    industry_colors = ["#c0392b" if p > 0 else "#27ae60" for p in industry_pcts]
    
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

<!-- 四、概念板块 -->
<div class="section">
  <div class="section-title">四、概念板块热力图</div>
  <div class="board-grid">
    <div class="board-col">
      <h4>🔥 涨幅 Top 10</h4>
'''
    for b in concepts[:10]:
        cls = "up" if b["pct"] > 0 else "down"
        html += f'      <div class="board-item"><span class="name">{b["name"]}</span><span class="pct {cls}">{b["pct"]:+.2f}%</span></div>\n'
    
    html += '''    </div>
    <div class="board-col">
      <h4>❄️ 跌幅 Top 10</h4>
'''
    for b in concepts[-10:]:
        cls = "up" if b["pct"] > 0 else "down"
        html += f'      <div class="board-item"><span class="name">{b["name"]}</span><span class="pct {cls}">{b["pct"]:+.2f}%</span></div>\n'
    
    html += '''    </div>
  </div>
</div>

<!-- 五、自选股 -->
<div class="section">
  <div class="section-title">五、自选股表现</div>
  <table>
    <thead><tr><th>股票</th><th class="num">收盘价</th><th class="num">涨跌幅</th><th class="num">换手率</th><th class="num">量比</th><th class="num">成交额(亿)</th></tr></thead>
    <tbody>
'''
    for s in watchlist:
        pct = s.get("pct", 0)
        cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")
        pct_disp = f"**{pct:+.2f}%**" if abs(pct) >= 3 else f"{pct:+.2f}%"
        amt = s.get("amount", 0)
        html += f'''      <tr><td>{s["name"]} <span style="color:#999;font-size:11px">({s["code"]})</span></td>
        <td class="num">{s.get("price",0):.2f}</td>
        <td class="num {cls}">{pct_disp}</td>
        <td class="num">{s.get("turnover",0):.2f}%</td>
        <td class="num">{s.get("volume_ratio",0):.2f}</td>
        <td class="num">{amt:.2f}</td></tr>
'''
    
    html += '''    </tbody>
  </table>
</div>

<!-- 六、市场新闻 -->
<div class="section">
  <div class="section-title">六、今日要闻</div>
  <ul class="news-list">
'''
    for n in news:
        html += f'    <li><span class="time">{n.get("time","")}</span>{n["title"]}</li>\n'
    if not news:
        html += '    <li style="color:#999">暂无重要新闻</li>\n'
    
    html += f'''  </ul>
</div>

<div class="source">
  以上数据由云端自动化生成，仅供参考，不构成投资建议 | 生成时间：{now.strftime("%Y-%m-%d %H:%M:%S")}
</div>

</div><!-- container -->

<script>
// 行业板块图表
(function() {{
  var dom = document.getElementById('chart-industry');
  var chart = echarts.init(dom);
  var option = {{
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }},
    grid: {{ left: '12%', right: '5%', top: '3%', bottom: '3%', containLabel: true }},
    xAxis: {{ type: 'value', axisLabel: {{ formatter: '{{value}}%' }} }},
    yAxis: {{ type: 'category', data: {json.dumps(industry_names[::-1])}, axisLabel: {{ fontSize: 12 }} }},
    series: [{{
      type: 'bar',
      data: {json.dumps([round(p,2) for p in industry_pcts[::-1]])},
      itemStyle: {{
        color: function(params) {{ return params.value > 0 ? '#c0392b' : '#27ae60'; }}
      }},
      label: {{ show: true, position: 'right', formatter: '{{c}}%', fontSize: 11 }}
    }}]
  }};
  chart.setOption(option);
  window.addEventListener('resize', function() {{ chart.resize(); }});
}})();

// 资金流向图表
(function() {{
  var dom = document.getElementById('chart-fundflow');
  var chart = echarts.init(dom);
  var flowData = {json.dumps([{"name": f["name"], "value": round(f["main_net"], 2)} for f in (fund_flows[:5] + fund_flows[-5:])])};
  var names = flowData.map(function(d) {{ return d.name; }});
  var values = flowData.map(function(d) {{ return d.value; }});
  var option = {{
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }},
    grid: {{ left: '12%', right: '5%', top: '3%', bottom: '3%', containLabel: true }},
    xAxis: {{ type: 'value', axisLabel: {{ formatter: '{{value}}亿' }} }},
    yAxis: {{ type: 'category', data: names.reverse(), axisLabel: {{ fontSize: 11 }} }},
    series: [{{
      type: 'bar',
      data: values.reverse(),
      itemStyle: {{
        color: function(params) {{ return params.value > 0 ? '#c0392b' : '#27ae60'; }}
      }},
      label: {{ show: true, position: 'right', formatter: '{{c}}亿', fontSize: 11 }}
    }}]
  }};
  chart.setOption(option);
  window.addEventListener('resize', function() {{ chart.resize(); }});
}})();
</script>
</body>
</html>'''
    
    return html

# ============================================================
# 入口
# ============================================================
def fetch_all_data():
    """获取所有数据，返回 dict。"""
    return {
        "overview": get_market_overview(),
        "indices": get_index_data(),
        "industries": get_industry_boards(),
        "concepts": get_concept_boards(),
        "fund_flows": get_industry_fund_flow(),
        "watchlist": get_watchlist_data(),
        "news": get_market_news(),
    }

def generate_report(all_data, output_dir="reports"):
    """生成 HTML 报告并返回文件路径。"""
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
    """获取报告在线地址。"""
    if date_str is None:
        beijing_tz = timezone(timedelta(hours=8))
        date_str = datetime.now(beijing_tz).strftime("%Y-%m-%d")
    return f"{GITHUB_PAGES_BASE}/reports/{date_str}.html"

def build_summary_md(all_data):
    """构建钉钉推送摘要。"""
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
    
    md = f"### 📊 A股收盘复盘\n**{display_date}**\n\n"
    md += f"---\n\n"
    md += f"**市场概况：** 上涨 {up} 家 / 下跌 {down} 家 | 涨停 {limit_up} / 跌停 {limit_down} | 成交 {total_vol:.0f} 亿\n\n"
    
    # 指数
    md += "**主要指数：**\n"
    for idx in indices:
        pct = idx.get("pct", 0)
        emoji = "🔴" if pct > 0 else ("🟢" if pct < 0 else "⚪")
        md += f"- {emoji} {idx['name']}: {idx.get('price',0):.2f} ({pct:+.2f}%)\n"
    
    # 自选股涨跌幅前三
    a_stocks = [s for s in watchlist if s["market"] == "A"]
    a_stocks.sort(key=lambda x: x.get("pct", 0), reverse=True)
    md += f"\n**自选股前三：**\n"
    for s in a_stocks[:3]:
        pct = s.get("pct", 0)
        md += f"- {s['name']}: {pct:+.2f}%\n"
    
    report_url = get_report_url()
    md += f"\n📄 [查看完整复盘报告]({report_url})\n"
    
    return md
