"""
自选股盘中监控 — 云函数版
每个交易日 9:30/10:30/11:30/13:30/14:30/15:30 执行
对 13 只自选股进行多维度扫描，发现预警信号则推送到钉钉。
"""
import sys
import os
from datetime import datetime, time as dtime
from io import StringIO

from config import (
    WATCHLIST_A, WATCHLIST_HK, ALERT_THRESHOLDS,
)
from dingtalk_push import send_markdown
from utils import (
    is_trade_day, get_a_spot, get_hk_spot,
    get_kline_a, get_kline_hk,
    calc_macd, calc_kdj, calc_rsi, calc_boll, calc_chg_n,
    get_fund_flow, get_last_n_fund_flows, get_stock_news,
)


def now_str():
    return datetime.now().strftime("%m月%d日 %H:%M")


def run_monitor() -> dict:
    """主入口：执行盘中监控。"""
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now()

    alerts = []       # 预警信号
    normal = []       # 正常股票
    errors = []       # 获取失败的股票

    # ---- 1. 交易日检查 ----
    if not is_trade_day(today):
        print(f"[{now_str()}] 今日休市，无需监控。")
        return {"status": "holiday", "msg": "今日休市"}

    print(f"[{now_str()}] 交易日确认，开始扫描 {len(WATCHLIST_A) + len(WATCHLIST_HK)} 只自选股...")

    # ---- 2. 获取实时行情 ----
    a_codes = [s["code"] for s in WATCHLIST_A]
    hk_codes = [s["code"] for s in WATCHLIST_HK]

    spot_a = get_a_spot(a_codes)
    spot_hk = get_hk_spot(hk_codes)

    # ---- 3. 逐只扫描 ----
    now_time = now.time()
    a_session = dtime(9, 30) <= now_time <= dtime(15, 0)
    hk_session = dtime(9, 30) <= now_time <= dtime(16, 0)

    all_stocks = WATCHLIST_A + WATCHLIST_HK

    for stock in all_stocks:
        code = stock["code"]
        name = stock["name"]
        market = stock["market"]

        # 判断是否在交易时段
        if market == "hk":
            in_session = hk_session
            spot_df = spot_hk
        else:
            in_session = a_session
            spot_df = spot_a

        # 获取行情
        if not spot_df.empty:
            row = spot_df[spot_df["代码"] == code]
            if row.empty:
                errors.append(f"{name}({code}): 未获取到行情")
                normal.append({"name": name, "code": code, "price": "N/A", "pct": "N/A"})
                continue
            row = row.iloc[0]
            price = float(row.get("最新价", 0) or 0)
            change_pct = float(row.get("涨跌幅", 0) or 0)
            volume_ratio = float(row.get("量比", 0) or 0)
            turnover = float(row.get("换手率", 0) or 0)
            volume_money = float(row.get("成交额", 0) or 0)
        else:
            price = 0
            change_pct = 0
            volume_ratio = 0
            turnover = 0
            volume_money = 0
            errors.append(f"{name}({code}): 行情接口失败")
            normal.append({"name": name, "code": code, "price": "N/A", "pct": "N/A"})
            continue

        stock_alerts = []

        # ---- 3a. 涨跌幅预警 ----
        if abs(change_pct) >= ALERT_THRESHOLDS["change_pct_abs"]:
            direction = "暴涨" if change_pct > 0 else "暴跌"
            stock_alerts.append(f"{direction}: 当日涨跌幅 {change_pct:+.2f}%")

        # ---- 3b. 量比 / 换手率预警 ----
        if volume_ratio >= ALERT_THRESHOLDS["volume_ratio"]:
            stock_alerts.append(f"放量: 量比 {volume_ratio:.1f}")
        if turnover >= ALERT_THRESHOLDS["turnover_rate"]:
            stock_alerts.append(f"高换手: 换手率 {turnover:.1f}%")

        # ---- 3c. 技术指标（仅在交易时段且有行情时检查） ----
        if in_session and price > 0:
            try:
                if market == "hk":
                    kline = get_kline_hk(code)
                else:
                    kline = get_kline_a(code)

                if not kline.empty and len(kline) >= 30:
                    close = kline["收盘"].astype(float)
                    high = kline["最高"].astype(float)
                    low = kline["最低"].astype(float)

                    # MACD
                    dif, dea, macd_hist = calc_macd(close)
                    if len(dif) >= 2 and len(dea) >= 2:
                        if (dif.iloc[-2] > dea.iloc[-2] and dif.iloc[-1] < dea.iloc[-1]) or \
                           (macd_hist.iloc[-2] > 0 and macd_hist.iloc[-1] < 0):
                            stock_alerts.append("MACD死叉信号")

                    # KDJ
                    k, d, j = calc_kdj(high, low, close)
                    if len(j) >= 2 and j.iloc[-1] > ALERT_THRESHOLDS["kdj_j_overbought"] and j.iloc[-1] < j.iloc[-2]:
                        stock_alerts.append(f"KDJ超买回落: J={j.iloc[-1]:.1f}")

                    # RSI
                    rsi = calc_rsi(close, 6)
                    if len(rsi) >= 1 and rsi.iloc[-1] > ALERT_THRESHOLDS["rsi_overbought"]:
                        stock_alerts.append(f"RSI超买: RSI(6)={rsi.iloc[-1]:.1f}")

                    # BOLL
                    upper, mid, lower = calc_boll(close)
                    if len(upper) >= 2 and len(close) >= 2:
                        if close.iloc[-2] > upper.iloc[-2] and close.iloc[-1] < upper.iloc[-1]:
                            stock_alerts.append("触及布林上轨后回落")

                    # 5日/20日涨跌幅
                    chg_5d = calc_chg_n(kline, 5)
                    chg_20d = calc_chg_n(kline, 20)
                    if chg_5d is not None and abs(chg_5d) >= ALERT_THRESHOLDS["chg_5d_abs"]:
                        stock_alerts.append(f"5日累计涨跌幅 {chg_5d:+.2f}%")
                    if chg_20d is not None and abs(chg_20d) >= ALERT_THRESHOLDS["chg_20d_abs"]:
                        stock_alerts.append(f"20日累计涨跌幅 {chg_20d:+.2f}%")
            except Exception as e:
                errors.append(f"{name}({code}): 技术指标计算失败 - {e}")

        # ---- 3d. 资金流向 ----
        if in_session and market in ("sh", "sz"):
            try:
                flow = get_fund_flow(code, market)
                if flow and flow["main_net"] is not None:
                    main_net = flow["main_net"]
                    if main_net < 0 and volume_money > 0:
                        outflow_pct = abs(main_net) / volume_money
                        if outflow_pct > ALERT_THRESHOLDS["fund_outflow_pct"]:
                            stock_alerts.append(
                                f"主力资金出逃: 净流出 {main_net/1e8:.2f}亿, 占比 {outflow_pct*100:.1f}%"
                            )

                # 连续 3 日主力流出
                flows = get_last_n_fund_flows(code, market, 3)
                if len(flows) >= 3 and all(f["main_net"] < 0 for f in flows):
                    total_out = sum(f["main_net"] for f in flows)
                    stock_alerts.append(
                        f"连续3日主力净流出: 累计 {total_out/1e8:.2f}亿"
                    )
            except Exception:
                pass  # 资金流向非核心，静默失败

        # ---- 3e. 新闻扫描 ----
        try:
            news = get_stock_news(code, limit=5)
            important_keywords = [
                "业绩预告", "业绩快报", "重组", "资产重组", "停牌", "复牌",
                "监管", "减持", "大宗交易", "增发", "配股", "退市", "处罚",
                "预亏", "亏损", "ST", "退市风险", "立案", "调查",
            ]
            for n in news:
                title = n["title"]
                for kw in important_keywords:
                    if kw in title:
                        stock_alerts.append(f"重要信息: {title}")
                        break
        except Exception:
            pass

        if stock_alerts:
            alerts.append({
                "name": name, "code": code, "market": market,
                "price": price, "change_pct": change_pct,
                "reasons": stock_alerts,
            })
        else:
            normal.append({
                "name": name, "code": code,
                "price": f"{price:.2f}" if price else "N/A",
                "pct": f"{change_pct:+.2f}%" if price else "N/A",
            })

    # ---- 4. 输出 & 推送 ----
    if not alerts:
        msg = f"{now_str()} 盘中扫描完成，{len(normal)}只自选股暂无异常信号。"
        print(msg)
        # 没有预警时不推钉钉，避免打扰
        return {"status": "ok", "alerts": 0, "msg": msg}

    # 有预警 —— 构建 Markdown 消息
    md = f"### ⚠️ 自选股盘中预警\n**时间：** {now_str()}\n\n---\n\n"

    for a in alerts:
        direction_emoji = "🔴" if a["change_pct"] < -3 else ("🟢" if a["change_pct"] > 3 else "🟡")
        md += f"#### {direction_emoji} {a['name']}({a['code']}) — {a['market'].upper()}\n"
        md += f"- **当前价：** ¥{a['price']:.2f}\n"
        md += f"- **涨跌幅：** {a['change_pct']:+.2f}%\n"
        for r in a["reasons"]:
            md += f"- **触发：** {r}\n"
        md += "\n"

    md += "---\n\n#### 📊 其余自选股表现\n\n"
    md += "| 股票 | 当前价 | 涨跌幅 |\n|------|--------|--------|\n"
    for n in normal:
        md += f"| {n['name']}({n['code']}) | {n['price']} | {n['pct']} |\n"

    if errors:
        md += "\n---\n\n#### ⚡ 数据获取异常\n\n"
        for e in errors:
            md += f"- {e}\n"

    # 推送到钉钉
    try:
        result = send_markdown("自选股盘中预警", md)
        print(f"钉钉推送结果: {result}")
        if result.get("errcode") != 0:
            print(f"⚠️ 钉钉推送失败: {result}")
    except Exception as e:
        print(f"⚠️ 钉钉推送异常: {e}")

    print(md)
    return {"status": "ok", "alerts": len(alerts), "msg": f"发现 {len(alerts)} 只股票触发预警"}


# 直接运行入口
if __name__ == "__main__":
    run_monitor()
