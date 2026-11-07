"""地理标签补齐/复核脚本。

规则层见 app/geo_rules.py（2026-11-06 重构）：
  flag > 强地理（国名/城市/州省）> 弱线索（企业/媒体名，默认不采信）

用法：
  python backfill_geo.py                 # 只给空标签补强地理/国旗判定
  python backfill_geo.py --org           # 允许企业名兜底（写 soft_org 低置信）
  python backfill_geo.py --ai            # 规则不命中时调 AI 补
  python backfill_geo.py --audit         # 复核存量标签，只报告不写库
  python backfill_geo.py --audit --fix   # 复核并修正（冲突/企业名误标 → AI 重判）
  python backfill_geo.py --audit --fix --no-ai   # 复核修正但不调 AI（冲突项清空待审）

修正原则：企业/媒体总部不再当作涉事国家；"FBI/CISA 披露"类机构名不参与判定。
"""
import json
import re
import sys
import time
from pathlib import Path

import sqlite3

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.geo_rules import match_geo  # noqa: E402

DB = r"D:\AI-work\黑客组织报告编写\x-intel-monitor\data\xintel.db"

# 区域级标签是合法的粗粒度结论（AI 判定"主要受害国"之外的兜底），不参与复核改写
REGION_LABELS = {"欧洲", "亚洲", "非洲", "北美", "南美", "拉美", "美洲", "中东",
                 "东南亚", "东亚", "欧盟", "全球", "未知"}


def _ensure_cols(conn):
    """补 geo_basis 列（独立运行脚本时数据库可能尚未被服务端迁移过）"""
    conn.execute("PRAGMA busy_timeout=20000")  # 服务在线时与之抢写锁，最多等 20s
    try:
        conn.execute("ALTER TABLE tweets ADD COLUMN geo_basis TEXT DEFAULT ''")
    except Exception:
        pass


def _ai_cfg():
    rt = json.loads((Path(DB).parent / "runtime.json").read_text(encoding="utf-8"))["ai"]
    base = rt["base_url"].rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return rt, base, {"Authorization": "Bearer " + rt["api_key"]}


def ai_country(text, rt, base, hdr):
    """AI 判定涉事国家。prompt 明确区分披露方与受害方，防止企业总部误标。"""
    import requests
    prompt = (
        "判断以下推文中【事件发生地/受害方所在国家】的中文标准名（如 美国/中国/日本）。\n"
        "重要规则：\n"
        "1) 发布消息的企业、媒体、安全厂商、政府机构的总部所在地【不是】涉事国家，"
        "例如 'Microsoft 披露某漏洞' 不能因此判为美国；\n"
        "2) 只有企业名、产品名，没有任何受害方或事件发生地的地理线索时，填 全球；\n"
        "3) 攻击者所属国家不等于受害国，除非推文明确说受害方在该国；\n"
        "4) 多国受害填主要受害国；不确定填 全球。\n"
        '只输出 JSON：{"country":"..."}\n\n推文：\n{text}'
    )
    r = requests.post(base + "/chat/completions", json={
        "model": rt["model"],
        "messages": [{"role": "user", "content": prompt.replace("{text}", text[:1000])}],
        "temperature": 0, "max_tokens": 80,
    }, headers=hdr, timeout=60)
    content = r.json()["choices"][0]["message"]["content"]
    m = re.search(r'"country"\s*:\s*"([^"]+)"', content)
    c = (m.group(1) if m else content).strip()[:30] or "全球"
    if not re.search(r"[\u4e00-\u9fff]", c):
        c = "全球"
    return c


