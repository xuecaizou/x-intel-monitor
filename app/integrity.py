# -*- coding: utf-8 -*-
"""AI 结果完整性分级与下游使用规则（第五批 L2）。

关键区分：
  - ai_repair 非空 = 处理痕迹，不等同于"不可信"，也不等同于"完整"
  - 完整性等级由结构性证据判定（Schema 校验 + 是否修复/截断 + 是否缺必需字段）

下游（推送/报告/导出）必须统一调用 may_auto_downstream()，不得各自判断。
"""

VERIFIED = "verified"
NORMALIZED = "normalized"
PARTIAL = "partial"
INVALID = "invalid"

_LABEL = {
    VERIFIED: "完整研判",
    NORMALIZED: "格式规范化",
    PARTIAL: "部分结果（研判未完成）",
    INVALID: "无效结果",
}


def grade_from_issues(issues, fatal=False, repaired=False, truncated=False):
    """由校验问题列表判定等级（analyze_tweet 使用）"""
    if fatal:
        return INVALID
    issues = issues or []
    hard = any(("缺少" in i) or ("类型异常" in i) or ("丢弃" in i) for i in issues)
    if truncated or hard or repaired:
        return PARTIAL
    if issues:
        return NORMALIZED      # 仅枚举纠正/范围钳制/字符串单值容错等可验证规范化
    return VERIFIED


def normalize_stored(value):
    """读取库内值：空/未知 → verified（历史数据未记录则按完整处理，但需标注来源）"""
    v = str(value or "").strip()
    if v in (VERIFIED, NORMALIZED, PARTIAL, INVALID):
        return v
    return VERIFIED


def structural_grade(tweet):
    """结构完整性原始分级（不受人工审核影响）——partial/invalid 及修复痕迹必须保留可查。"""
    return normalize_stored(tweet.get("ai_integrity"))


def review_state(tweet):
    """人工审核绑定状态三态（2026-11-09 缺陷 C 修复）。

    返回 (state, reviewed_version, current_version)：
      valid — ai_review=manual 且 ai_review_version == 当前 analysis_version
              （审核者实际审阅的就是当前版本，人工认可有效）
      stale — 版本已推进/未记录绑定版本：旧审核只是历史决定，不得冒充当前批准
      none  — 未人工审核
    """
    if str(tweet.get("ai_review") or "") != "manual":
        return "none", None, int(tweet.get("analysis_version") or 0)
    cur = int(tweet.get("analysis_version") or 0)
    rev = tweet.get("ai_review_version")
    if rev is None or str(rev).strip() == "":
        # 历史数据未记录绑定版本 → 无法证明审的是当前版本，保守按 stale
        return "stale", None, cur
    try:
        rev = int(rev)
    except Exception:
        return "stale", None, cur
    return ("valid" if rev == cur else "stale"), rev, cur


def effective_grade(tweet):
    """当前有效完整性等级。

    人工认可升级仅在审核绑定当前版本时生效（review_state=valid）；
    版本推进后旧审核自动失效，回落到结构分级——审核必须绑定用户实际审阅的版本。
    原始结构分级始终可经 structural_grade() 查询，不被人工审核抹掉。"""
    state, _rev, _cur = review_state(tweet)
    if state == "valid":
        return VERIFIED
    return structural_grade(tweet)


def may_auto_downstream(tweet):
    """是否允许自动用于下游（推送/报告/导出自动结论）。

    返回 (allowed: bool, must_label: bool, reason: str)
      verified/normalized → (True, False, "")
      partial             → (True, True, "部分结果：只能作为原始情报推送，须标注研判未完成")
      invalid             → (False, False, "无效结果：不得下游使用")
    """
    g = effective_grade(tweet)
    if g == INVALID:
        return False, False, "无效结果：不得下游使用"
    if g == PARTIAL:
        return True, True, "部分结果：AI 研判未完成，仅按原始情报处理"
    return True, False, ""


def label(tweet):
    g = effective_grade(tweet)
    return _LABEL.get(g, g)


def is_verified(tweet):
    return effective_grade(tweet) in (VERIFIED, NORMALIZED)
