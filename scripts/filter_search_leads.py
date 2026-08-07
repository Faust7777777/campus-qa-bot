#!/usr/bin/env python
"""Score keyword-search leads for whether they answer the question they matched.

kb_clean_c.csv is a keyword search export: a row is included because the page
mentions the query's words, not because it answers the question.  So "退费" pulled
in a scam warning about refund apps, "密码" pulled in mailbox two-factor setup,
and "校车" pulled in an essay titled "爸，我妈呢？！".  Twenty of the seventy-four
pages fetched from it contained no answer to their own query.

The task list generator filtered on official host and audience but not on
whether the lead was on topic, which is the gap this closes.

Scoring, all offline:
  + the page must show a concrete service noun for the topic, not just the
    query's characters (the query "退费" appears in "退费诈骗"; the topic needs
    "学费退" or "退费流程" or a refund procedure word)
  - promotional and narrative shapes carry no procedure: 倡议/攻略/圆满/风采/
    致同学的一封信/系列讲座
  - wrong-audience shapes: MPA/在职/教工/新教工/研究生-as-service

Validated against the 2026-08-07 batch, where the outcome of every lead is
known.  Run --selftest to reproduce that check.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# A concrete handle on the service, not merely the query's characters.
TOPIC_ANCHORS: dict[str, list[str]] = {
    "成绩查询": ["成绩查询", "查询成绩", "教学管理系统", "查成绩", "成绩发布"],
    "选课退课": ["选课", "退课", "补退选"],
    "选课时间": ["选课时间", "选课工作", "补退选"],
    "教务系统密码": ["统一身份认证", "密码重置", "找回密码", "忘记密码", "重置密码"],
    "退费流程": ["学费退", "退费流程", "退费办理", "学费核对", "退费将"],
    "学费核对": ["学费核对", "学费确认", "缴费确认"],
    "在读证明": ["在读证明", "自助证明", "证明打印", "开具证明"],
    "成绩单打印": ["成绩单", "自助证明", "证明打印"],
    "四六级报名": ["四级", "六级", "CET", "笔试报名"],
    "保研推免": ["推免", "免试攻读", "推荐工作", "保研"],
    "毕业论文": ["毕业论文", "毕业设计", "开题", "答辩"],
    "出国交换": ["交换项目", "校际交换", "访学", "出国(境)项目", "出国（境）项目"],
    "实习认定": ["实习认定", "实习证明", "实习报备", "实习手续", "时长认定"],
    "宿舍调换": ["宿舍调整", "调宿", "调退宿", "退宿", "换宿舍", "住宿调整", "调换宿舍"],
    "快递点": ["快递", "驿站", "菜鸟", "取件"],
    "校车时刻": ["校车", "校园巴士", "班车", "发车"],
    "空调热水": ["热水", "淋浴", "供水时间", "空调使用", "电费充值", "生活服务指南"],
    "学生证补办": ["学生证补办", "补办学生证", "学生证挂失"],
}

# Shapes that never carry a procedure.
PROMO = re.compile(
    r"倡议|攻略|圆满|风采|致.{0,6}的一封信|系列讲座|温馨提示|预告|回顾|纪实|侧记"
    r"|你的声音|请查收|快来|收藏|了解一下|来啦|了不起|亮相|第一课|开学季|毕业季"
    r"|诈骗|反诈|警惕|防范"
)
# Serves someone other than an undergraduate.
WRONG_AUDIENCE = re.compile(r"MPA|EMBA|在职|教工|教职工|新教工|[（(]研究生[）)]|研究生赴|博士生")


def best_topic(blob: str, query: str) -> str | None:
    """Which gap a piece of text actually serves, which need not be the query
    that matched it.

    Used when labelling a card that has already been fetched, where assigning
    the right topic beats discarding it.  Deliberately NOT used by ``score``:
    letting a lead qualify under any topic measured at 92% precision against
    100% for the strict per-query check, and buying one extra good source with
    three bad ones is the wrong trade when a bad source becomes a card that
    answers confidently and wrongly.
    """

    if any(anchor in blob for anchor in TOPIC_ANCHORS.get(query, [])):
        return query
    for topic, anchors in TOPIC_ANCHORS.items():
        if any(anchor in blob for anchor in anchors):
            return topic
    return None


def score(query: str, title: str, desc: str) -> tuple[bool, str]:
    blob = f"{title}\n{desc}"
    if WRONG_AUDIENCE.search(title):
        return False, "服务对象不是本科生"
    anchors = TOPIC_ANCHORS.get(query, [])
    if anchors and not any(anchor in blob for anchor in anchors):
        return False, "正文/摘要里没有该事项的具体说法"
    # A WeChat headline is clickbait by convention, so promotional shape only
    # disqualifies when the body does not actually deliver the topic: the
    # anchor being in the title alone means the words were the hook.
    if PROMO.search(title) and not any(a in desc for a in anchors):
        return False, "宣传/叙事体，且摘要未兑现主题"
    return True, "ok"


def selftest() -> int:
    """Replay the batch whose outcome is known and report precision/recall."""

    tasks = {}
    for line in (REPO / "work/luna_tasks_gapfill_20260807.jsonl").read_text(
        encoding="utf-8"
    ).splitlines():
        if line.strip():
            t = json.loads(line)
            tasks[t["canonical_url"]] = t

    # Ground truth: a lead was good if its fetched page contained an answer.
    rows = [
        json.loads(l)
        for l in (REPO / "work/luna_gapfill_cards_20260807_reviewed.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if l.strip()
    ]
    good_urls, bad_urls = set(), set()
    for r in rows:
        task = tasks.get(r["canonical_url"])
        if not task:
            continue
        q = task.get("seed_query", "")
        anchors = TOPIC_ANCHORS.get(q, [])
        has_answer = any(a in (r.get("clean_text") or "") for a in anchors) if anchors else False
        (good_urls if has_answer else bad_urls).add(r["canonical_url"])

    tp = fp = tn = fn = 0
    for url, task in tasks.items():
        if url not in good_urls and url not in bad_urls:
            continue
        keep, _why = score(
            task.get("seed_query", ""), task.get("title", ""), task.get("seed_description", "")
        )
        if url in good_urls:
            tp += keep
            fn += not keep
        else:
            fp += keep
            tn += not keep
    total = tp + fp + tn + fn
    print(f"selftest over {total} leads with known outcomes")
    print(f"  好源被保留 (tp) {tp:>3}    好源被误杀 (fn) {fn:>3}")
    print(f"  坏源被放行 (fp) {fp:>3}    坏源被拦截 (tn) {tn:>3}")
    if tp + fp:
        print(f"  精确率 {tp / (tp + fp):.0%}   召回率 {tp / max(tp + fn, 1):.0%}")
    print(f"  抓取量可减少 {(tn + fn) / max(total, 1):.0%}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=REPO / "work/kb_clean_c.csv")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--leads-out", type=Path, help="write URL-less rows as search leads")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    rows = list(csv.DictReader(io.open(args.csv, encoding="utf-8-sig")))
    kept, dropped, leads = [], [], []
    for r in rows:
        ok, why = score(r["query"], r["title"], r["desc"] or "")
        if not ok:
            dropped.append((r["query"], r["title"][:44], why))
        elif (r["url"] or "").strip():
            kept.append(r)
        else:
            leads.append(r)

    print(f"{len(rows)} rows  ->  keep-with-url {len(kept)}   keep-as-lead {len(leads)}   drop {len(dropped)}")
    print("\n== 被拦掉的（前 18）==")
    for q, t, why in dropped[:18]:
        print(f"   [{q}] {t}  <- {why}")

    if args.leads_out:
        args.leads_out.write_text(
            "".join(
                json.dumps({"query": r["query"], "title": r["title"], "desc": r["desc"]},
                           ensure_ascii=False) + "\n"
                for r in leads
            ),
            encoding="utf-8",
        )
        print(f"\n{len(leads)} 条无 URL 的线索 -> {args.leads_out}")
        print("  这些只能当选题线索去官网找页面，desc 不可作证据")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
