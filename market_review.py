"""
A 股每日收盘复盘 — 云函数版（v2）
生成 HTML 报告部署到 GitHub Pages，钉钉推送摘要 + 链接。
"""
import sys
from datetime import datetime, timezone, timedelta

from dingtalk_push import send_markdown
from report_builder import fetch_all_data, generate_report, get_report_url, build_summary_md


def is_trade_day() -> bool:
    """简单交易日判断（周一至周五）。"""
    beijing_tz = timezone(timedelta(hours=8))
    wd = datetime.now(beijing_tz).weekday()
    return wd < 5


def run_review():
    beijing_tz = timezone(timedelta(hours=8))
    today = datetime.now(beijing_tz).strftime("%Y-%m-%d")

    if not is_trade_day():
        print(f"[{today}] 今日非交易日，跳过复盘。")
        return {"status": "holiday", "msg": "今日休市"}

    print(f"[{today}] 开始收盘复盘 (v2)...")

    # 1. 获取所有数据
    print("=" * 50)
    print("阶段 1: 数据采集")
    print("=" * 50)
    all_data = fetch_all_data()

    # 2. 生成 HTML 报告
    print("\n" + "=" * 50)
    print("阶段 2: 生成报告")
    print("=" * 50)
    filepath = generate_report(all_data)
    report_url = get_report_url()

    # 3. 构建摘要并推送钉钉
    print("\n" + "=" * 50)
    print("阶段 3: 推送钉钉")
    print("=" * 50)
    summary_md = build_summary_md(all_data)
    try:
        result = send_markdown("A股收盘复盘简报", summary_md)
        print(f"  钉钉推送: errcode={result.get('errcode')}")
        if result.get("errcode") != 0:
            print(f"  [WARN] 推送异常: {result}")
    except Exception as e:
        print(f"  [WARN] 推送失败: {e}")

    print(f"\n{'='*50}")
    print(f"复盘完成!")
    print(f"  报告文件: {filepath}")
    print(f"  在线链接: {report_url}")
    print(f"{'='*50}")

    return {"status": "ok", "report_file": filepath, "report_url": report_url}


if __name__ == "__main__":
    run_review()
