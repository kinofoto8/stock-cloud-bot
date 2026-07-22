"""
自选股盘中监控 v3 — 纯腾讯API架构 (不依赖akshare)
每个交易日 10:30, 14:30 执行
对 13 只自选股进行多维度扫描，发现预警信号则推送到钉钉。

v3 改进 (对齐本地自动化风格):
  1. 预警格式: 高危/关注 分类 + 触发原因摘要 + 操作建议
  2. 移除要闻速览栏目
  3. 新增综合研判栏目
  4. 资金流向增强: 3日+20日主力净流出检测
"""
import sys
import os
import requests
from datetime import datetime, time as dtime

# 从 report_builder 导入腾讯API函数和技术分析工具
from report_builder import (
    fetch_tencent_quotes, get_kline_tencent, get_kline_tencent_hk,
    analyze_technicals, generate_tech_summary, _calc_amount_change_pct,
    WATCHLIST, INDICES, safe_float, SESSION,
)
from config import ALERT_THRESHOLDS
from dingtalk_push import send_markdown


def now_str():
    return datetime.now().strftime("%m月%d日 %H:%M")


# ---- 预警等级判定 ----
def _classify_alert_level(pct, reasons, tech_data, fund_summary):
    """判定预警等级：高危 / 关注。"""
    score = 0
    pct_abs = abs(pct)

    if pct_abs >= 9:
        score += 3
    elif pct_abs >= 7:
        score += 2
    elif pct_abs >= 5:
        score += 1

    for r in reasons:
        if "涨停" in r or "跌停" in r:
            score += 3
        if "量比" in r and ("2.5" in r or "3" in r or "4" in r or "5" in r):
            score += 1

    if tech_data:
        kdj_j = tech_data.get("kdj_j", 0) or 0
        rsi6 = tech_data.get("rsi6", 0) or 0
        chg_5d = tech_data.get("chg_5d")
        chg_20d = tech_data.get("chg_20d")
        if kdj_j > 100:
            score += 2
        if rsi6 and rsi6 > 80:
            score += 2
        if chg_5d is not None and abs(chg_5d) >= 15:
            score += 1
        if chg_20d is not None and abs(chg_20d) >= 30:
            score += 2
        if chg_20d is not None and chg_20d <= -25:
            score += 2

    if fund_summary:
        if fund_summary.get("outflow_3d"):
            score += 2
        if fund_summary.get("outflow_20d"):
            score += 1

    return "高危" if score >= 3 else "关注"


# ---- 操作建议生成 ----
def _generate_suggestion(name, reasons, pct, tech_data, fund_summary):
    """根据预警原因生成操作建议。"""
    kdj_j = (tech_data.get("kdj_j", 0) or 0) if tech_data else 0
    rsi6 = (tech_data.get("rsi6", 0) or 0) if tech_data else 0
    has_limit = any("涨停" in r for r in reasons)
    has_overbought = kdj_j > 90 or (rsi6 and rsi6 > 70)
    has_death_cross = any("死叉" in r for r in reasons)
    has_outflow = any("出逃" in r or "流出" in r for r in reasons)
    has_breakout = any("突破上轨" in r for r in reasons)
    all_reasons = " ".join(reasons)

    if has_limit and has_overbought:
        return "短线严重超买，建议逢高减仓或设置止盈。"
    if pct >= 7 and has_overbought:
        if "利好" in all_reasons or "业绩" in all_reasons:
            return "利好兑现后超买明显，不建议追高，考虑分批止盈。"
        return "超买严重，短线止盈。"
    if pct >= 7:
        if "利好" in all_reasons:
            return "消息复杂，短线冲高后可部分止盈。"
        return "短线涨幅过大，警惕冲高回落，止盈为主。"
    if has_overbought and has_breakout:
        return "业绩预增已反映，超买+突破上轨，建议逢高减仓。"
    if has_overbought:
        if "减持" in all_reasons:
            return "注意减持压力，短线不追高。"
        return "短线偏热，谨慎追高。"
    if has_death_cross and has_outflow:
        return "趋势弱势，利空未消化，减仓或规避。"
    if has_death_cross:
        return "趋势走弱，观望为主。"
    if has_outflow:
        return "多空消息交织，关注后续资金流向。"
    if "换手率" in all_reasons:
        return "高换手但方向不明，观望或减仓。"
    return "密切关注后续走势。"


