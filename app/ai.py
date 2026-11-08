"""AI 研判 / 翻译 / 地理标签 / 情报分类：OpenAI 兼容 /chat/completions 接口。

分类采用「脚本规则 + AI」混合策略：
- rule_classify(text)：确定性关键词规则，命中即得分类（零 AI 成本）
- classify_batch(tweets)：规则未命中的批量送 AI（每 10 条一次调用）
- analyze_tweet：研判 prompt 内嵌 16 类定义，新推文研判时顺带完成分类
"""
import base64
import datetime
import hashlib
import json
import re
import time

import requests

from . import db
from .config import load

TIMEOUT = 90

NL = chr(10)          # 文件生成辅助：换行
BSN = chr(92) + 'n'   # 提示词字符串内的换行转义序列（\n 两字符）


# ---------- OpenAI 兼容调用 ----------

def _close_truncated(s):
    """补齐被截断 JSON 缺失的收尾引号与右括号（思维链模型长输出常截断在数组中段）"""
    in_str = esc = False
    stack = []
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack:
                    stack.pop()
    if in_str:
        s += '"'
    for c in reversed(stack):
        s += "}" if c == "{" else "]"
    return s


def _parse_json_loose(text):
    """JSON 宽松解析：直接 loads → 抠 {...} 块 → 截断补括号（三层兜底）"""
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
        try:
            return json.loads(_close_truncated(m.group(0)))
        except Exception:
            pass
    i = text.find("{")  # 截断在末尾、没有收尾 } 的情形：从首个 { 取到末尾补括号
    if i >= 0:
        try:
            return json.loads(_close_truncated(text[i:]))
        except Exception:
            pass
    raise ValueError("AI 未返回有效 JSON（前 80 字: " + str(text)[:80] + "）")


LAST_PARSE_META = {"repaired": False, "truncated": False}


def _chat(cfg, messages, json_mode=False, action="", tweet_id=None, ref=""):
    """计时外壳：一次 AI 调用（含内部重试）写一条 ai_logs 审计日志（动作/模型/耗时/状态）"""
    t0 = time.time()
    model = cfg.get("model") or ""
    try:
        out = _chat_call(cfg, messages, json_mode)
        if action:
            db.add_ai_log(action, model, tweet_id, ref,
                          int((time.time() - t0) * 1000), "ok", "")
        return out
    except Exception as e:
        if action:
            db.add_ai_log(action, model, tweet_id, ref,
                          int((time.time() - t0) * 1000), "error",
                          f"{type(e).__name__}: {str(e)[:150]}")
        raise


