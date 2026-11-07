# -*- coding: utf-8 -*-
"""地理标签规则层（backfill_geo.py 与审计脚本共用）。

Bug 修复（2026-11-06）
---------------------
原 KEYWORDS 把企业总部地名（tencent/microsoft/google/apple/cloudflare/namecheap…）
和政府机构（FBI/CISA/NCSC…）当作国家特征，导致"A 国企业/机构披露 B 国受害事件"时，
把披露方总部误标成受害国。典型误标：
  - "Apple warns German MPs ... spyware"      → 美国（应为 德国）
  - "Google dismantles Shenzhen maker ..."    → 美国（主体在中国）
  - "FBI attributes Binance theft to North Korea" → 美国（应为 朝鲜）
  - "Namecheap suspended a fake GrapheneOS site"  → 以色列（Namecheap 是美国注册商）

现规则分三层，且**只有前两层会直接产出国家标签**：
1) FLAG_MAP        国旗 emoji（最高置信，basis=flag）
2) STRONG_GEO      国名/国民称谓/城市/州省/行政区（basis=geo）
3) ORG_TERMS       企业/媒体/机构名（**仅作弱线索**）：默认不产出标签，
                   仅在 allow_org=True 且无任何强线索时产出 basis=soft_org（低置信）
政府/执法机构（FBI/CISA/NCSC/KISA…）一律不参与国家判定——它们通常只是披露方。

匹配细节：
- ASCII 词条按词边界匹配（"korea" 不会命中 "korean" 以外的词，也不会命中 "ukraine" 里的 uk）
- 最长匹配优先并剔除被长词覆盖的短词（"north korea" 优先于 "korea"；"new mexico" 优先于 "mexico"）
- 命中多个不同国家 → basis=ambiguous，不写标签，交给 AI 复核
"""
import re

FLAG_MAP = {
    "🇨🇳": "中国", "🇹🇼": "中国台湾", "🇭🇰": "中国香港",
    "🇺🇸": "美国", "🇯🇵": "日本", "🇰🇷": "韩国", "🇰🇵": "朝鲜",
    "🇮🇳": "印度", "🇷🇺": "俄罗斯", "🇬🇧": "英国", "🇫🇷": "法国",
    "🇩🇪": "德国", "🇮🇹": "意大利", "🇪🇸": "西班牙", "🇵🇹": "葡萄牙",
    "🇧🇷": "巴西", "🇦🇷": "阿根廷", "🇲🇽": "墨西哥", "🇨🇦": "加拿大",
    "🇦🇺": "澳大利亚", "🇳🇿": "新西兰", "🇮🇱": "以色列", "🇮🇷": "伊朗",
    "🇸🇦": "沙特", "🇦🇪": "阿联酋", "🇹🇷": "土耳其", "🇮🇩": "印度尼西亚",
    "🇻🇳": "越南", "🇹🇭": "泰国", "🇵🇭": "菲律宾", "🇲🇾": "马来西亚",
    "🇸🇬": "新加坡", "🇵🇰": "巴基斯坦", "🇧🇩": "孟加拉国", "🇲🇲": "缅甸",
    "🇰🇭": "柬埔寨", "🇿🇦": "南非", "🇳🇬": "尼日利亚", "🇪🇬": "埃及",
    "🇺🇦": "乌克兰", "🇵🇱": "波兰", "🇳🇱": "荷兰", "🇧🇪": "比利时",
    "🇸🇪": "瑞典", "🇳🇴": "挪威", "🇫🇮": "芬兰", "🇩🇰": "丹麦",
    "🇨🇭": "瑞士", "🇦🇹": "奥地利", "🇨🇿": "捷克", "🇷🇴": "罗马尼亚",
    "🇭🇺": "匈牙利", "🇬🇷": "希腊", "🇮🇪": "爱尔兰", "🇨🇴": "哥伦比亚",
    "🇨🇱": "智利", "🇵🇪": "秘鲁", "🇻🇪": "委内瑞拉", "🇰🇿": "哈萨克斯坦",
    "🇲🇳": "蒙古", "🇱🇰": "斯里兰卡", "🇳🇵": "尼泊尔", "🇪🇨": "厄瓜多尔",
}

