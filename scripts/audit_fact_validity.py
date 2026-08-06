#!/usr/bin/env python
"""Propose validity corrections for historical fact cards.  Offline, no gateway.

Why only fact cards
-------------------
Navigation validity is assigned mechanically in ``pipeline/catalog.py``: any
indexed article published before the current year becomes ``historical``.  That
looks crude, but the indexed corpus is overwhelmingly college news and notice
archives (public results, award announcements, event write-ups), so the rule is
roughly right there and recovering those cards would add noise, not answers.

Fact cards are different.  Their validity comes from Luna's per-card judgement,
and a standing procedure that merely carries a date in its title - opening
hours, a refund contact, an application requirement - gets marked historical and
becomes permanently unreachable, because a normal query filters on
``validity != 'historical'``.  That is where recoverable material sits.

Two constraints this enforces
-----------------------------
* A recovered fact card must become ``current``, not ``unknown``: review.py
  sends fact cards with unknown validity to PENDING, which are not publishable.
* ``parent_scope_covers_child`` requires ``parent_validity == child_validity``
  exactly, so a card and its parent must move together or the build fails.
  Cards whose parent cannot move are reported as blocked rather than proposed.

Nothing is rewritten in place.  Run without --write to review the proposal.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# A specific instance that has closed.  The content described one cycle and that
# cycle is over.
CLOSED_CYCLE = re.compile(
    r"20\d{2}级|20\d{2}届|20\d{2}年度|[（(]\s*20\d{2}\s*年\s*[）)]"
    r"|20\d{2}\s*[-–—]\s*20\d{2}\s*学年|第[一二三四五六七八九十百]+届"
)
# Superseded every term, so the stored copy is not the current arrangement.
SEASONAL = re.compile(r"寒假|暑假|春节|劳动节|国庆|清明|端午|中秋|元旦|[0-9]{1,2}月[上中下]旬|补测|补缓考")
# A one-off interruption or event.  These read like standing arrangements
# ("座位预约") but describe something that has long since ended.
TEMPORARY = re.compile(r"暂停|暂缓|停用|施工|临时|延期|举办|举行|召开|历史通知|期间.*(调整|安排|暂)")

# --- signals read from the evidence text rather than the title ---------------
# An article of a standing regulation.  A 管理办法 does not expire because the
# notice publishing it carries a date.
REGULATION_ARTICLE = re.compile(r"第[一二三四五六七八九十百]+条")
# A deadline or a scheduled window that has passed.  The content is about *when*
# something happened, so it cannot answer "how do I do this" today.
DATE_BOUND = re.compile(
    r"截止"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日\s*[-–—至到]"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日\s*前"
    r"|于\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|(测试|考试|报到|服务|申报)时间\s*[：:为]"
)
# Describes how something works rather than that something happened.
EVERGREEN = re.compile(
    r"流程|办法|规定|细则|条例|制度|条件|要求|方式|材料|入口|地点|联系|电话|咨询"
    r"|标准|原则|规则|义务|权利|章程|认定|注销|补办|报修|预约|缴费|退费|开馆|开放时间"
)


def classify(title: str, evidence: str = "") -> tuple[str, str]:
    """Return (action, reason).

    Order matters.  A regulation article is decisive whatever the title looks
    like; otherwise a closed cycle beats evergreen wording, and a deadline in
    the evidence beats an evergreen-sounding title.
    """

    if REGULATION_ARTICLE.search(evidence):
        return "recover", "quotes an article of a standing regulation"
    if CLOSED_CYCLE.search(title):
        return "keep", "names a closed cycle"
    if SEASONAL.search(title):
        return "keep", "seasonal arrangement, superseded each term"
    if TEMPORARY.search(title):
        return "keep", "one-off interruption, not a standing arrangement"
    if DATE_BOUND.search(evidence):
        return "keep", "evidence is a deadline or a scheduled window"
    if EVERGREEN.search(title):
        return "recover", "standing procedure, rule or contact"
    return "review", "no decisive signal - needs a human"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reviewed",
        type=Path,
        default=REPO_ROOT / "work/luna_final_reviewed_20260806.jsonl",
    )
    parser.add_argument(
        "--write",
        type=Path,
        help="emit a re-labelled JSONL to this path (never overwrites the input)",
    )
    args = parser.parse_args()

    if args.write and args.write.resolve() == args.reviewed.resolve():
        print("refusing to overwrite the reviewed input", file=sys.stderr)
        return 2

    rows = [
        json.loads(line)
        for line in args.reviewed.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_id = {row["card"]["card_id"]: row for row in rows}

    proposals: dict[str, tuple[str, str]] = {}
    for row in rows:
        card = row["card"]
        if card.get("card_kind") != "fact" or card.get("validity") != "historical":
            continue
        proposals[card["card_id"]] = classify(
            card.get("title", ""), card.get("evidence_quote", "") or ""
        )

    # Parent and child must share a validity, so a card can only move if every
    # relative it is bound to moves with it.
    blocked: list[tuple[str, str]] = []
    for card_id, (action, _reason) in list(proposals.items()):
        if action != "recover":
            continue
        parent_id = by_id[card_id]["card"].get("parent_card_id")
        if not parent_id:
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            blocked.append((card_id, f"parent {parent_id} is not in the reviewed set"))
            proposals[card_id] = ("review", "parent missing")
        elif parent["card"].get("validity") == "historical" and proposals.get(
            parent_id, ("keep", "")
        )[0] != "recover":
            blocked.append((card_id, f"parent {parent_id} would stay historical"))
            proposals[card_id] = ("review", "parent cannot move with it")

    recover = [cid for cid, (action, _) in proposals.items() if action == "recover"]
    keep = [cid for cid, (action, _) in proposals.items() if action == "keep"]
    review = [cid for cid, (action, _) in proposals.items() if action == "review"]

    live_facts = sum(
        1
        for row in rows
        if row["card"].get("card_kind") == "fact"
        and row["card"].get("validity") != "historical"
    )
    print(f"historical fact cards: {len(proposals)}")
    print(f"  recover -> current : {len(recover)}")
    print(f"  keep historical    : {len(keep)}")
    print(f"  needs a human      : {len(review)}")
    if blocked:
        print(f"  (blocked by a parent that cannot move: {len(blocked)})")
    print(f"live fact pool: {live_facts} -> {live_facts + len(recover)}")

    for label, ids in (("RECOVER", recover), ("KEEP", keep), ("REVIEW", review)):
        print(f"\n== {label} ==")
        for card_id in ids:
            card = by_id[card_id]["card"]
            print(f"  {card['title'][:52]:<52}  {proposals[card_id][1]}")

    if args.write:
        for card_id in recover:
            by_id[card_id]["card"]["validity"] = "current"
        args.write.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.write} with {len(recover)} cards moved to current")
        print("review it, then rebuild: the reviewed checksum changes, so review")
        print("report, build report and manifest are all regenerated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
