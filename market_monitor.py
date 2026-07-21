"""
自选股盘中监控 v2 — 纯腾讯API架构 (不依赖akshare)
每个交易日 10:30, 14:30 执行
对 13 只自选股进行多维度扫描，发现预警信号则推送到钉钉。

改进 (对齐每日复盘 v7.4):
  1. 数据源: 腾讯API批量行情 (快速稳定, 不依赖akshare)
  2. 成交额环比变化: 显示较前一交易日的成交额放大/缩小
  3. 港股换手率: 从腾讯港股K线API提取
  4. 技术分析文字总结: 每只股票附带K线/布林带/成交量等分析
  5. 新闻来源: 同花顺24小时滚动新闻
  6. 个股资金流向: EM API直连 (非akshare), 只提示真正净流出
"""
import sys
import os
import requests
from datetime import datetime, time as dtime

# 从 report_builder 导入腾讯API函数和技术分析工具
from report_builder import (
    fetch_tencent_quotes, get_kline_tencent, get_kline_tencent_hk,
    analyze_technicals, generate_tech_summary, _calc_amount_change_pct,
    get_market_news, WATCHLIST, INDICES, safe_float, SESSION,
)
from config import ALERT_THRESHOLDS
from dingtalk_push import send_markdown


def now_str():
    return datetime.now().strftime("%m月%d日 %H:%M")


