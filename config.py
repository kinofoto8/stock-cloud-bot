"""
共享配置 — 自选股列表、钉钉 Webhook、关注指数等
支持从环境变量读取敏感信息（GitHub Actions Secrets），本地回退到硬编码值。
"""
import os

# ============================================================
# 钉钉机器人配置（优先环境变量，兼容本地硬编码）
# ============================================================
DINGTALK_WEBHOOK = os.environ.get(
    "DINGTALK_WEBHOOK",
    "https://oapi.dingtalk.com/robot/send"
    "?access_token=2464ae3639ee0ebf737361a6a24e9c69bcd2ed5bb023730799520bbaede1cc41",
)
DINGTALK_SECRET = os.environ.get(
    "DINGTALK_SECRET",
    "SEC63372b026d2692228eecf6dca2b1079206c8819ecba91dd3282408e2c6ffc373",
)

# ============================================================
# 自选股列表（A股 + 港股）
# ============================================================
WATCHLIST_A = [
    {"code": "601899", "name": "紫金矿业", "market": "sh"},
    {"code": "000426", "name": "兴业银锡", "market": "sz"},
    {"code": "600489", "name": "中金黄金", "market": "sh"},
    {"code": "000408", "name": "藏格矿业", "market": "sz"},
    {"code": "600331", "name": "宏达股份", "market": "sh"},
    {"code": "002240", "name": "盛新锂能", "market": "sz"},
    {"code": "588170", "name": "科创半导体ETF华夏", "market": "sh"},
    {"code": "600988", "name": "赤峰黄金", "market": "sh"},
    {"code": "000807", "name": "云铝股份", "market": "sz"},
    {"code": "000933", "name": "神火股份", "market": "sz"},
]

WATCHLIST_HK = [
    {"code": "00883", "name": "中国海洋石油", "market": "hk"},
    {"code": "09992", "name": "泡泡玛特", "market": "hk"},
    {"code": "02259", "name": "紫金黄金国际", "market": "hk"},
]

# ============================================================
# 关注指数列表
# ============================================================
WATCH_INDICES = [
    {"code": "000001", "name": "上证指数"},
    {"code": "399001", "name": "深证成指"},
    {"code": "399006", "name": "创业板指"},
    {"code": "000688", "name": "科创50"},
    {"code": "000300", "name": "沪深300"},
    {"code": "000905", "name": "中证500"},
    {"code": "000852", "name": "中证1000"},
]

# ============================================================
# 预警阈值
# ============================================================
ALERT_THRESHOLDS = {
    "change_pct_abs": 5.0,       # 涨跌幅绝对值 ≥ 5%
    "chg_5d_abs": 15.0,          # 5日涨跌幅绝对值 ≥ 15%
    "chg_20d_abs": 25.0,         # 20日涨跌幅绝对值 ≥ 25%
    "volume_ratio": 2.0,         # 量比 ≥ 2
    "turnover_rate": 8.0,        # 换手率 ≥ 8%
    "rsi_overbought": 70,        # RSI(6) > 70
    "kdj_j_overbought": 80,     # KDJ J值 > 80
    "fund_outflow_pct": 0.20,   # 主力流出 / 成交额 > 20%
}