# 强地理线索：只放真正的地理名（国名/国民/城市/州省/行政区），不放任何企业或披露机构
STRONG_GEO = [
    # 东亚 —— 顺序不重要，"north korea" 靠最长匹配压过 "korea"
    ("朝鲜", ["north korea", "dprk", "朝鲜", "平壤", "pyongyang"]),
    ("韩国", ["south korea", "republic of korea", "korea", "korean", "seoul", "韩国", "首尔"]),
    ("日本", ["japan", "japanese", "tokyo", "osaka", "kyoto", "日本", "东京", "大阪", "京都"]),
    ("中国", ["china", "chinese", "prc", "中国", "北京", "上海", "深圳", "广州", "杭州", "成都",
             "beijing", "shanghai", "shenzhen", "guangzhou", "hangzhou", "chengdu"]),
    ("中国台湾", ["taiwan", "taiwanese", "taipei", "台湾", "台北"]),
    ("中国香港", ["hong kong", "香港"]),
    ("中国澳门", ["macau", "macao", "澳门"]),
    ("蒙古", ["mongolia", "ulaanbaatar", "蒙古"]),
    # 北美
    ("美国", ["united states", "u.s.", "u.s.a.", "usa", "us-based", "american", "美国",
             "washington", "new york", "california", "texas", "florida", "virginia",
             "minnesota", "pennsylvania", "oregon", "mississippi", "ohio", "illinois",
             "michigan", "arizona", "colorado", "massachusetts", "new jersey",
             "new mexico", "north carolina", "south carolina", "tennessee", "kentucky",
             "maryland", "connecticut", "wisconsin", "missouri", "utah", "nevada",
             "白宫", "五角大楼"]),
    ("加拿大", ["canada", "canadian", "ottawa", "toronto", "vancouver", "montreal",
             "burnaby", "加拿大"]),
    ("墨西哥", ["mexico", "mexican", "墨西哥"]),
    # 欧洲
    ("英国", ["united kingdom", "u.k.", "uk", "britain", "british", "england", "scotland",
             "wales", "london", "英国", "伦敦"]),
    ("法国", ["france", "french", "paris", "strasbourg", "法国", "巴黎"]),
    ("德国", ["germany", "german", "berlin", "munich", "德国", "柏林"]),
    ("意大利", ["italy", "italian", "rome", "milan", "意大利"]),
    ("西班牙", ["spain", "spanish", "madrid", "barcelona", "西班牙", "马德里"]),
    ("葡萄牙", ["portugal", "portuguese", "lisbon", "葡萄牙"]),
    ("荷兰", ["netherlands", "dutch", "amsterdam", "荷兰"]),
    ("比利时", ["belgium", "belgian", "brussels", "比利时"]),
    ("瑞士", ["switzerland", "swiss", "zurich", "geneva", "瑞士"]),
    ("奥地利", ["austria", "austrian", "vienna", "奥地利"]),
    ("瑞典", ["sweden", "swedish", "stockholm", "瑞典"]),
    ("挪威", ["norway", "norwegian", "oslo", "挪威"]),
    ("芬兰", ["finland", "finnish", "helsinki", "芬兰"]),
    ("丹麦", ["denmark", "danish", "copenhagen", "丹麦"]),
    ("波兰", ["poland", "polish", "warsaw", "波兰"]),
    ("捷克", ["czech", "prague", "捷克"]),
    ("罗马尼亚", ["romania", "romanian", "bucharest", "罗马尼亚"]),
    ("匈牙利", ["hungary", "hungarian", "budapest", "匈牙利"]),
    ("希腊", ["greece", "greek", "athens", "希腊"]),
    ("爱尔兰", ["ireland", "irish", "dublin", "爱尔兰"]),
    ("乌克兰", ["ukraine", "ukrainian", "kyiv", "kiev", "乌克兰", "基辅"]),
    ("俄罗斯", ["russia", "russian", "moscow", "kremlin", "俄罗斯", "莫斯科"]),
    # 中东/中亚
    ("伊朗", ["iran", "iranian", "tehran", "伊朗", "德黑兰"]),
    ("以色列", ["israel", "israeli", "tel aviv", "jerusalem", "以色列"]),
    ("土耳其", ["turkey", "turkish", "ankara", "istanbul", "土耳其"]),
    ("沙特", ["saudi", "riyadh", "沙特"]),
    ("阿联酋", ["united arab emirates", "uae", "dubai", "abu dhabi", "阿联酋"]),
    ("哈萨克斯坦", ["kazakhstan", "astana", "almaty", "哈萨克斯坦"]),
    # 南亚/东南亚
    ("印度", ["india", "indian", "new delhi", "mumbai", "bangalore", "bengaluru",
             "hyderabad", "chennai", "印度", "新德里", "孟买"]),
    ("巴基斯坦", ["pakistan", "pakistani", "islamabad", "karachi", "巴基斯坦"]),
    ("孟加拉国", ["bangladesh", "dhaka", "孟加拉"]),
    ("斯里兰卡", ["sri lanka", "colombo", "斯里兰卡"]),
    ("尼泊尔", ["nepal", "kathmandu", "尼泊尔"]),
    ("新加坡", ["singapore", "新加坡"]),
    ("马来西亚", ["malaysia", "malaysian", "kuala lumpur", "马来西亚"]),
    ("印度尼西亚", ["indonesia", "indonesian", "jakarta", "印度尼西亚", "印尼"]),
    ("菲律宾", ["philippines", "filipino", "manila", "菲律宾", "马尼拉"]),
    ("越南", ["vietnam", "vietnamese", "hanoi", "ho chi minh", "越南"]),
    ("泰国", ["thailand", "thai", "bangkok", "泰国"]),
    ("缅甸", ["myanmar", "burma", "yangon", "缅甸"]),
    ("柬埔寨", ["cambodia", "phnom penh", "柬埔寨"]),
    # 大洋洲/非洲/拉美
    ("澳大利亚", ["australia", "australian", "sydney", "melbourne", "canberra", "澳大利亚"]),
    ("新西兰", ["new zealand", "auckland", "wellington", "新西兰"]),
    ("南非", ["south africa", "johannesburg", "cape town", "南非"]),
    ("尼日利亚", ["nigeria", "lagos", "abuja", "尼日利亚"]),
    ("埃及", ["egypt", "egyptian", "cairo", "埃及"]),
    ("肯尼亚", ["kenya", "nairobi", "肯尼亚"]),
    ("赞比亚", ["zambia", "lusaka", "赞比亚"]),
    ("巴西", ["brazil", "brazilian", "sao paulo", "rio de janeiro", "巴西"]),
    ("阿根廷", ["argentina", "buenos aires", "阿根廷"]),
    ("智利", ["chile", "santiago", "智利"]),
    ("秘鲁", ["peru", "lima", "秘鲁"]),
    ("哥伦比亚", ["colombia", "bogota", "哥伦比亚"]),
    ("委内瑞拉", ["venezuela", "caracas", "委内瑞拉"]),
    ("厄瓜多尔", ["ecuador", "quito", "厄瓜多尔"]),
]