def is_trade_day_simple(date_str=None):
    """简单交易日检查 (不依赖akshare, 仅判断周一至周五)。"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    wd = datetime.strptime(date_str, "%Y-%m-%d").weekday()
    return wd < 5


def get_fund_flow_em(secid, n=3):
    """通过EM API获取个股资金流向 (不依赖akshare)。
    secid: EM格式代码 (如 "1.601899")
    返回: [{"date", "main_net", "main_pct", ...}, ...] 或 []
    """
    try:
        resp = SESSION.get(
            "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get",
            params={
                "secid": secid,
                "lmt": str(n),
                "klt": "101",
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                "ut": "b2884a393a59ad64002292a3e90d46a5",
            },
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://quote.eastmoney.com/",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        klines = (data.get("data") or {}).get("klines", [])
        results = []
        for k in klines:
            parts = k.split(",")
            if len(parts) >= 7:
                results.append({
                    "date": parts[0],
                    "main_net": safe_float(parts[1]),       # 主力净流入-净额(元)
                    "super_large_net": safe_float(parts[2]),
                    "large_net": safe_float(parts[3]),
                    "medium_net": safe_float(parts[4]),
                    "small_net": safe_float(parts[5]),
                    "main_pct": safe_float(parts[6]),        # 主力净流入-净占比(%)
                })
        return results
    except Exception as e:
        print(f"  [WARN] EM资金流向获取失败 {secid}: {e}")
        return []


def run_monitor():
    """主入口：执行盘中监控。"""
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now()

    # ---- 1. 交易日检查 ----
    if not is_trade_day_simple(today):
        print(f"[{now_str()}] 今日休市，无需监控。")
        return {"status": "holiday", "msg": "今日休市"}

    print(f"[{now_str()}] 交易日确认，开始扫描 {len(WATCHLIST)} 只自选股...")

    # ---- 2. 获取实时行情 (腾讯API批量, 1次调用) ----
    tencent_symbols = [s["sina"] for s in WATCHLIST]
    quotes = fetch_tencent_quotes(tencent_symbols)

    # 获取指数行情
    index_symbols = [idx["sina"].replace("s_", "") for idx in INDICES]
    index_quotes = fetch_tencent_quotes(index_symbols)

    if not quotes:
        msg = f"{now_str()} 腾讯API行情获取失败，无法监控。"
        print(msg)
        try:
            send_markdown("自选股盘中监控", f"### ⚠️ 监控异常\n**{now_str()}**\n\n行情数据获取失败，请检查网络。")
        except Exception:
            pass
        return {"status": "error", "msg": msg}

    # ---- 3. 逐只扫描 ----
    alerts = []
    normal = []
    now_time = now.time()
    a_session = dtime(9, 25) <= now_time <= dtime(15, 5)
    hk_session = dtime(9, 25) <= now_time <= dtime(16, 5)

    for stock in WATCHLIST:
        tencent_sym = stock["sina"]
        code = stock["code"]
        name = stock["name"]
        market = stock["market"]
        secid = stock.get("secid", "")

        q = quotes.get(tencent_sym)
        if not q or q["price"] == 0:
            normal.append({
                "name": name, "code": code, "price": "N/A", "pct": "N/A",
                "turnover": "-", "amount": "-", "amount_change": None,
                "tech_summary": "", "vol_ratio": 0,
            })
            continue

        price = q["price"]
        pct = q["pct"]
        turnover = q["turnover"]
        vol_ratio = q["volume_ratio"]
        amount = q["amount"]
        is_hk = q.get("is_hk", market == "HK")

        # 判断是否在交易时段
        in_session = hk_session if is_hk else a_session

        # 港股换手率: 从K线API获取
        klines = []
        if is_hk and turnover == 0:
            klines = get_kline_tencent_hk(tencent_sym, 5)
            if klines:
                turnover = klines[-1].get("turnover", 0)

        stock_alerts = []

        # ---- 3a. 涨跌幅预警 ----
        if abs(pct) >= ALERT_THRESHOLDS["change_pct_abs"]:
            direction = "暴涨" if pct > 0 else "暴跌"
            stock_alerts.append(f"{direction}: 当日涨跌幅 {pct:+.2f}%")

        # ---- 3b. 量比 / 换手率预警 ----
        if vol_ratio >= ALERT_THRESHOLDS["volume_ratio"]:
            stock_alerts.append(f"放量: 量比 {vol_ratio:.1f}")
        if turnover >= ALERT_THRESHOLDS["turnover_rate"]:
            stock_alerts.append(f"高换手: 换手率 {turnover:.1f}%")

        # ---- 3c. 技术指标 + 文字总结 ----
        tech_summary = ""
        tech_data = {}
        if in_session and price > 0:
            if len(klines) < 30:
                if is_hk:
                    klines = get_kline_tencent_hk(tencent_sym, 60)
                else:
                    klines = get_kline_tencent(tencent_sym, 60)

            if klines and len(klines) >= 30:
                tech_data = analyze_technicals(klines)
                tech_summary = generate_tech_summary(klines, tech_data, name)

                # 检查预警信号
                macd_sig = tech_data.get("macd_signal", "")
                if "死叉" in macd_sig:
                    stock_alerts.append(f"MACD{macd_sig}")

                kdj_sig = tech_data.get("kdj_signal", "")
                if "超买" in kdj_sig:
                    kdj_j = tech_data.get("kdj_j", 0)
                    stock_alerts.append(f"KDJ{kdj_sig} (J={kdj_j:.1f})")

                rsi_sig = tech_data.get("rsi_signal", "")
                rsi6 = tech_data.get("rsi6")
                if "超买" in rsi_sig and rsi6 is not None:
                    stock_alerts.append(f"RSI超买: RSI(6)={rsi6:.1f}")

                boll_sig = tech_data.get("boll_signal", "")
                if "突破上轨" in boll_sig:
                    stock_alerts.append("触及布林上轨后回落风险")

                chg_5d = tech_data.get("chg_5d")
                chg_20d = tech_data.get("chg_20d")
                if chg_5d is not None and abs(chg_5d) >= ALERT_THRESHOLDS["chg_5d_abs"]:
                    stock_alerts.append(f"5日累计涨跌幅 {chg_5d:+.2f}%")
                if chg_20d is not None and abs(chg_20d) >= ALERT_THRESHOLDS["chg_20d_abs"]:
                    stock_alerts.append(f"20日累计涨跌幅 {chg_20d:+.2f}%")

        # ---- 3d. 成交额环比 ----
        amt_change = _calc_amount_change_pct(klines) if klines else None

        # ---- 3e. 资金流向 (EM API, 仅A股, 只提示真正净流出) ----
        if in_session and not is_hk and secid:
            try:
                flows = get_fund_flow_em(secid, n=3)
                if flows:
                    latest = flows[-1]
                    main_net = latest["main_net"]
                    # 主力流出且占比超过阈值
                    if main_net < 0 and amount > 0:
                        outflow_pct = abs(main_net) / (amount * 1e8)
                        if outflow_pct > ALERT_THRESHOLDS["fund_outflow_pct"]:
                            stock_alerts.append(
                                f"主力资金出逃: 净流出 {main_net/1e8:.2f}亿, 占比 {outflow_pct*100:.1f}%"
                            )

                    # 连续3日主力流出
                    if len(flows) >= 3 and all(f["main_net"] < 0 for f in flows):
                        total_out = sum(f["main_net"] for f in flows)
                        stock_alerts.append(
                            f"连续3日主力净流出: 累计 {total_out/1e8:.2f}亿"
                        )
            except Exception:
                pass  # 资金流向非核心，静默失败

        if stock_alerts:
            alerts.append({
                "name": name, "code": code, "market": market,
                "price": price, "change_pct": pct,
                "turnover": turnover, "amount": amount,
                "amount_change": amt_change,
                "vol_ratio": vol_ratio,
                "tech_summary": tech_summary,
                "reasons": stock_alerts,
            })
        else:
            normal.append({
                "name": name, "code": code,
                "price": f"{price:.2f}" if price else "N/A",
                "pct": f"{pct:+.2f}%" if price else "N/A",
                "turnover": f"{turnover:.2f}%" if turnover else "-",
                "amount": f"{amount:.2f}" if amount else "-",
                "amount_change": amt_change,
                "vol_ratio": vol_ratio,
                "tech_summary": tech_summary,
            })

    # ---- 4. 获取市场新闻 (同花顺) ----
    try:
        news = get_market_news()
    except Exception:
        news = []

    # ---- 5. 构建推送消息 ----
    md = build_dingtalk_md(alerts, normal, index_quotes, news)

    title = "⚠️ 自选股盘中预警" if alerts else "📊 自选股盘中扫描"

    try:
        result = send_markdown(title, md)
        print(f"钉钉推送结果: {result}")
        if result.get("errcode") != 0:
            print(f"钉钉推送失败: {result}")
    except Exception as e:
        print(f"钉钉推送异常: {e}")

    print(md[:500])
    return {
        "status": "ok",
        "alerts": len(alerts),
        "msg": f"发现 {len(alerts)} 只股票触发预警",
    }


def build_dingtalk_md(alerts, normal, index_quotes, news):
    """构建钉钉Markdown消息。"""
    md = f"### {'⚠️ 自选股盘中预警' if alerts else '📊 自选股盘中扫描'}\n"
    md += f"**时间：** {now_str()}\n\n---\n\n"

    # ---- 指数概况 ----
    md += "**大盘指数：**\n"
    for idx in INDICES[:4]:  # 只显示前4个指数
        sym = idx["sina"].replace("s_", "")
        q = index_quotes.get(sym)
        if q and q["price"] > 0:
            emoji = "🔴" if q["pct"] > 0 else ("🟢" if q["pct"] < 0 else "⚪")
            md += f"- {emoji} {idx['name']}: {q['price']:.2f} ({q['pct']:+.2f}%)"
            if q["amount"] > 0:
                md += f" 成交{q['amount']:.0f}亿"
            md += "\n"
    md += "\n"

    # ---- 预警股票详情 ----
    if alerts:
        for a in alerts:
            emoji = "🔴" if a["change_pct"] < -5 else ("🟢" if a["change_pct"] > 5 else "🟡")
            market_str = "HK" if a["market"] == "HK" else a["market"].upper()
            md += f"#### {emoji} {a['name']}({a['code']}) — {market_str}\n"
            price_sym = "HK$" if a["market"] == "HK" else "¥"
            md += f"- **当前价：** {price_sym}{a['price']:.2f}\n"
            md += f"- **涨跌幅：** {a['change_pct']:+.2f}%\n"
            if a["turnover"] > 0:
                md += f"- **换手率：** {a['turnover']:.2f}%\n"
            if a["amount"] > 0:
                md += f"- **成交额：** {a['amount']:.2f}亿"
                if a["amount_change"] is not None:
                    direction = "放大" if a["amount_change"] > 0 else "缩小"
                    md += f" (较前日{direction}{abs(a['amount_change']):.1f}%)"
                md += "\n"
            for r in a["reasons"]:
                md += f"- **触发：** {r}\n"
            if a["tech_summary"]:
                md += f"- **技术分析：** {a['tech_summary']}\n"
            md += "\n"

    # ---- 正常股票表格 ----
    if normal:
        if alerts:
            md += "---\n\n#### 📊 其余自选股表现\n\n"
        else:
            md += "✅ **无异常信号** — 全部自选股运行正常\n\n"

        md += "| 股票 | 价格 | 涨跌幅 | 换手率 | 成交额(亿) | 较前日 |\n"
        md += "|------|------|--------|--------|-----------|--------|\n"
        for n in normal:
            amt_chg_str = "-"
            if n.get("amount_change") is not None:
                direction = "+" if n["amount_change"] > 0 else ""
                amt_chg_str = f"{direction}{n['amount_change']:.1f}%"
            md += f"| {n['name']}({n['code']}) | {n['price']} | {n['pct']} | {n['turnover']} | {n['amount']} | {amt_chg_str} |\n"

    # ---- 新闻 ----
    if news:
        md += "\n---\n\n**📰 要闻速览：**\n"
        for n in news[:5]:
            title = n.get("title", "")
            time_str = n.get("time", "")
            if time_str:
                md += f"- [{time_str}] {title}\n"
            else:
                md += f"- {title}\n"

    return md


# 直接运行入口
if __name__ == "__main__":
    run_monitor()
