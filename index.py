"""
腾讯云函数 SCF 入口文件

部署为两个云函数，通过不同 Handler 区分：
- 盘中监控：handler = index.monitor_handler
- 收盘复盘：handler = index.review_handler
"""

from market_monitor import run_monitor
from market_review import run_review


def monitor_handler(event, context):
    """盘中监控入口 — 定时触发"""
    result = run_monitor()
    return result


def review_handler(event, context):
    """收盘复盘入口 — 定时触发"""
    result = run_review()
    return result