# 弱线索：企业/媒体/组织名。默认不产出国家标签；仅 allow_org=True 且无强线索时用。
ORG_TERMS = [
    ("中国", ["tencent", "level infinite", "腾讯", "alibaba", "阿里巴巴", "huawei", "华为",
             "baidu", "百度", "weibo", "微博", "bytedance", "字节跳动", "xiaomi", "小米",
             "oppo", "vivo", "zte", "中兴", "dji", "大疆"]),
    ("美国", ["microsoft", "微软", "google", "谷歌", "alphabet", "amazon", "亚马逊", "apple",
             "苹果", "cloudflare", "meta", "facebook", "instagram", "whatsapp", "namecheap",
             "godaddy", "crowdstrike", "mandiant", "palo alto", "fortinet", "cisco",
             "broadcom", "vmware", "oracle", "ibm", "intel", "amd", "nvidia", "qualcomm",
             "salesforce", "adobe", "twitter", "x corp", "openai", "anthropic", "okta"]),
    ("日本", ["sony", "索尼", "nintendo", "任天堂", "toyota", "丰田", "honda", "本田",
             "panasonic", "松下", "fujitsu", "富士通", "nec", "rakuten", "乐天"]),
    ("韩国", ["samsung", "三星", "lg electronics", "naver", "kakao", "sk telecom",
             "hyundai", "现代"]),
    ("俄罗斯", ["kaspersky", "卡巴斯基", "yandex"]),
    ("以色列", ["check point", "wiz", "cyberark", "cellebrite"]),
    ("英国", ["bbc", "sophos", "bae systems"]),
    ("德国", ["sap", "siemens", "西门子", "deutsche telekom", "德国电信"]),
    ("法国", ["thales", "orange", "airbus"]),
    ("荷兰", ["philips", "飞利浦"]),
]


def _is_ascii(s):
    return bool(re.search(r"[a-z]", s.lower()))


# URL / 裸域名 / 邮箱 / @handle 会制造假命中（xcopy.uk → uk、ai.google.com → google），
# 匹配前先掩码；域名后缀不作为地理证据（yna.co.kr 之类的弱域名线索已随之一并放弃）。
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
_DOMAIN_RE = re.compile(
    r"\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|edu|gov|mil|int|io|co|me|ai|app|dev|info|biz|xyz|"
    r"uk|us|cn|hk|tw|jp|kr|kp|de|fr|ru|ua|pl|nl|be|se|no|fi|dk|ch|at|cz|ro|hu|gr|ie|it|es|pt|"
    r"il|ir|sa|ae|tr|id|vn|th|ph|my|sg|pk|bd|mm|kh|za|ng|eg|kz|mn|lk|np|ec|mx|ar|cl|pe|co|ve|"
    r"br|ca|au|nz|in)\b", re.I)