def _chat_call(cfg, messages, json_mode=False):
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("未配置 AI base_url")
    if not base.endswith("/v1") and not re.search(r"/v\d+$", base):
        base += "/v1"  # 用户只填了域名时自动补 /v1
    body = {"model": cfg.get("model") or "", "messages": messages,
            "temperature": 0.2, "max_tokens": int(cfg.get("max_tokens", 4096))}
    # 注：不发送 response_format=json_object——实测部分 OpenAI 兼容网关/思维链模型
    # 在该模式下会把思维链文本写进 content（JSON 泄漏/截断）。JSON 约束由 prompt
    # 承担 + _parse_json_loose 宽松解析兜底，兼容性最广。
    proxies = None
    if cfg.get("proxy"):
        p = cfg["proxy"]
        proxies = {"http": p, "https": p}
    headers = {"Authorization": "Bearer " + (cfg.get("api_key") or "")}
    tmo = TIMEOUT
    for attempt in (0, 1):
        r = requests.post(base + "/chat/completions", json=body,
                          headers=headers, timeout=tmo, proxies=proxies)
        if r.status_code != 200:
            if r.status_code == 429 or _is_quota_error(r.text):
                raise AIQuotaError(f"AI 接口 HTTP {r.status_code}: {r.text[:160]}")
            raise RuntimeError(f"AI 接口 HTTP {r.status_code}: {r.text[:160]}")
        d = r.json()
        choice = (d.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = (msg.get("content") or "").strip()
        finish = choice.get("finish_reason")
        if not content:
            # 思维链模型 token 全耗在 reasoning_content 上（finish=length）→ 扩容重试一次
            if attempt == 0 and finish == "length":
                body["max_tokens"] = int(body["max_tokens"]) * 3
                tmo = TIMEOUT * 2  # 思维链变长，同步放宽读超时
                continue
            rc = msg.get("reasoning_content") or ""
            if json_mode and rc:
                LAST_PARSE_META["repaired"] = True
                return _parse_json_loose(rc)  # 兜底：从思维链文本里抠 JSON
            raise RuntimeError(f"模型返回空内容（finish_reason={finish}）")
        if not json_mode:
            return content
        text = content
        m = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
        if m:
            text = m.group(1)
        try:
            LAST_PARSE_META["repaired"] = True
            return _parse_json_loose(text)
        except Exception:
            if attempt == 0 and finish == "length":
                body["max_tokens"] = int(body["max_tokens"]) * 3  # JSON 被截断 → 扩容重试
                tmo = TIMEOUT * 2
                continue
            if attempt == 0:
                # 思维链泄漏（content 是分析过程而非 JSON）→ 追加硬约束重试一次
                body["messages"] = body["messages"] + [
                    {"role": "user",
                     "content": "不要输出任何分析过程。立即只输出 JSON 对象本身：以 { 开头、} 结束，无其他文字。"}]
                continue
            raise
    raise RuntimeError("AI 调用重试后仍无有效结果")


def _cfg():
    return load().get("ai", {})


_QUOTA_HINTS = ("429", "rate limit", "频率限制", "使用量已超出", "quota", "额度")


def _is_quota_error(text) -> bool:
    t = str(text or "").lower()
    return any(h.lower() in t for h in _QUOTA_HINTS)


def _require_key(cfg):
    if not cfg.get("enabled"):
        raise RuntimeError("AI 功能未启用（请在「AI 设置」面板配置并启用）")
    if not cfg.get("api_key"):
        raise RuntimeError("未配置 AI API Key")


# ---------- 情报分类定义（16 类 + 其他） ----------

CATEGORIES = ["预警情报", "参考情报", "舆论情报", "政策情报", "动态情报", "态势情报",
              "主体情报", "木马攻击线索", "钓鱼网站攻击线索", "钓鱼邮件攻击线索",
              "水坑攻击线索", "暴破扫描攻击线索", "僵尸网络传播线索", "蠕虫病毒传播线索",
              "网页篡改线索", "数据泄露线索", "其他"]

CLASSIFY_DEFS = (
    "1.预警情报——即将发生的、可能对我网络安全造成影响的重大网络安全威胁事件和信息" + NL +
    "2.参考情报——已经发生的、未对我网络安全造成影响的、对防范应对类似情况具有借鉴意义的重大威胁事件和信息" + NL +
    "3.舆论情报——涉及我网络安全的、待证实的舆论信息" + NL +
    "4.政策情报——其他国家和地区、组织机构发布的、对我网络安全防护具有借鉴意义的法定或规范性文件" + NL +
    "5.动态情报——其他国家和地区、组织机构在网络安全领域采取的攻防演习、机构调整、专项行动等动作" + NL +
    "6.态势情报——以某个国家和地区、行业领域、黑客组织、威胁形式等为主题的网络安全状态和趋势" + NL +
    "7.主体情报——参与网络威胁活动和对我有潜在威胁的组织机构和人员信息" + NL +
    "8.木马攻击线索——黑客组织研发和使用的木马程序及其实施的木马攻击活动；关键信息基础设施和重点单位被植入的木马和遭受的木马攻击" + NL +
    "9.钓鱼网站攻击线索——黑客组织搭建的钓鱼网站和实施的钓鱼攻击活动" + NL +
    "10.钓鱼邮件攻击线索——黑客组织发出的钓鱼邮件和实施的钓鱼邮件攻击活动；关键信息基础设施和重点单位收到的钓鱼邮件和遭受的钓鱼邮件攻击" + NL +
    "11.水坑攻击线索——黑客组织控制或利用第三方平台传播恶意程序和实施的攻击活动" + NL +
    "12.暴破扫描攻击线索——黑客组织实施的、关键信息基础设施和重点单位遭受的定向暴破和恶意扫描攻击" + NL +
    "13.僵尸网络传播线索——在境内大范围传播、可能危害关键信息基础设施和重点单位网络安全的僵尸网络" + NL +
    "14.蠕虫病毒传播线索——在境内大范围传播、可能危害关键信息基础设施和重点单位网络安全的蠕虫病毒" + NL +
    "15.网页篡改线索——国家和地区背景黑客组织实施的、关键信息基础设施和重点单位遭受的网页篡改攻击活动" + NL +
    "16.数据泄露线索——关键信息基础设施和重点单位的数据泄露情况" + NL +
    "其他——与上述类别均不匹配"
)

CLASSIFY_PROMPT = (
    "你是威胁情报分类专家。对以下每条推文，从固定类别中选出最匹配的一个，输出严格 JSON：" + BSN +
    '{"results":[{"id":<推文序号>,"category":"<类别名>"}]}' + BSN + BSN +
    "类别定义（每条必选其一，无法判断填 其他）：" + BSN +
    CLASSIFY_DEFS + BSN +
    "分类要点：将发生且可能影响我方的归 预警情报；已发生且未影响我方但有借鉴价值的归 参考情报；"
    "对某主题的安全状态与趋势的总结归 态势情报；介绍威胁组织/人员的归 主体情报；"
    "具体攻击活动按手法归对应线索类（木马/钓鱼网站/钓鱼邮件/水坑/暴破扫描/僵尸网络/蠕虫/网页篡改/数据泄露）。" + BSN +
    "推文列表（id 为序号）：" + BSN + "{texts}"
)


def rule_classify(text):
    """脚本规则分类：高置信关键词直接判定（零 AI 成本），未命中返回空串"""
    t = (text or "").lower()
    rules = [
        ("钓鱼网站攻击线索", ["钓鱼网站", "phishing site", "phishing page", "fake login page", "仿冒网站"]),
        ("钓鱼邮件攻击线索", ["钓鱼邮件", "phishing email", "phishing mail", "恶意邮件附件"]),
        ("僵尸网络传播线索", ["僵尸网络", "botnet"]),
        ("蠕虫病毒传播线索", ["蠕虫", "worm spreading", "self-propagating worm"]),
        ("网页篡改线索", ["网页篡改", "defaced", "defacement", "website hijacked"]),
        ("暴破扫描攻击线索", ["暴破", "brute force", "bruteforce", "credential stuffing",
                          "恶意扫描", "port scanning", "masscan"]),
        ("水坑攻击线索", ["水坑攻击", "watering hole"]),
        ("木马攻击线索", ["木马", "trojan", "rat implant", "远控", "infostealer",
                      "info-stealer", "stealer malware", "backdoor 后门", "implant",
                      "python 后门", "loader payload"]),
        ("数据泄露线索", ["数据泄露", "data breach", "database for sale", "records for sale",
                      "泄露出售", "data leak", "患者记录", "数据库出售",
                      "allegedly breached", "claims breach", "dump database"]),
    ]
    for cat, kws in rules:
        for kw in kws:
            if kw in t:
                return cat
    return ""


# ---------- 研判 / 翻译 ----------

ANALYZE_PROMPT = (
    "你是资深威胁情报分析师。分析以下来自 X(Twitter) 账号 @{name} 的推文，"
    "只依据推文内容判断、不要编造，输出严格 JSON（值为中文，键固定）：" + BSN +
    '{"threat_type":"威胁类型，如 勒索软件/数据泄露/漏洞利用/APT/钓鱼/诈骗/无直接威胁",' + BSN +
    '"actors":["威胁行为体/勒索组织"],' + BSN +
    '"organizations":["涉事单位/受害机构全称，尽量写完整官方名称"],' + BSN +
    '"industry":"涉事单位所属行业：医疗/金融/政府/能源/教育/科技/零售/交通/制造/电信/军工，无法判断填 未知",' + BSN +
    '"country":"涉事国家中文标准名，多国填主要国家顿号分隔，全球性事件填 全球",' + BSN +
    '"province":"省/州/邦级行政区中文标准名，未提及则空字符串",' + BSN +
    '"city":"城市名，未提及则空字符串",' + BSN +
    '"iocs":["可提取的 IOC：域名/IP/哈希/CVE 编号等，没有则空数组"],' + BSN +
    '"tags":["情报关键词标签，最多10个，有多少提多少：威胁手法/技术要素/恶意家族/威胁组织/涉事企业机构/攻击者国家/受害国等；'
    '英文单词或缩写必须译成\\"英文（中文说明）\\"格式，如 \\"C2（命令与控制服务器）\\"、\\"RAT（远程访问木马）\\"、\\"Infostealer（信息窃取木马）\\"；'
    '泛化空词（攻击/安全/漏洞）不要；没有可靠线索给空数组"],' + BSN +
    '"severity":"critical|high|medium|low|info 之一，按对企业的潜在影响判断",' + BSN +
    '"assessment":"≤150字中文态势评估：发生了什么、谁受影响、为什么重要；信息不足时说明局限",' + BSN +
    '"recommendations":["面向企业防守方的中文处置建议，1-3条"],' + BSN +
    '"confidence":0到100的整数置信度,' + BSN +
    '"intel_category":"情报分类，从以下类别中选最匹配的一个：预警情报/参考情报/舆论情报/'
    '政策情报/动态情报/态势情报/主体情报/木马攻击线索/钓鱼网站攻击线索/钓鱼邮件攻击线索/'
    '水坑攻击线索/暴破扫描攻击线索/僵尸网络传播线索/蠕虫病毒传播线索/网页篡改线索/数据泄露线索/其他"}' + BSN +
    "分类要点：将发生且可能影响我方的归 预警情报；已发生且未影响我方但有借鉴价值的归 参考情报；"
    "对某主题的安全状态与趋势的总结归 态势情报；介绍威胁组织/人员的归 主体情报；"
    "具体攻击活动按手法归对应线索类（木马/钓鱼网站/钓鱼邮件/水坑/暴破扫描/僵尸网络/蠕虫/网页篡改/数据泄露）。" + BSN +
    "地理与实体要求：尽量把地区推断到最细层级（国家→省州→城市）；"
    "涉事单位用全称（如 Texas Tech University 而非 德州某大学）；"
    "行业按受害机构主营业务判断；不得凭空补全推文未含的地理信息。" + BSN +
    "标签要求：从推文提取具体情报要素作标签——威胁手法（木马/勒索软件/数据泄露/钓鱼/0day 利用/供应链攻击）、"
    "技术要素（C2/僵尸网络/窃密/横向移动/载荷投递）、恶意软件家族名、威胁组织名、涉事企业或机构名、"
    "攻击者归属国家、受害者所在国家；英文术语/缩写一律保留原文并附中文说明，格式为「英文（中文说明）」，"
    "如 APT（高级持续性威胁）、RMM（远程监控管理工具）；只收具体情报要素，泛化词不要；按相关度排序，最多10个。" + BSN +
    "推文原文：" + BSN + "{text}"
)


class AIQuotaError(RuntimeError):
    """AI 配额/限流错误（429/额度耗尽）：应停止自动重试，等待配额重置"""


class AIModifiedError(RuntimeError):
    """人工修订保护：该推文已由人工修订，自动研判跳过"""


class AIResultError(ValueError):
    """AI 结果未通过严格校验（进入可重试 error 状态，附脱敏诊断）"""


VALID_SEVERITY = ("critical", "high", "medium", "low", "info")
VALID_CATEGORIES = set(CATEGORIES)


def _as_str_list(v, maxn, field, issues):
    """列表字段规范化：字符串→单元素列表（记容错），逐字符拆分被禁止；非列表/字符串→空"""
    if v is None or v == "":
        return []
    if isinstance(v, list):
        out = []
        for x in v[:maxn]:
            x = str(x).strip()
            if x:
                out.append(x)
        return out
    if isinstance(v, str):
        sv = v.strip()
        if sv:
            issues.append(f"{field}:字符串按单值容错")
            return [sv[:80]]
        return []
    issues.append(f"{field}:类型异常已丢弃({type(v).__name__})")
    return []


def _validate_result(result, last_parse):
    """严格校验。返回 (clean, issues, fatal)：
    fatal=True → 结果不可信（{}、缺 assessment、severity 非法且无法纠正），调用方按失败处理"""
    issues = []
    if not isinstance(result, dict) or not result:
        return {}, ["结果为空或非对象"], True
    # 必需字段
    if not str(result.get("assessment") or "").strip():
        issues.append("缺少 assessment")
        fatal = True
    else:
        fatal = False
    # severity 枚举
    sev = str(result.get("severity") or "").strip().lower()
    if sev not in VALID_SEVERITY:
        if sev:
            issues.append(f"severity 非法({sev})→info")
        result["severity"] = "info"
    # confidence 类型与范围
    try:
        conf = int(result.get("confidence"))
    except Exception:
        if str(result.get("confidence") or "").strip():
            issues.append("confidence 非整数→0")
        conf = 0
    conf = max(0, min(100, conf))
    result["confidence"] = conf
    # 列表字段
    result["actors"] = _as_str_list(result.get("actors"), 6, "actors", issues)
    result["iocs"] = _as_str_list(result.get("iocs"), 12, "iocs", issues)
    result["recommendations"] = _as_str_list(result.get("recommendations"), 4, "recommendations", issues)
    orgs = result.get("organizations")
    result["organizations"] = _as_str_list(orgs, 6, "organizations", issues)
    # 截断/修复来源标注
    if last_parse.get("repaired"):
        issues.append("JSON 经修复解析（repaired）")
    if last_parse.get("truncated"):
        issues.append("响应被截断（truncated）")
    return result, issues, fatal


def analyze_tweet(tweet, cfg=None):
    """返回结构化研判结果 dict（含 intel_category 分类；prompt 携带中英对照与媒体备注）。
    ai_modified=1（人工修订过）的推文抛出跳过标记，由调用方决定保留人工有效值。"""
    if int(tweet.get("ai_modified") or 0) == 1:
        raise AIModifiedError("人工修订保护：跳过自动研判")
    cfg = cfg or _cfg()
    _require_key(cfg)
    prompt = (ANALYZE_PROMPT
              .replace("{name}", tweet.get("screen_name", "external"))
              .replace("{text}", tweet.get("text", "")[:1200]))
    if tweet.get("text_zh"):
        prompt += NL + "已有中文参考翻译（可采信，如有出入以原文为准）：" + NL + tweet["text_zh"][:600]
    if tweet.get("media_url"):
        prompt += (NL + "（注：该推文附有截图媒体 " + tweet["media_url"]
                   + "，当前为文本模型无法查看图片内容，请仅基于文字研判）")
    LAST_PARSE_META["repaired"] = False
    LAST_PARSE_META["truncated"] = False
    result = _chat(cfg, [{"role": "user", "content": prompt}], json_mode=True,
                   action="研判", tweet_id=tweet.get("id"),
                   ref="@" + str(tweet.get("screen_name", "")))
    parse_meta = dict(LAST_PARSE_META)
    result, issues, fatal = _validate_result(result, parse_meta)
    if fatal:
        raise AIResultError("; ".join(issues)[:200] or "结果未通过校验")
    from . import integrity as _integ
    integrity_grade = _integ.grade_from_issues(
        issues, fatal=False, repaired=parse_meta.get("repaired", False),
        truncated=parse_meta.get("truncated", False))
    sev = str(result.get("severity", "info")).lower()
    if sev not in ("critical", "high", "medium", "low", "info"):
        sev = "info"
    cat = str(result.get("intel_category", "")).strip()[:14]
    if cat not in CATEGORIES:
        cat = "其他"
    # 关键词标签：去重、去空、截断，最多 10 个（英文缩写要求模型带中文说明，这里不二次加工）
    tag_list = []
    for x in (result.get("tags") or []):
        s = str(x).strip()[:40]
        if s and s not in tag_list:
            tag_list.append(s)
        if len(tag_list) >= 10:
            break
    # 地理兜底（2026-11-07）：AI 漏判国家时用确定性规则补强（国旗/强地理词，企业名不参与），
    # 闭合"AI 实时判定"与"离线规则补标"两条路径的口径差（如 "Spanish developers" 被漏判）
    geo_country = _geo_text(result.get("country"), 60)
    geo_basis = "ai"
    if not geo_country:
        try:
            from .geo_rules import match_geo
            g = match_geo(tweet.get("text") or "")
            if g["basis"] in ("flag", "geo"):
                geo_country, geo_basis = g["country"], g["basis"]
        except Exception:
            pass
    return {
        "ai_status": "ok", "ai_error": "", "ai_model": cfg.get("model", ""),
        "ai_severity": sev,
        "ai_type": str(result.get("threat_type", ""))[:80],
        "ai_actors": json.dumps([str(x) for x in (result.get("actors") or [])][:6], ensure_ascii=False),
        "ai_iocs": json.dumps([str(x) for x in (result.get("iocs") or [])][:12], ensure_ascii=False),
        "ai_assessment": str(result.get("assessment", ""))[:400],
        "ai_recommendations": json.dumps([str(x) for x in (result.get("recommendations") or [])][:4], ensure_ascii=False),
        "ai_confidence": int(result.get("confidence") or 0),
        "ai_analyzed_at": db.now(),
        "category": cat,
        # 地理/实体字段：原实现漏写，导致入库后地区标签恒为空（2026-11-06 修复）
        # geo_country 为空时由强地理规则兜底（见上方），geo_basis 如实记录判定来源
        "geo_country": geo_country,
        "geo_province": _geo_text(result.get("province"), 40),
        "geo_city": _geo_text(result.get("city"), 40),
        "org": json.dumps([str(x) for x in (result.get("organizations") or [])][:6],
                          ensure_ascii=False) if isinstance(result.get("organizations"), list) else "[]",
        "industry": _geo_text(result.get("industry"), 20),
        "tags": json.dumps(tag_list, ensure_ascii=False),
        "ai_repair": "; ".join(issues)[:200],
        "ai_integrity": integrity_grade,
        "geo_basis": geo_basis,
    }


def _geo_text(v, n):
    """地理字段归一：列表/None → 字符串；过滤掉模型常写的占位词"""
    if isinstance(v, list):
        v = "、".join(str(x) for x in v if str(x).strip())
    s = str(v or "").strip()
    if s in ("未知", "不详", "无", "N/A", "n/a", "none", "None", "null", "NULL"):
        return ""
    return s[:n]



TRANSLATE_PROMPT = (
    "将以下 X(Twitter) 推文翻译为简体中文。要求：保留人名、组织名、产品名、域名、"
    "哈希、CVE 编号等专有名词原文；语气保持情报简报风格；输出严格 JSON：" + BSN +
    '{"title_zh":"≤60字标题式中文概括","text_zh":"完整中文翻译"}' + BSN + BSN +
    "推文原文：" + BSN + "{text}"
)


def translate_tweet(tweet, cfg=None):
    cfg = cfg or _cfg()
    _require_key(cfg)
    prompt = TRANSLATE_PROMPT.replace("{text}", tweet.get("text", "")[:1200])
    result = _chat(cfg, [{"role": "user", "content": prompt}], json_mode=True,
                   action="翻译", tweet_id=tweet.get("id"),
                   ref="@" + str(tweet.get("screen_name", "")))
    return {
        "title_zh": str(result.get("title_zh", ""))[:120],
        "text_zh": str(result.get("text_zh", ""))[:800],
        "translated_at": db.now(),
    }


def process_tweet(tweet, cfg=None):
    """研判+翻译一次完成；单步失败不阻塞另一步（失败累计 ai_retries，满 2 次放行推送并附说明）"""
    cfg = cfg or _cfg()
    fields = {}
    try:
        fields.update(analyze_tweet(tweet, cfg))
        fields["ai_retries"] = 0
    except Exception as e:
        fields.update({"ai_status": "error", "ai_error": f"{type(e).__name__}: {str(e)[:180]}",
                       "ai_retries": int(tweet.get("ai_retries") or 0) + 1,
                       "ai_analyzed_at": db.now()})
    try:
        fields.update(translate_tweet(tweet, cfg))
    except Exception as e:
        fields.setdefault("ai_error", f"{type(e).__name__}: {str(e)[:180]}")
    db.update_ai(tweet["id"], fields)
    return fields


def test_ai(cfg=None):
    """连通性测试：让模型回一个固定短语"""
    cfg = cfg or _cfg()
    _require_key(cfg)
    reply = _chat(cfg, [{"role": "user", "content": "请只回复四个字：链路正常"}], action="测试")
    return {"ok": True, "model": cfg.get("model"), "reply": reply[:80]}


def list_models(cfg=None):
    """拉取 OpenAI 兼容 /models 模型列表（无需 model 配置，只要 base_url+key）"""
    cfg = cfg or _cfg()
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("未配置 AI base_url")
    if not base.endswith("/v1") and not re.search(r"/v\d+$", base):
        base += "/v1"
    proxies = None
    if cfg.get("proxy"):
        p = cfg["proxy"]
        proxies = {"http": p, "https": p}
    r = requests.get(base + "/models",
                     headers={"Authorization": "Bearer " + (cfg.get("api_key") or "")},
                     timeout=20, proxies=proxies)
    if r.status_code != 200:
        raise RuntimeError(f"模型列表 HTTP {r.status_code}: {r.text[:160]}")
    d = r.json()
    ids = [m.get("id") for m in (d.get("data") or []) if m.get("id")]
    return sorted(set(ids))


def batch_process(ids, action, cfg=None):
    """批量翻译/研判（同步串行，单条失败不中断），返回统计"""
    cfg = cfg or _cfg()
    _require_key(cfg)
    if action not in ("analyze", "translate"):
        raise ValueError("action 只支持 analyze / translate")
    done, fail, errors = 0, 0, []
    for tid in ids[:20]:
        t = db.get_tweet(tid)
        if not t:
            continue
        base = int(t.get("analysis_version") or 0)
        try:
            fields = (analyze_tweet(t, cfg) if action == "analyze"
                      else translate_tweet(t, cfg))
            applied, reason, cur = db.apply_ai_result(tid, fields, base)
            if applied:
                done += 1
            else:
                db.add_ai_log("批量结果冲突", detail=f"{reason} v{base}->v{cur}",
                              tweet_id=tid, status="conflict")
                errors.append(f"#{tid}: 冲突({reason})")
                fail += 1
        except Exception as e:
            if action == "analyze":
                db.update_ai(tid, {"ai_status": "error",
                                   "ai_error": f"{type(e).__name__}: {str(e)[:180]}",
                                   "ai_analyzed_at": db.now()})
            fail += 1
            errors.append(f"#{tid}: {str(e)[:80]}")
        time.sleep(0.5)
    return {"action": action, "total": len(ids[:20]), "done": done,
            "fail": fail, "errors": errors[:5]}


def classify_batch(tweets, cfg=None):
    """AI 批量分类：每 10 条一次调用（省额度），直接更新 tweets.category。
    返回统计 {total, done, fail}"""
    cfg = cfg or _cfg()
    _require_key(cfg)
    done, fail = 0, 0
    groups = [tweets[i:i + 10] for i in range(0, len(tweets), 10)]
    sep = chr(10)
    for grp in groups:
        texts = sep.join(
            f"{i}. [id:{t['id']}] {(t.get('text') or '')[:200]}"
            for i, t in enumerate(grp, 1))
        prompt = CLASSIFY_PROMPT.replace("{texts}", texts)
        try:
            result = _chat(cfg, [{"role": "user", "content": prompt}], json_mode=True,
                           action="分类", ref=f"{len(grp)}条批量")
            results = result.get("results") or []
            # 单一身份：仅接受库内真实 id（序号映射已移除——序号与 id 撞号会写错情报）
            byid = {str(t["id"]): t for t in grp}
            hit = 0
            seen = set()
            for r0 in results:
                t = byid.get(str(r0.get("id", "")).strip())
                if not t:
                    continue
                if t["id"] in seen:
                    continue  # 重复结果忽略
                seen.add(t["id"])
                # 版本语义：分类结果按"当前有效值"条件写入（人工改过的不覆盖）
                if int(t.get("ai_modified") or 0) == 1:
                    continue
                cat = str(r0.get("category", "其他")).strip()[:14]
                if cat not in CATEGORIES:
                    cat = "其他"
                db.update_ai(t["id"], {"category": cat})
                hit += 1
            missing = len(grp) - hit
            fail += max(0, missing)
        except Exception as e:
            fail += len(grp)
            print("[classify] 批次失败:", str(e)[:120])
        time.sleep(1)
    return {"total": len(tweets), "done": done, "fail": fail}