# ---- 综合研判生成 ----
def _generate_comprehensive_analysis(alerts, normal, index_quotes):
    """根据当日扫描结果生成丰富的内容综合研判。"""
    now_hour = datetime.now().hour
    all_stocks = alerts + normal
    high_risk = [a for a in alerts if a.get("severity") == "高危"]
    attention = [a for a in alerts if a.get("severity") == "关注"]

    paragraphs = []

    # ==== 段落1：大盘环境 ====
    p1_parts = []
    sh = index_quotes.get("sh000001")
    sz = index_quotes.get("sz399001")
    hsi = index_quotes.get("hkHIS")

    if sh and sh["price"] > 0:
        direction = "上涨" if sh["pct"] > 0 else ("下跌" if sh["pct"] < 0 else "平盘")
        emoji = "🔴" if sh["pct"] > 0 else ("🟢" if sh["pct"] < 0 else "⚪")
        strength = "强势" if sh["pct"] > 1 else ("弱势" if sh["pct"] < -1 else "震荡")
        p1_parts.append(f"今日A股大盘{strength}运行，上证指数报{sh['price']:.0f}点（{sh['pct']:+.2f}%）")
        if sz and sz["price"] > 0:
            p1_parts.append(f"深证成指报{sz['price']:.0f}点（{sz['pct']:+.2f}%）")
    if hsi and hsi["price"] > 0:
        p1_parts.append(f"恒生指数报{hsi['price']:.0f}点（{hsi['pct']:+.2f}%）")

    # 自选股涨跌统计
    up_count = sum(1 for a in all_stocks if isinstance(a.get("change_pct"), (int, float)) and a["change_pct"] > 0)
    down_count = sum(1 for a in all_stocks if isinstance(a.get("change_pct"), (int, float)) and a["change_pct"] < 0)
    limit_up = [a for a in alerts if a["change_pct"] >= 9.5]
    if limit_up:
        names = "、".join([a["name"] for a in limit_up])
        p1_parts.append(f"自选股中{limit_up[0]['name']}等触及或接近涨停")
    if up_count + down_count > 0:
        p1_parts.append(f"自选股{up_count}涨{down_count}跌，涨跌比{up_count}:{down_count}")

    if p1_parts:
        paragraphs.append("**一、大盘环境**\n" + "；".join(p1_parts) + "。")

    # ==== 段落2：强势板块/个股分析 ====
    strong_stocks = [a for a in alerts if a["change_pct"] >= 3]  # 日涨超3%的
    if strong_stocks:
        p2_parts = []
        # 分类：贵金属/有色类 vs 其他
        gold_metal_names = ["紫金矿业", "赤峰黄金", "中金黄金", "兴业银锡", "藏格矿业",
                            "宏达股份", "紫金黄金国际", "云铝股份", "神火股份"]
        gold_stocks = [a for a in strong_stocks if a["name"] in gold_metal_names]
        other_strong = [a for a in strong_stocks if a["name"] not in gold_metal_names]

        if gold_stocks:
            max_stock = max(gold_stocks, key=lambda x: x["change_pct"])
            gold_pcts = [a["change_pct"] for a in gold_stocks]
            avg_pct = sum(gold_pcts) / len(gold_pcts)
            p2_parts.append(f"有色/贵金属板块集体爆发：{len(gold_stocks)}只有色/贵金属股全线上涨")
            p2_parts.append(f"平均涨幅{avg_pct:+.1f}%，其中{max_stock['name']}领涨{max_stock['change_pct']:+.2f}%")

            # 分析触发信号
            overbought_gold = [a for a in gold_stocks if any("超买" in r for r in a["reasons"])]
            if overbought_gold:
                names = "、".join([a["name"] for a in overbought_gold[:4]])
                max_j = max([a.get("tech_data", {}).get("kdj_j", 0) or 0 for a in overbought_gold])
                p2_parts.append(f"{names}等同步触发KDJ/RSI超买（J值最高{max_j:.0f}）并突破布林上轨，短线面临技术性回调压力")

            # 资金面
            inflow_gold = [a for a in gold_stocks if any("净流入" in r for r in a["reasons"])]
            if inflow_gold:
                p2_parts.append(f"部分品种获主力资金大幅净流入，机构对贵金属板块短期情绪高涨")

        if other_strong:
            for a in other_strong:
                p2_parts.append(f"{a['name']}{a['change_pct']:+.2f}%，但需注意其独立催化因素及持续性")

        paragraphs.append("**二、强势品种分析**\n" + "；".join(p2_parts) + "。")

    # ==== 段落3：弱势/风险品种分析 ====
    weak_conditions = ["死叉", "持续流出", "跌停", "暴跌", "减持", "定增"]
    risk_stocks = [a for a in alerts if any(any(kw in r for kw in weak_conditions) for r in a["reasons"])]
    if risk_stocks:
        p3_parts = []
        for a in risk_stocks:
            td = a.get("tech_data", {}) or {}
            chg_20d = td.get("chg_20d")
            reasons_str = "；".join([r for r in a["reasons"]
                                     if any(kw in r for kw in weak_conditions)])
            detail = f"{a['name']}（{a['change_pct']:+.2f}%）"
            if chg_20d is not None:
                detail += f"，20日累计{chg_20d:+.2f}%"
            if reasons_str:
                detail += f"：{reasons_str[:80]}"
            p3_parts.append(detail)

        paragraphs.append("**三、风险提示品种**\n" + "\n- " + "\n- ".join(p3_parts))

    # ==== 段落4：港股权重分析 ====
    hk_stocks = [a for a in alerts if a.get("is_hk") or a["market"] == "HK"]
    if hk_stocks:
        p4_parts = []
        for a in hk_stocks:
            td = a.get("tech_data", {}) or {}
            kdj_j = td.get("kdj_j")
            detail = f"{a['name']}（{a['change_pct']:+.2f}%）"
            if kdj_j and kdj_j > 80:
                detail += f" J值={kdj_j:.1f}超买"
            # 南向/北向相关
            south_reasons = [r for r in a["reasons"] if "南向" in r]
            if south_reasons:
                detail += f"，{south_reasons[0]}"
            p4_parts.append(detail)

        paragraphs.append("**四、港股权重表现**\n" + "；".join(p4_parts) + "。")

    # ==== 段落5：综合操作建议 ====
    p5_parts = []
    # 高危过半数 → 风险警示
    if len(high_risk) >= len(alerts) * 0.5 and len(alerts) > 0:
        p5_parts.append("目前过半预警为高危级别，整体短线风险偏高")
        p5_parts.append("对涨停/暴涨品种建议分批止盈锁定利润，避免追高")
        p5_parts.append("对弱势品种继续控制仓位")

    # 超买大面积
    overbought_count = sum(1 for a in alerts if any("超买" in r for r in a["reasons"]))
    if overbought_count >= 5:
        p5_parts.append(f"大面积超买（{overbought_count}只），短线追高风险极大，耐心等待回调再择机介入")
    elif overbought_count >= 2:
        p5_parts.append(f"{overbought_count}只超买，短线偏热，追高需谨慎")

    # 死叉个股
    death_cross_count = sum(1 for a in alerts if any("死叉" in r for r in a["reasons"]))
    if death_cross_count >= 1:
        p5_parts.append(f"{death_cross_count}只MACD死叉品种趋势未扭转，不建议左侧抄底")

    # 出逃品种
    outflow_stocks = [a for a in alerts if any("流出" in r or "出逃" in r for r in a["reasons"])]
    if outflow_stocks:
        names = "、".join([a["name"] for a in outflow_stocks[:2]])
        p5_parts.append(f"{names}资金面持续偏空，需等待资金回流信号再考虑")

    if p5_parts:
        paragraphs.append("**五、操作建议**\n" + "；".join(p5_parts) + "。")

    # ==== 段落6：后市关注 ====
    p6_parts = []
    if now_hour >= 15:
        p6_parts.append("今晚重点关注美股三大指数及COMEX黄金/白银期货走势，有色板块受海外大宗商品联动明显")
        p6_parts.append("关注美元指数及美联储政策预期变化对贵金属价格的影响")
    else:
        p6_parts.append("午后关注大盘能否维持强势、以及有色金属期货价格走势")
        hk_trading = any(a.get("is_hk") or a["market"] == "HK" for a in alerts)
        if hk_trading:
            p6_parts.append("港股尾盘走势对A股相关品种有先行指标作用")
        p6_parts.append("建议结合晚间COMEX黄金期货收盘价判断明日贵金属板块持续性")

    paragraphs.append("**六、后市关注**\n" + "；".join(p6_parts) + "。")

    if not paragraphs:
        return "今日自选股整体运行平稳，未触发预警信号。各品种技术指标正常，可继续按原有策略操作。建议关注晚间消息面及次日开盘走势。"

    return "\n\n".join(paragraphs)


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

        # ---- 3e. 资金流向 (EM API, 仅A股) ----
        fund_summary = {}
        if in_session and not is_hk and secid:
            try:
                flows_3d = get_fund_flow_em(secid, n=3)
                flows_20d = get_fund_flow_em(secid, n=20)
                if flows_3d:
                    latest = flows_3d[-1]
                    main_net = latest["main_net"]
                    # 主力流出且占比超过阈值
                    if main_net < 0 and amount > 0:
                        outflow_pct = abs(main_net) / (amount * 1e8)
                        if outflow_pct > ALERT_THRESHOLDS["fund_outflow_pct"]:
                            stock_alerts.append(
                                f"主力资金出逃: 净流出 {main_net/1e8:.2f}亿, 占比 {outflow_pct*100:.1f}%"
                            )

                    # 主力大幅流入也记录（用于建议）
                    if main_net > 0 and amount > 0:
                        inflow_pct = main_net / (amount * 1e8)
                        if inflow_pct > 0.15:
                            stock_alerts.append(
                                f"主力资金净流入 {main_net/1e8:.2f}亿（占成交额{inflow_pct*100:.1f}%）"
                            )

                    # 连续3日主力流出
                    if len(flows_3d) >= 3 and all(f["main_net"] < 0 for f in flows_3d):
                        total_out = sum(f["main_net"] for f in flows_3d)
                        stock_alerts.append(
                            f"连续3日主力净流出: 累计 {total_out/1e8:.2f}亿"
                        )
                        fund_summary["outflow_3d"] = total_out

                # 20日资金流向
                if flows_20d:
                    out_20d = [f for f in flows_20d if f["main_net"] < 0]
                    if len(out_20d) >= 15:
                        total_20d = sum(f["main_net"] for f in flows_20d)
                        if total_20d < 0:
                            stock_alerts.append(
                                f"20日主力资金持续净流出: 累计 {total_20d/1e8:.1f}亿"
                            )
                            fund_summary["outflow_20d"] = total_20d
            except Exception:
                pass  # 资金流向非核心，静默失败

        if stock_alerts:
            severity = _classify_alert_level(pct, stock_alerts, tech_data, fund_summary)
            suggestion = _generate_suggestion(name, stock_alerts, pct, tech_data, fund_summary)
            alerts.append({
                "name": name, "code": code, "market": market,
                "price": price, "change_pct": pct,
                "turnover": turnover, "amount": amount,
                "amount_change": amt_change,
                "vol_ratio": vol_ratio,
                "tech_summary": tech_summary,
                "reasons": stock_alerts,
                "severity": severity,
                "suggestion": suggestion,
                "tech_data": tech_data,
                "fund_summary": fund_summary,
                "is_hk": is_hk,
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

    # ---- 4. 构建推送消息 ----
    md = build_dingtalk_md(alerts, normal, index_quotes)

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


def build_dingtalk_md(alerts, normal, index_quotes):
    """构建钉钉Markdown消息 — v3风格（高危/关注分类+操作建议+综合研判）。"""
    md = f"### {'⚠️ 自选股盘中预警' if alerts else '📊 自选股盘中扫描'}\n"
    md += f"**时间：** {now_str()}\n\n---\n\n"

    # ---- 指数概况 ----
    md += "**大盘指数：**\n"
    for idx in INDICES[:4]:
        sym = idx["sina"].replace("s_", "")
        q = index_quotes.get(sym)
        if q and q["price"] > 0:
            emoji = "🔴" if q["pct"] > 0 else ("🟢" if q["pct"] < 0 else "⚪")
            md += f"- {emoji} {idx['name']}: {q['price']:.2f} ({q['pct']:+.2f}%)"
            if q["amount"] > 0:
                md += f" 成交{q['amount']:.0f}亿"
            md += "\n"
    md += "\n"

    # ---- 分为高危和关注两组 ----
    high_risk = [a for a in alerts if a.get("severity") == "高危"]
    attention = [a for a in alerts if a.get("severity") == "关注"]

    def _fmt_stock_detail(a, emoji):
        """格式化单只预警股票详情。"""
        price_sym = "HK$" if a.get("is_hk") or a["market"] == "HK" else "¥"
        code_fmt = f"{a['market']}{a['code']}" if a["market"] == "HK" else (
            f"sh{a['code']}" if a["market"] in ("sh", "A") else f"sz{a['code']}"
        ) if a["market"] not in ("sh", "sz") else f"{a['market']}{a['code']}"

        # 简化市场标签
        mkt_label = "HK" if a.get("is_hk") or a["market"] == "HK" else ""

        # 构建触发原因摘要
        reason_parts = []
        # 涨跌幅
        pct = a["change_pct"]
        if abs(pct) >= 9:
            reason_parts.append(f"日涨{pct:+.2f}%{'触及涨停' if pct >= 9.9 else ''}")
        elif abs(pct) >= 5:
            reason_parts.append(f"日涨{pct:+.2f}%")

        # 5日/20日
        td = a.get("tech_data", {}) or {}
        chg_5d = td.get("chg_5d")
        chg_20d = td.get("chg_20d")
        if chg_5d is not None and abs(chg_5d) >= 10:
            reason_parts.append(f"5日累计{chg_5d:+.2f}%")
        if chg_20d is not None and abs(chg_20d) >= 20:
            reason_parts.append(f"20日累计{chg_20d:+.2f}%")

        # 量比
        if a.get("vol_ratio", 0) >= 2.0:
            reason_parts.append(f"量比{a['vol_ratio']:.1f}放量")

        # 换手率
        if a.get("turnover", 0) >= 8:
            reason_parts.append(f"换手率{a['turnover']:.1f}%极高")

        # KDJ
        kdj_j = td.get("kdj_j")
        if kdj_j is not None and kdj_j > 80:
            reason_parts.append(f"KDJ J={kdj_j:.1f}{'严重超买' if kdj_j>100 else '超买'}")

        # RSI
        rsi6 = td.get("rsi6")
        if rsi6 is not None and rsi6 > 70:
            reason_parts.append(f"RSI(6)={rsi6:.1f}{'严重超买' if rsi6>80 else '超买'}")

        # MACD
        macd_sig = td.get("macd_signal", "")
        if macd_sig and ("死叉" in macd_sig or "空头" in macd_sig):
            reason_parts.append(f"MACD{macd_sig}")

        # 布林
        boll_sig = td.get("boll_signal", "")
        if boll_sig:
            reason_parts.append(boll_sig)

        # 资金流向
        fund = a.get("fund_summary", {}) or {}
        if fund.get("outflow_3d"):
            total = fund["outflow_3d"]
            reason_parts.append(f"主力连续3日净流出{total/1e8:.1f}亿")
        if fund.get("outflow_20d"):
            total = fund["outflow_20d"]
            reason_parts.append(f"20日主力净流出{total/1e8:.1f}亿")

        # 主力流入
        inflow_reasons = [r for r in a["reasons"] if "净流入" in r]
        if inflow_reasons:
            reason_parts.append(inflow_reasons[0].replace("主力资金", "主力"))

        # 合并原因
        reason_str = "；".join(reason_parts) if reason_parts else "触发多项预警"

        detail = f"#### {emoji} {a['name']}({code_fmt})"
        if mkt_label:
            detail += f" — {mkt_label}"
        detail += f"｜{a['severity']}｜{reason_str}\n"
        detail += f"- **当前价：** {price_sym}{a['price']:.2f}\n"
        detail += f"- **涨跌幅：** {a['change_pct']:+.2f}%\n"
        detail += f"- **触发原因：** {reason_str}"

        # 添加其他原因的详情
        other_reasons = [r for r in a["reasons"]
                         if "连续3日" not in r and "20日主力" not in r
                         and "净流入" not in r and "净流出" not in r
                         and "出逃" not in r]
        # 找出资金/消息类原因追加
        extra = [r for r in a["reasons"] if r not in reason_str]
        for r in extra[:3]:
            if any(kw in r for kw in ["公告", "新闻", "减持", "增持", "南向", "主力"]):
                detail += f"。{r}"

        detail += f"。\n- **建议操作：** {a.get('suggestion', '密切关注后续走势。')}\n\n"
        return detail

    # ---- 高危预警 ----
    if high_risk:
        for a in high_risk:
            md += _fmt_stock_detail(a, "🔴")

    # ---- 关注预警 ----
    if attention:
        for a in attention:
            md += _fmt_stock_detail(a, "🟠")

    # ---- 正常股票表格 ----
    if normal:
        if alerts:
            md += "---\n\n#### 📊 其余表现平稳的股票\n\n"
        else:
            md += "✅ **无异常信号** — 全部自选股运行正常\n\n"

        md += "| 股票 | 当前价 | 涨跌幅 |\n"
        md += "|------|--------|--------|\n"
        for n in normal:
            md += f"| {n['name']}({n['code']}) | {n['price']} | {n['pct']} |\n"

    if not normal:
        md += "---\n\n#### 📊 其余表现平稳的股票\n\n"
        md += "| 股票 | 当前价 | 涨跌幅 |\n"
        md += "|------|--------|--------|\n"
        md += "| 无 | - | - |\n"
        md += f"\n*注：今日{len(alerts)}只自选股均触发不同程度的预警或关注信号。*\n"

    # ---- 综合研判 ----
    md += "\n---\n\n#### 📌 综合研判\n"
    md += _generate_comprehensive_analysis(alerts, normal, index_quotes)

    # 钉钉 markdown 不支持 emoji 和特殊 Unicode 符号，做清理
    md = _sanitize_dingtalk(md)

    return md


def _sanitize_dingtalk(text):
    """替换钉钉 markdown 不支持的 emoji 和特殊符号为纯文本。"""
    replacements = {
        '\U0001f534': '',        # 🔴
        '\U0001f7e2': '',        # 🟢
        '\U0001f7e0': '',        # 🟠
        '\U0001f7e1': '',        # 🟡
        '\U0001f535': '',        # 🔵
        '\u26aa': '',            # ⚪
        '\U0001f4ca': '',        # 📊
        '\U0001f4cc': '',        # 📌
        '\u2705': '',            # ✅
        '\u26a0\ufe0f': '',      # ⚠️
        '\u26a0': '',            # ⚠
        '\u2191': '升',          # ↑
        '\u2193': '降',          # ↓
        '\u2197': '+',           # ↗
        '\u2198': '-',           # ↘
        '\u25b2': '',            # ▲
        '\u25bc': '',            # ▼
        '\u00d7': 'x',           # ×
        '\u201c': '"',           # "
        '\u201d': '"',           # "
        '\u300c': '[',           # 「
        '\u300d': ']',           # 」
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


# 直接运行入口
if __name__ == "__main__":
    run_monitor()