_HANDLE_RE = re.compile(r"@[A-Za-z0-9_]{2,}")


def _prepare(text):
    """掩码 URL/域名/@handle，返回用于匹配的小写文本。"""
    t = _URL_RE.sub(" ", text or "")
    t = _DOMAIN_RE.sub(" ", t)
    t = _HANDLE_RE.sub(" ", t)
    return t.lower()


def _find_all(text, terms, country):
    """返回 [(start, end, kw, country)]；ASCII 用词边界，中文用子串。"""
    out = []
    tl = text.lower()
    for kw in terms:
        k = kw.lower().strip()
        if not k:
            continue
        if _is_ascii(k):
            pat = r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])"
            for m in re.finditer(pat, tl):
                out.append((m.start(), m.end(), kw, country))
        else:
            start = 0
            while True:
                i = tl.find(k, start)
                if i < 0:
                    break
                out.append((i, i + len(k), kw, country))
                start = i + len(k)
    return out


def _dedupe_overlaps(matches):
    """最长匹配优先，剔除被更长命中覆盖的短命中（north korea > korea, new mexico > mexico）"""
    kept = []
    for m in sorted(matches, key=lambda x: (-(x[1] - x[0]), x[0])):
        s, e = m[0], m[1]
        if any(not (e <= ks or s >= ke) for ks, ke, *_ in kept):
            continue
        kept.append(m)
    return kept


def strong_matches(text):
    """强地理线索命中列表 [(kw, country)]（已去重叠）"""
    raw = []
    t = _prepare(text)
    for country, terms in STRONG_GEO:
        raw.extend(_find_all(t, terms, country))
    kept = _dedupe_overlaps(raw)
    seen, out = set(), []
    for _s, _e, kw, country in kept:
        key = (kw.lower(), country)
        if key in seen:
            continue
        seen.add(key)
        out.append((kw, country))
    return out


def org_matches(text):
    """弱线索（企业/媒体名）命中列表 [(kw, country)]"""
    raw = []
    t = _prepare(text)
    for country, terms in ORG_TERMS:
        raw.extend(_find_all(t, terms, country))
    kept = _dedupe_overlaps(raw)
    seen, out = set(), []
    for _s, _e, kw, country in kept:
        key = (kw.lower(), country)
        if key in seen:
            continue
        seen.add(key)
        out.append((kw, country))
    return out


# 大中华区细化标签：同时命中"中国"与港/台时保留更具体的
_GREATER_CN = {"中国", "中国香港", "中国台湾", "中国澳门"}


def _resolve_countries(matches):
    countries = []
    for _kw, c in matches:
        if c not in countries:
            countries.append(c)
    if len(countries) > 1 and set(countries) <= _GREATER_CN:
        # 取最具体的一个（中国香港/中国台湾/中国澳门 优先于 中国）
        for c in countries:
            if c != "中国":
                return [c]
    return countries


def match_geo(text, allow_org=False):
    """判定地理标签。

    返回 dict:
      country    建议国家（空串表示不定）
      basis      flag | geo | soft_org | ambiguous | soft_org_only | ""
      matches    强线索命中 [(kw, country)]
      orgs       弱线索命中 [(kw, country)]
    只有 basis in (flag, geo) 或 (soft_org 当 allow_org=True) 时 country 才非空。
    """
    t = text or ""
    for flag, country in FLAG_MAP.items():
        if flag in t:
            return {"country": country, "basis": "flag",
                    "matches": [(flag, country)], "orgs": org_matches(t)}
    sm = strong_matches(t)
    countries = _resolve_countries(sm)
    om = org_matches(t)
    if len(countries) == 1:
        return {"country": countries[0], "basis": "geo", "matches": sm, "orgs": om}
    if len(countries) > 1:
        return {"country": "", "basis": "ambiguous", "matches": sm, "orgs": om}
    if allow_org and om:
        oc = _resolve_countries(om)
        if len(oc) == 1:
            return {"country": oc[0], "basis": "soft_org", "matches": sm, "orgs": om}
        if len(oc) > 1:
            return {"country": "", "basis": "ambiguous", "matches": sm, "orgs": om}
    return {"country": "", "basis": "soft_org_only" if om else "",
            "matches": sm, "orgs": om}


def rule_country(text, allow_org=False):
    """兼容旧调用：返回 (国家, 来源)。来源为 flag/geo/soft_org 或空。"""
    r = match_geo(text, allow_org=allow_org)
    return r["country"], (r["basis"] if r["country"] else "")
