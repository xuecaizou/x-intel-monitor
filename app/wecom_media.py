"""企微群机器人文件两步法：upload_media 取 media_id → file 消息发送。

reports.push_task 的附件推送依赖本模块（此前缺失导致附件推送必然失败且被 +FAIL 掩盖）。
大小限制 20MB（企微接口上限）；仅使用传入 webhook，不落盘、不打日志。
"""
import os

import requests

UPLOAD_LIMIT = 20 * 1024 * 1024


def push_file(webhook, path):
    """上传并推送文件；返回企微响应 dict（errcode==0 为成功）"""
    size = os.path.getsize(path)
    if size > UPLOAD_LIMIT:
        return {"errcode": 90001, "errmsg": f"文件 {size} 字节超过企微 20MB 上限，未上传"}
    key = webhook.split("key=")[-1]
    up = (f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media"
          f"?key={key}&type=file")
    with open(path, "rb") as f:
        r = requests.post(up, files={"media": (os.path.basename(path), f)}, timeout=60)
    d = r.json()
    if d.get("errcode") != 0:
        return d
    media_id = d.get("media_id")
    if not media_id:
        return {"errcode": -1, "errmsg": "upload_media 未返回 media_id"}
    r2 = requests.post(webhook, json={"msgtype": "file", "file": {"media_id": media_id}},
                       timeout=30)
    return r2.json()
