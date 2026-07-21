"""
钉钉机器人 Webhook 推送模块
支持 text 和 markdown 两种消息类型，含加签安全设置。
"""
import time
import hmac
import hashlib
import base64
import json
import urllib.request
import urllib.parse
from config import DINGTALK_WEBHOOK, DINGTALK_SECRET


def _sign() -> str:
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{DINGTALK_SECRET}"
    hmac_code = hmac.new(
        DINGTALK_SECRET.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{DINGTALK_WEBHOOK}&timestamp={timestamp}&sign={sign}"


def send_markdown(title: str, text: str) -> dict:
    """发送 Markdown 消息到钉钉群。"""
    url = _sign()
    message = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    req = urllib.request.Request(
        url,
        data=json.dumps(message).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_text(content: str) -> dict:
    """发送纯文本消息到钉钉群。"""
    url = _sign()
    message = {"msgtype": "text", "text": {"content": content}}
    req = urllib.request.Request(
        url,
        data=json.dumps(message).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))
