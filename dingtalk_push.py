"""
钉钉机器人 Webhook 推送模块
支持 text 和 markdown 两种消息类型，含加签安全设置。
含消息长度安全检查和失败重试。
"""
import time
import hmac
import hashlib
import base64
import json
import os
import urllib.request
import urllib.parse
import urllib.error

# 优先从环境变量读取（GitHub Actions Secrets），本地回退到 config.py 硬编码值
_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK")
_SECRET = os.environ.get("DINGTALK_SECRET")

if not _WEBHOOK or not _SECRET:
    try:
        from config import DINGTALK_WEBHOOK, DINGTALK_SECRET
        _WEBHOOK = _WEBHOOK or DINGTALK_WEBHOOK
        _SECRET = _SECRET or DINGTALK_SECRET
    except ImportError:
        pass

# 钉钉 markdown 消息上限 5000 字符，留 200 字符余量
MAX_MARKDOWN_LEN = 4800
MAX_RETRY = 3
RETRY_INTERVAL = 5  # 秒


def _sign() -> str:
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{_SECRET}"
    hmac_code = hmac.new(
        _SECRET.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{_WEBHOOK}&timestamp={timestamp}&sign={sign}"


def _truncate_markdown(text: str, report_url: str = "") -> str:
    """截断 markdown 消息到安全长度，保留结尾标记。"""
    if len(text) <= MAX_MARKDOWN_LEN:
        return text
    # 从段落边界截断
    cut = text[:MAX_MARKDOWN_LEN]
    last_sep = cut.rfind("\n---\n")
    if last_sep > MAX_MARKDOWN_LEN // 2:
        cut = cut[:last_sep]
    url_hint = f"\n\n[查看完整复盘报告]({report_url})" if report_url else ""
    return cut + f"\n\n---\n\n*注：消息超长已截断，完整报告请查看 GitHub Pages。*{url_hint}"


def send_markdown(title: str, text: str, report_url: str = "") -> dict:
    """发送 Markdown 消息到钉钉群。含长度检查和失败重试。"""
    text = _truncate_markdown(text, report_url)
    url = _sign()
    message = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    payload = json.dumps(message).encode("utf-8")

    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("errcode") == 0:
                    return result
                # 钉钉返回错误（如频率限制）
                last_err = f"DingTalk error: {result}"
                print(f"  [dingtalk] attempt {attempt} failed: {result}")
            time.sleep(RETRY_INTERVAL * attempt)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            last_err = str(e)
            print(f"  [dingtalk] attempt {attempt} network error: {e}")
            time.sleep(RETRY_INTERVAL * attempt)

    print(f"  [dingtalk] all {MAX_RETRY} attempts failed: {last_err}")
    return {"errcode": -1, "errmsg": str(last_err)}


def send_text(content: str) -> dict:
    """发送纯文本消息到钉钉群。含失败重试。"""
    url = _sign()
    message = {"msgtype": "text", "text": {"content": content[:4800]}}
    payload = json.dumps(message).encode("utf-8")

    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("errcode") == 0:
                    return result
                last_err = f"DingTalk error: {result}"
            time.sleep(RETRY_INTERVAL * attempt)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            last_err = str(e)
            time.sleep(RETRY_INTERVAL * attempt)

    return {"errcode": -1, "errmsg": str(last_err)}
