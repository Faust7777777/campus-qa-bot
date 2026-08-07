from __future__ import annotations

import re
from typing import Any

from .contracts import normalized_text


_CAMPUS_ALIASES = {
    "凌水校区": "凌水",
    "开发区校区": "开发区",
    "盘锦校区": "盘锦",
    "凌水": "凌水",
    "开发区": "开发区",
    "盘锦": "盘锦",
}
_PROCEDURE_HINTS = ("怎么", "如何", "怎么办", "申请", "办理", "报修", "连接", "补办")
_FACT_HINTS = ("是什么", "多少", "几点", "时间", "地址", "电话", "在哪里", "哪儿")
_FACET_HINTS = {
    "材料": ("材料", "证件"),
    # Keep required facets conservative.  Generic "怎么/如何" wording says
    # little about which evidence facet is actually needed; treating it as a
    # mandatory流程 facet caused otherwise valid fast-path queries to fail.
    "流程": ("流程", "步骤"),
    "申请": ("申请", "申报"),
    "入口": ("入口", "系统", "平台", "在哪里", "哪儿"),
    "时间": ("时间", "几点", "时段", "开放"),
    "地点": ("地点", "地址", "位置"),
    "联系": ("电话", "联系", "咨询"),
    "报修": ("报修", "故障"),
    "账号": ("账号", "学号"),
    "密码": ("密码",),
    "费用": ("费用", "收费", "缴费"),
    "医保": ("医保",),
    "住宿": ("宿舍", "住宿"),
}


def fast_query_plan(question: str, history: list[dict[str, str]] | None = None) -> dict[str, Any] | None:
    """Return a conservative planner-equivalent object for simple questions.

    This deliberately declines ambiguous, historical, multi-part, or
    context-dependent questions.  The caller still runs QueryPlan validation
    and all normal retrieval/scope gates.
    """

    text = question.strip()
    compact = normalized_text(text)
    if history or not compact or len(text) > 48:
        return None
    if any(marker in compact for marker in ("上次", "刚才", "那个", "这个", "去年", "往年", "以前")):
        return None
    if any(char in text for char in ("，", ",", "；", ";", "和", "以及", "还有")):
        return None
    if not any(hint in compact for hint in (*_PROCEDURE_HINTS, *_FACT_HINTS)):
        return None
    intent = "procedure" if any(hint in compact for hint in _PROCEDURE_HINTS) else "fact"
    campus = ""
    for alias, value in _CAMPUS_ALIASES.items():
        if alias in compact:
            campus = value
            break
    facets = [name for name, hints in _FACET_HINTS.items() if any(h in compact for h in hints)]
    # In "怎么申请/如何申请", 申请 is the verb describing the operation,
    # not proof that the answer must contain an application facet.  Explicit
    # phrases such as "申请材料" still retain their facet below.
    if re.search(r"(?:怎么|如何)申请", compact) and "申请材料" not in compact:
        facets = [facet for facet in facets if facet != "申请"]
    # A query with no concrete facet is still useful for navigation, but keep
    # the list small so the existing evidence-coverage gate is not made stricter.
    facets = list(dict.fromkeys(facets))[:4]
    return {
        "intent": intent,
        "standalone_query": text,
        "subqueries": [text],
        "entities": [],
        "required_facets": facets,
        "filters": {"campus": campus, "audience": "本科生", "time_scope": "current"},
    }


def lexical_query_plan(question: str) -> dict[str, Any]:
    """A plan for a single-turn question, built without calling a model.

    Everything the planner produced that retrieval still reads can be read off
    the question itself.  The campus is a substring match; the audience is
    always 本科生; the time scope is always current, because a question asked in
    a group is about the rule in force.  Nothing here can be wrong in a way that
    removes evidence, which is what the planner's guesses kept doing - a rewrite
    that dropped the question's own vocabulary, an out_of_scope verdict that
    skipped retrieval, a historical verdict that filtered out every current card.

    Measured over thirty colloquial questions, retrieving on the question as
    typed cited the right card 28 times against the planner's 23.
    """

    text = question.strip()
    compact = normalized_text(text)
    campus = ""
    for alias, value in _CAMPUS_ALIASES.items():
        if alias in compact:
            campus = value
            break
    return {
        "intent": "fact",
        "standalone_query": text,
        "subqueries": [text],
        "entities": [],
        "required_facets": [],
        # Empty rather than "current": whether the answer lives on a dated
        # card is a property of the knowledge base that the question
        # cannot reveal, so both are searched and the selector chooses.
        "filters": {"campus": campus, "audience": "本科生", "time_scope": ""},
    }
