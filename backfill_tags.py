"""关键词标签回填脚本：为存量推文补 AI 关键词标签（tags 列），顺带补齐行业/单位/省州缺口。

设计（与 backfill_geo.py 同款工程约定）：
- 目标行：tags='[]'（未回填）且 ai_status='ok' 的推文；确实无可靠标签的行处理完保持 '[]'，
  重跑会被再处理一次（幂等无害，新推文由 analyze 链路实时打标，通常不进本脚本范围）
- 轻量 prompt：单条一次调用，max_tokens=350，只问 tags/industry/organizations/province/city，
  不重跑完整研判、不动 ai_* 研判结论（额度约为完整研判的 1/10）
- 行业/单位/省州/城市只在原值为空时回填（不覆盖已有结论）
- 自动提交模式：每条 UPDATE 独立短事务 + busy_timeout，可与在线服务并行
- 英文缩写要求模型写成「英文（中文说明）」格式，如 C2（命令与控制服务器）

用法：
  python backfill_tags.py            # 回填全部待处理行
  python backfill_tags.py --limit 50 # 只跑 50 条（试跑/分批）
"""
import json
import re
import sys
import time
from pathlib import Path

import sqlite3

sys.path.insert(0, str(Path(__file__).resolve().parent))

DB = r"D:\AI-work\黑客组织报告编写\x-intel-monitor\data\xintel.db"

PROMPT = (
    "你是威胁情报标注员。从以下推文提取情报要素，输出严格 JSON（值为中文，键固定）：\n"
    '{"tags":["关键词标签，最多10个，有多少提多少，按相关度排序：威胁手法（木马/勒索软件/数据泄露/钓鱼/'
    '漏洞利用/0day/供应链攻击）、技术要素（C2/僵尸网络/窃密/横向移动/载荷投递）、恶意软件家族名、'
    '威胁组织名、涉事企业或机构名、攻击者归属国家、受害者所在国家；'
    '英文术语或缩写必须写成「英文（中文说明）」格式，如 "C2（命令与控制服务器）"、"RAT（远程访问木马）"、'
    '"APT（高级持续性威胁）"；泛化空词（攻击/安全/漏洞）不要；没有可靠线索给空数组"],'
    '"industry":"涉事单位所属行业（医疗/金融/政府/能源/教育/科技/零售/交通/制造/电信/军工），'
    '无法判断填空字符串",'
    '"organizations":["涉事单位/受害机构全称，最多3个，没有则空数组"],'
    '"province":"省/州/邦级行政区中文标准名，未提及填空字符串",'
    '"city":"城市名，未提及填空字符串"}\n\n推文：\n{text}'
)


def _ai_cfg():
    rt = json.loads((Path(DB).parent / "runtime.json").read_text(encoding="utf-8"))["ai"]
    base = rt["base_url"].rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return rt, base, {"Authorization": "Bearer " + rt["api_key"]}


def _loose_json(text):
    """宽松解析：直接 loads → 抠 {...} 块 → 截断补括号"""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        frag = text[i:j + 1]
        try:
            return json.loads(frag)
        except Exception:
            while len(frag) > 2:
                try:
                    return json.loads(frag)
                except Exception:
                    frag = frag.rstrip().rstrip(",") + "}" if not frag.rstrip().endswith("}") \
                        else frag[:-1].rstrip() + "}"
    return {}


def _clean_str(v, n):
    if isinstance(v, list):
        v = "、".join(str(x) for x in v if str(x).strip())
    s = str(v or "").strip()
    if s in ("未知", "不详", "无", "N/A", "n/a", "none", "None", "null", "NULL"):
        return ""
    return s[:n]


def main():
    limit_n = 0
    if "--limit" in sys.argv:
        limit_n = int(sys.argv[sys.argv.index("--limit") + 1])

    conn = sqlite3.connect(DB, timeout=30)
    conn.isolation_level = None  # 自动提交：每条独立短事务，不锁在线服务
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")

    rows = conn.execute(
        "SELECT id, text, industry, org, geo_province, geo_city FROM tweets "
        "WHERE tags='[]' AND ai_status='ok' ORDER BY id DESC"
        + (f" LIMIT {int(limit_n)}" if limit_n else "")).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM tweets WHERE tags='[]' AND ai_status='ok'").fetchone()[0]
    print(f"待回填标签: {len(rows)} 条（库内共 {total} 条待处理）")
    if not rows:
        conn.close()
        return

    rt, base, hdr = _ai_cfg()
    import requests

    done = empty = fail = 0
    tag_counter = {}
    t0 = time.time()
    for idx, r in enumerate(rows, 1):
        try:
            resp = requests.post(base + "/chat/completions", json={
                "model": rt["model"],
                "messages": [{"role": "user",
                              "content": PROMPT.replace("{text}", (r["text"] or "")[:1200])}],
                "temperature": 0, "max_tokens": 350,
            }, headers=hdr, timeout=60)
            result = _loose_json(resp.json()["choices"][0]["message"]["content"])
        except Exception as e:
            fail += 1
            if fail <= 5 or fail % 20 == 0:
                print(f"  ! #{r['id']} AI 失败: {type(e).__name__}: {str(e)[:70]}")
            time.sleep(1)
            continue

        # tags：去重去空截断，最多 10 个
        tag_list = []
        for x in (result.get("tags") or []):
            s = str(x).strip()[:40]
            if s and s not in tag_list:
                tag_list.append(s)
            if len(tag_list) >= 10:
                break
        for s in tag_list:
            tag_counter[s] = tag_counter.get(s, 0) + 1

        # 行业/单位/省州/城市：仅原值为空时回填（不覆盖已有结论）
        sets, args = ["tags=?"], [json.dumps(tag_list, ensure_ascii=False)]
        if not (r["industry"] or "").strip():
            ind = _clean_str(result.get("industry"), 20)
            if ind:
                sets.append("industry=?"); args.append(ind)
        if not (r["org"] or "").strip() or r["org"] == "[]":
            orgs = [str(x).strip()[:60] for x in (result.get("organizations") or [])
                    if str(x).strip()][:3]
            if orgs:
                sets.append("org=?"); args.append(json.dumps(orgs, ensure_ascii=False))
        if not (r["geo_province"] or "").strip():
            pv = _clean_str(result.get("province"), 40)
            if pv:
                sets.append("geo_province=?"); args.append(pv)
        if not (r["geo_city"] or "").strip():
            ct = _clean_str(result.get("city"), 40)
            if ct:
                sets.append("geo_city=?"); args.append(ct)
        args.append(r["id"])
        for attempt in range(3):
            try:
                conn.execute(f"UPDATE tweets SET {', '.join(sets)} WHERE id=?", args)
                break
            except sqlite3.OperationalError:
                if attempt == 2:
                    raise
                time.sleep(2)
        done += 1
        empty += 0 if tag_list else 1
        if idx % 50 == 0:
            rate = idx / max(time.time() - t0, 1)
            remain = (len(rows) - idx) / max(rate, 0.01) / 60
            print(f"  进度 {idx}/{len(rows)} · 有标签 {done - empty} · 无标签 {empty} · "
                  f"失败 {fail} · {rate:.1f} 条/s · 预计剩余 {remain:.0f} 分钟")

    print(f"\n回填完成：{done} 条（其中 {empty} 条无可靠标签 → '[]'）| 失败 {fail}")
    top = sorted(tag_counter.items(), key=lambda x: -x[1])[:15]
    print("高频标签 Top15:")
    for k, v in top:
        print(f"  {k} × {v}")
    conn.close()


if __name__ == "__main__":
    main()