def audit(conn, fix=False, use_ai=True, rt=None, base=None, hdr=None):
    """复核存量非空标签，返回统计。"""
    rows = conn.execute(
        "SELECT id, screen_name, geo_country, text FROM tweets "
        "WHERE geo_country NOT IN ('','全球','未知') ORDER BY id").fetchall()
    flag_ok = geo_ok = region_ok = 0
    soft, conflict, unos = [], [], []
    for r in rows:
        text = r["text"] or ""
        if r["geo_country"] in REGION_LABELS:
            region_ok += 1
            continue
        g = match_geo(text)
        if g["basis"] == "flag":
            flag_ok += 1
            continue
        if g["basis"] == "geo" and g["country"] == r["geo_country"]:
            geo_ok += 1
            continue
        if g["basis"] == "geo" and g["country"] != r["geo_country"]:
            conflict.append((r, g))
            continue
        if g["basis"] == "soft_org_only":
            om = g["orgs"]
            if len({c for _k, c in om}) == 1 and om[0][1] == r["geo_country"]:
                soft.append((r, g))
            else:
                conflict.append((r, g))
            continue
        if g["basis"] == "ambiguous":
            conflict.append((r, g))
            continue
        unos.append(r)

    print(f"存量标签 {len(rows)} 条：国旗一致 {flag_ok} | 强地理一致 {geo_ok} | "
          f"区域级 {region_ok} | 仅企业名支撑 {len(soft)} | 冲突/多国 {len(conflict)} | "
          f"无规则命中(历史 AI) {len(unos)}")
    print("\n=== 仅企业名支撑（最低置信，系统性误标来源）===")
    for r, g in soft:
        print(f"#{r['id']} @{r['screen_name']} → {r['geo_country']} | org={g['orgs']} | "
              f"{(r['text'] or '')[:80]}")

    todo = []
    if fix:
        for r, g in conflict:
            # 强地理线索明确且唯一 → 确定性改写，不必耗 AI
            if g["basis"] == "geo" and g["country"] and g["country"] != r["geo_country"]:
                conn.execute("UPDATE tweets SET geo_country=?, geo_basis='geo_recheck' WHERE id=?",
                             (g["country"], r["id"]))
                print(f"  #{r['id']}: {r['geo_country']} → {g['country']} [强地理纠正] "
                      f"{r['text'][:60]!r}")
            else:
                todo.append((r, "conflict", g))
        for r, g in soft:
            todo.append((r, "soft", g))
    if not fix:
        print("\n=== 冲突/多国（需复核）===")
        for r, g in conflict:
            print(f"#{r['id']} @{r['screen_name']} 库值 {r['geo_country']} → 规则 {g['country'] or '不定'}"
                  f"({g['basis']}) matches={g['matches']} orgs={g['orgs']} | {(r['text'] or '')[:80]}")
        print("\n（加 --fix 才会改写；--fix 默认用 AI 重判，--no-ai 则清空待审）")
        return {"total": len(rows), "soft": len(soft), "conflict": len(conflict), "fixed": 0}

    fixed = cleared = failed = 0
    for i, (r, kind, g) in enumerate(todo, 1):
        newc = ""
        if use_ai:
            try:
                newc = ai_country(r["text"] or "", rt, base, hdr)
            except Exception as e:
                failed += 1
                print(f"  ! #{r['id']} AI 失败: {str(e)[:70]}")
        if not newc:
            conn.execute("UPDATE tweets SET geo_country='', geo_basis='rewrite_pending' WHERE id=?",
                         (r["id"],))
            cleared += 1
        else:
            conn.execute("UPDATE tweets SET geo_country=?, geo_basis=? WHERE id=?",
                         (newc, "ai_recheck", r["id"]))
            fixed += 1
            if newc != r["geo_country"]:
                print(f"  #{r['id']}: {r['geo_country']} → {newc} [{kind}] {(r['text'] or '')[:50]}")
        time.sleep(0.4)
    print(f"\n复核修正：改写 {fixed} 条（其中判定变化见上）| 清空待审 {cleared} 条 | AI 失败 {failed} 条")
    return {"total": len(rows), "soft": len(soft), "conflict": len(conflict),
            "fixed": fixed, "cleared": cleared, "failed": failed}


def main():
    use_ai = "--ai" in sys.argv
    allow_org = "--org" in sys.argv
    do_audit = "--audit" in sys.argv
    fix = "--fix" in sys.argv
    no_ai = "--no-ai" in sys.argv

    conn = sqlite3.connect(DB, timeout=30)
    conn.isolation_level = None  # 自动提交：每条 UPDATE 独立短事务，避免长事务锁死在线服务
    conn.row_factory = sqlite3.Row
    _ensure_cols(conn)

    rt = base = hdr = None
    if use_ai or (fix and not no_ai):
        rt, base, hdr = _ai_cfg()

    if do_audit:
        audit(conn, fix=fix, use_ai=(not no_ai),
              rt=rt, base=base, hdr=hdr)
        conn.close()
        return

    rows = conn.execute(
        "SELECT id, text FROM tweets WHERE ai_status='ok' AND (geo_country='' OR geo_country IS NULL)"
    ).fetchall()
    print(f"待补国家: {len(rows)} 条")

    rule_done, soft_done, ai_todo = 0, 0, []
    for r in rows:
        g = match_geo(r["text"] or "", allow_org=allow_org)
        if g["country"]:
            conn.execute("UPDATE tweets SET geo_country=?, geo_basis=? WHERE id=?",
                         (g["country"], g["basis"], r["id"]))
            if g["basis"] == "soft_org":
                soft_done += 1
            else:
                rule_done += 1
        else:
            ai_todo.append(r["id"])
    print(f"规则命中: {rule_done} 条（强地理/国旗）| 企业名兜底: {soft_done} 条 | 需 AI: {len(ai_todo)} 条")

    if use_ai and ai_todo:
        texts = {r["id"]: r["text"] for r in rows}
        done, fail = 0, 0
        for tid in ai_todo:
            try:
                c = ai_country(texts.get(tid) or "", rt, base, hdr)
                for attempt in range(3):
                    try:
                        conn.execute("UPDATE tweets SET geo_country=?, geo_basis='ai' WHERE id=?",
                                     (c, tid))
                        break
                    except sqlite3.OperationalError:
                        if attempt == 2:
                            raise
                        time.sleep(2)
                done += 1
            except Exception as e:
                fail += 1
                print("  AI 失败:", str(e)[:80])
            time.sleep(0.5)
        print(f"AI 补齐: {done} 条 | 失败 {fail}")

    remain = conn.execute(
        "SELECT COUNT(*) FROM tweets WHERE ai_status='ok' AND (geo_country='' OR geo_country IS NULL)"
    ).fetchone()[0]
    print(f"剩余未标注: {remain} 条")
    conn.close()


if __name__ == "__main__":
    main()
