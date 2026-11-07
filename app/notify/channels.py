"""四个推送通道：企业微信群机器人 / 钉钉机器人(加签) / PushPlus / WxPusher

send() 统一签名 send(cc, title, markdown, **extra)，各通道只取自己支持的能力参数，
其余忽略——调用方（engine/reports）按 caps 标签决定传什么，实现"按通道能力分发"：
- wecom:    markdown + image（image 消息 base64 直传）+ file（reports 附件两步法）
- dingtalk: markdown（加签）
- pushplus: markdown 或 html（富文本模板）
- wxpusher: markdown + url（卡片跳转链接）
"""
import base64
import hashlib
import hmac
import time
import urllib.parse

import requests

from .base import Channel


class WeComChannel(Channel):
    key, display = "wecom", "企业微信"
    caps = ("markdown", "image", "file")
    # 企微群机器人限频约 20 条/分钟（markdown+image=2 条/推），单条即时推送至少间隔 4s
    SEND_GAP = 4.0
    _last = 0.0

    def configured(self, cc):
        return bool(cc.get("webhook"))

    @classmethod
    def _pace(cls):
        gap = time.time() - cls._last
        if gap < cls.SEND_GAP:
            time.sleep(cls.SEND_GAP - gap)
        cls._last = time.time()

    @staticmethod
    def _post(webhook, payload):
        """单条消息发送，errcode 45009（限频）自动退避重试，最多 3 次"""
        for i in range(3):
            r = requests.post(webhook, json=payload, timeout=15)
            d = r.json()
            if d.get("errcode") == 45009:
                time.sleep(15 * (i + 1))  # 限频 → 15s/30s 退避后重试
                continue
            return d
        return d

    @staticmethod
    def _hint(d):
        """常见 errcode 附加处置提示，写进推送日志便于自查"""
        code = str(d.get("errcode", ""))
        if code == "93000":
            return (" ⚠ 企微判定 webhook 无效：到企业微信对应群的 群设置→群机器人→查看更多，"
                    "重新复制目标机器人的完整 Webhook 地址后保存再测试（机器人须已加入群且未被删除）")
        if code == "45009":
            return " ⚠ 触发限频且重试后仍失败：稍后自动恢复，或减少该群推送量"
        return ""

    def send(self, cc, title, markdown, image=None, html=None, url=None, **_):
        """image = {"base64":…, "md5":…} 时，markdown 发出后紧随一条图片消息
        （企微机器人 markdown 不渲染图片，必须用 image 消息类型直传）"""
        self._pace()
        d = self._post(cc["webhook"], {
            "msgtype": "markdown", "markdown": {"content": markdown[:4096]}})
        ok = d.get("errcode") == 0
        detail = str(d)[:180] + self._hint(d)
        if ok and image:
            self._pace()
            d2 = self._post(cc["webhook"], {"msgtype": "image", "image": image})
            ok = ok and d2.get("errcode") == 0
            detail += " +图片:" + str(d2.get("errcode"))
        return ok, detail[:200]


class DingTalkChannel(Channel):
    key, display = "dingtalk", "钉钉"
    caps = ("markdown",)  # 机器人不支持文件/直传图片，附件信息以目录路径形式并入 markdown

    def configured(self, cc):
        return bool(cc.get("webhook"))

    def send(self, cc, title, markdown, image=None, html=None, url=None, **_):
        url_ = cc["webhook"]
        secret = cc.get("secret") or ""
        if secret:
            ts = str(round(time.time() * 1000))
            sign = base64.b64encode(hmac.new(
                secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest())
            url_ += f"&timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"
        r = requests.post(url_, json={
            "msgtype": "markdown", "markdown": {"title": title, "text": markdown[:18000]}}, timeout=10)
        d = r.json()
        return d.get("errcode") == 0, str(d)[:200]


class PushPlusChannel(Channel):
    key, display = "pushplus", "PushPlus"
    caps = ("markdown", "html")

    def configured(self, cc):
        return bool(cc.get("token"))

    def send(self, cc, title, markdown, image=None, html=None, url=None, **_):
        """传 html 时用富文本模板（报告摘要可带链接与加粗）"""
        body = {"token": cc["token"], "title": title[:100],
                "content": html if html else markdown,
                "template": "html" if html else "markdown"}
        r = requests.post("https://www.pushplus.plus/send", json=body, timeout=10)
        d = r.json()
        return d.get("code") == 200, str(d)[:200]


class WxPusherChannel(Channel):
    key, display = "wxpusher", "WxPusher"
    caps = ("markdown", "url")

    def configured(self, cc):
        return bool(cc.get("app_token"))

    def send(self, cc, title, markdown, image=None, html=None, url=None, **_):
        """url：消息卡片底部跳转链接（如报告线索包/内网监控台）"""
        payload = {"appToken": cc["app_token"], "uids": [cc["uid"]] if cc.get("uid") else [],
                   "summary": title[:99], "content": markdown, "contentType": 3}
        if url:
            payload["url"] = url
        r = requests.post("https://wxpusher.zjiecode.com/api/send/message", json=payload, timeout=10)
        d = r.json()
        return d.get("code") == 1000, str(d)[:200]


CHANNELS = [WeComChannel(), DingTalkChannel(), PushPlusChannel(), WxPusherChannel()]
