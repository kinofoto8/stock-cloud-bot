"""
A 股每日收盘复盘 — 云函数版
每个交易日 15:30 执行，生成全面复盘简报并推送到钉钉。
"""
from datetime import datetime

from config import WATCH_INDICES, WATCHLIST_A, WATCHLIST_HK
from dingtalk_push import send_markdown
from utils import (
    is_trade_day, get_index_spot, get_a_spot, get_hk_spot,
    get_industry_board, get_concept_board, get_hot_stocks,
    get_market_stats, get_market_news,
)


def now_date():
    today = datetime.now()
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    return f"{today.strftime('%Y-%m-%d')}（周{weekdays[today.weekday()]}）"


def format_pct(val):
    """格式化涨跌幅百分比。"""
    try:
        v = float(val)
        return f"{v:+.2f}%"
    except (ValueError, TypeError):
        return "N/A"


def format_amount(val, unit="亿"):
    """格式化金额。"""
    try:
        v = float(val)
        if unit == "亿":
            v = v / 1e8
        return f"{v:.2f}{unit}"
    except (ValueError, TypeError):
        return "N/A"


def run_review() -> dict:
    """主入口：执行收盘复盘。"""
    today = datetime.now().strftime("%Y-%m-%d")

    # ---- 1. 交易日检查 ----
    if not is_trade_day(today):
        print(f"[{today}] 今日休市，无需复盘。")
        return {"status": "holiday", "msg": "今日休市"}

    print(f"[{today}] 交易日确认，开始收盘复盘...")

    md_parts = []
    md_parts.append(f"### 📊 A股每日收盘复盘\n**日期：** {now_date()}\n\n---\n")

    # ---- 2. 市场总览 ----
    print("  获取市场总览...")
    stats = get_market_stats()
    if stats:
        md_parts.append("#### 一、大盘概览\n\n")
        md_parts.append(
            f"- 全市场：上涨 **{stats.get('up', 'N/A')}** 家 / "
            f"下跌 **{stats.get('down', 'N/A')}** 家 / "
            f"涨停 **{stats.get('limit_up', 'N/A')}** 家 / "
            f"跌停 **{stats.get('limit_down', 'N/A')}** 家\n"
        )
        md_parts.append(f"- 两市成交：**{stats.get('total_volume', 'N/A')}** 亿\n\n")

    # ---- 3. 指数行情 ----
    print("  获取指数行情...")
    idx = get_index_spot()
    idx_codes = [i["code"] for i in WATCH_INDICES]
    md_parts.append("#### 二、主要指数\n\n")
    md_parts.append("| 指数 | 收盘 | 涨跌幅 |\n|------|------|--------|\n")
    if not idx.empty:
        for ind in WATCH_INDICES:
            row = idx[idx["代码"] == ind["code"]]
            if not row.empty:
                r = row.iloc[0]
                price = float(r.get("最新价", 0) or 0)
                pct = float(r.get("涨跌幅", 0) or 0)
                md_parts.append(f"| {ind['name']} | {price:.2f} | {pct:+.2f}% |\n")
            else:
                md_parts.append(f"| {ind['name']} | N/A | N/A |\n")
    else:
        md_parts.append("| 数据获取失败 | - | - |\n")
    md_parts.append("\n")

    # ---- 4. 板块排名 ----
    print("  获取板块排名...")
    md_parts.append("#### 三、板块表现\n\n")

    # 申万一级行业
    try:
        industry = get_industry_board()
        if not industry.empty:
            # Sort by 涨跌幅 descending and ascending
            ind_sorted = industry.sort_values("涨跌幅", ascending=False)
            md_parts.append("**涨幅前 5（申万一级行业）：**\n")
            for i, (_, row) in enumerate(ind_sorted.head(5).iterrows()):
                name = row.get("板块名称", "N/A")
                pct = float(row.get("涨跌幅", 0) or 0)
                md_parts.append(f"{i+1}. {name} {pct:+.2f}%\n")

            md_parts.append("\n**跌幅前 5（申万一级行业）：**\n")
            for i, (_, row) in enumerate(ind_sorted.tail(5).iterrows()):
                name = row.get("板块名称", "N/A")
                pct = float(row.get("涨跌幅", 0) or 0)
                md_parts.append(f"{i+1}. {name} {pct:+.2f}%\n")
            md_parts.append("\n")
    except Exception as e:
        md_parts.append(f"行业板块数据获取失败: {e}\n\n")

    # 概念板块
    try:
        concept = get_concept_board()
        if not concept.empty:
            con_sorted = concept.sort_values("涨跌幅", ascending=False)
            md_parts.append("**热门概念板块 Top 5：**\n")
            for i, (_, row) in enumerate(con_sorted.head(5).iterrows()):
                name = row.get("板块名称", "N/A")
                pct = float(row.get("涨跌幅", 0) or 0)
                md_parts.append(f"{i+1}. {name} {pct:+.2f}%\n")
            md_parts.append("\n")
    except Exception:
        pass

    # ---- 5. 热门个股 ----
    print("  获取热门个股...")
    try:
        hot = get_hot_stocks(10)
        if not hot.empty:
            md_parts.append("**今日热门个股 Top 10：**\n\n")
            md_parts.append("| 排名 | 股票 | 最新价 | 涨跌幅 |\n|------|------|--------|--------|\n")
            for _, row in hot.iterrows():
                rank = row.get("排名", row.get("序号", "N/A"))
                code = row.get("代码", "")
                name = row.get("名称", "")
                price = row.get("最新价", "N/A")
                pct = row.get("涨跌幅", 0)
                try:
                    pct = float(pct)
                    pct_str = f"{pct:+.2f}%"
                except (ValueError, TypeError):
                    pct_str = "N/A"
                md_parts.append(f"| {rank} | {name}({code}) | {price} | {pct_str} |\n")
            md_parts.append("\n")
    except Exception:
        pass

    # ---- 6. 自选股收盘 ----
    print("  获取自选股收盘数据...")
    md_parts.append("#### 四、自选股表现\n\n")
    md_parts.append("| 股票 | 收盘价 | 涨跌幅 |\n|------|--------|--------|\n")

    a_codes = [s["code"] for s in WATCHLIST_A]
    hk_codes = [s["code"] for s in WATCHLIST_HK]

    spot_a = get_a_spot(a_codes)
    spot_hk = get_hk_spot(hk_codes)

    for stock in WATCHLIST_A + WATCHLIST_HK:
        code = stock["code"]
        name = stock["name"]
        if stock["market"] == "hk":
            spot_df = spot_hk
        else:
            spot_df = spot_a

        if not spot_df.empty:
            row = spot_df[spot_df["代码"] == code]
            if not row.empty:
                r = row.iloc[0]
                price = float(r.get("最新价", 0) or 0)
                pct = float(r.get("涨跌幅", 0) or 0)
                pct_str = f"{pct:+.2f}%"
                if abs(pct) >= 3:
                    pct_str = f"**{pct_str}**"
                md_parts.append(f"| {name}({code}) | {price:.2f} | {pct_str} |\n")
            else:
                md_parts.append(f"| {name}({code}) | N/A | N/A |\n")
        else:
            md_parts.append(f"| {name}({code}) | N/A | N/A |\n")
    md_parts.append("\n")

    # ---- 7. 市场新闻 ----
    print("  获取市场要闻...")
    md_parts.append("#### 五、今日要闻\n\n")
    try:
        news = get_market_news(10)
        if news:
            for i, n in enumerate(news):
                md_parts.append(f"{i+1}. {n['title']}\n")
        else:
            md_parts.append("暂无重要新闻。\n")
    except Exception:
        md_parts.append("新闻获取失败。\n")
    md_parts.append("\n")

    # ---- 8. 明日关注 ----
    md_parts.append("---\n\n#### 六、明日关注\n\n")
    md_parts.append("- 关注今日涨跌幅较大的板块持续性\n")
    md_parts.append("- 关注盘后公告和夜间外盘走势\n\n")
    md_parts.append("> 以上数据由云端自动化生成，仅供参考，不构成投资建议。\n")

    # 组装消息
    full_md = "".join(md_parts)

    # ---- 9. 推送到钉钉 ----
    try:
        result = send_markdown("A股收盘复盘简报", full_md)
        print(f"钉钉推送结果: {result}")
        if result.get("errcode") != 0:
            print(f"⚠️ 钉钉推送失败: {result}")
    except Exception as e:
        print(f"⚠️ 钉钉推送异常: {e}")

    print(full_md)
    return {"status": "ok", "msg": "复盘简报已生成并推送"}


if __name__ == "__main__":
    run_review()
