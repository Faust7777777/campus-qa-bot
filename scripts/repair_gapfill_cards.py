#!/usr/bin/env python
"""Make the gapfill extraction usable: re-judge validity, drop off-topic cards.

Two defects, both mechanical, neither Luna's doing alone:

  over-marked historical (14 cards)
      The handoff said "describes a specific term or deadline -> historical",
      and Luna applied it to the notice's *title*.  But a notice titled
      "2025-2026学年第一学期选课工作安排" can carry a standing rule - "退课后
      系统在每天中午12:00释放前一天的容量" is how it always works.  Marked
      historical, that card is invisible to every normal query, so the answer
      was fetched, extracted, and then hidden.  The test belongs on the
      evidence, not the title: a hard date in the quote means the quote is
      about when something happened; a rule without one is how it works.

  off-topic current (8 of 10 live cards)
      Search drift upstream: "退费" matched a warning about refund-scam apps,
      "教务系统密码" matched mailbox two-factor setup.  Luna extracted these
      faithfully - the pages really do say that - but they answer a different
      question than the one they were fetched for, and a live card that
      answers "退费流程" with an anti-fraud notice is worse than no card.

Every decision is printed.  Nothing is rewritten in place.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from luna_kb.contracts import canonicalize_url  # noqa: E402

sys.path.insert(0, str(REPO / "scripts"))
from filter_search_leads import TOPIC_ANCHORS, best_topic  # noqa: E402

# A quote pinned to a moment: it records when something happened.
DATED_EVIDENCE = re.compile(
    r"\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|\d{4}\s*年\s*\d{1,2}\s*月"
    r"|截止(日期|时间)?[：:]"
    r"|报名时间[：:]"
)
# A quote describing how the thing works, whenever you ask.
STANDING_EVIDENCE = re.compile(
    r"第[一二三四五六七八九十百]+条"
    r"|原则上|一般为|不超过|须|应当|均需|统一|即可|可在|可通过|登录.{0,12}系统"
    r"|流程|办法|规定|标准|上限|时间是|供水时间|运行时间|票价"
)


def rejudge(title: str, evidence: str, current: str) -> tuple[str, str]:
    """Return (validity, why).  Only ever moves historical -> current."""

    if current != "historical":
        return current, ""
    if DATED_EVIDENCE.search(evidence):
        return "historical", "证据锚定在具体日期"
    if STANDING_EVIDENCE.search(evidence):
        return "current", "证据是常设规则/流程，标题里的学年不改变这一点"
    return "historical", "无常设特征"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", type=Path,
                        default=REPO / "work/luna_gapfill_cards_20260807_reviewed.jsonl")
    parser.add_argument("--tasks", type=Path,
                        default=REPO / "work/luna_tasks_gapfill_20260807.jsonl")
    parser.add_argument("--out", type=Path,
                        default=REPO / "work/luna_gapfill_cards_20260807_repaired.jsonl")
    args = parser.parse_args()

    seed = {}
    for line in args.tasks.read_text(encoding="utf-8").splitlines():
        if line.strip():
            t = json.loads(line)
            seed[canonicalize_url(t["canonical_url"])] = t.get("seed_query", "")

    rows = [json.loads(l) for l in args.cards.read_text(encoding="utf-8").splitlines() if l.strip()]

    promoted, dropped, kept_live, unchanged = [], [], [], 0
    for row in rows:
        query = seed.get(canonicalize_url(row["canonical_url"]), "")
        survivors = []
        for card in row.get("candidate_cards") or []:
            blob = f"{card['title']}\n{card.get('evidence_quote', '')}"
            if card["card_kind"] != "fact":
                survivors.append(card)
                continue

            topic = best_topic(blob, query)
            if topic is None:
                dropped.append((query, card["title"], "与所属缺口主题无关"))
                continue

            before = card.get("validity")
            after, why = rejudge(card["title"], card.get("evidence_quote", ""), before)
            if after != before:
                card["validity"] = after
                promoted.append((query, card["title"], why))
            elif before == "current":
                unchanged += 1
            if card["validity"] == "current":
                kept_live.append((topic, card["title"]))
            survivors.append(card)

        # parent and child must share a validity or the build refuses the release
        by_id = {c["card_id"]: c for c in survivors}
        for card in survivors:
            parent_id = card.get("parent_card_id")
            if parent_id and parent_id in by_id:
                if by_id[parent_id].get("validity") != card.get("validity"):
                    card["validity"] = by_id[parent_id]["validity"]
        row["candidate_cards"] = survivors

    args.out.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )

    print(f"== 提升为 current：{len(promoted)} ==")
    for q, t, why in promoted:
        print(f"   [{q}] {t[:44]}")
        print(f"        {why}")
    print(f"\n== 丢弃（跑题）：{len(dropped)} ==")
    for q, t, why in dropped:
        print(f"   [{q}] {t[:44]}  <- {why}")

    total_cards = sum(len(r.get("candidate_cards") or []) for r in rows)
    print(f"\n== 结果 ==")
    print(f"   卡片 {total_cards}（原 50，丢弃 {len(dropped)}）")
    print(f"   fact+current 净增量: {len(kept_live)}（原 10，其中 8 张跑题）")
    print(f"   覆盖缺口: {len(set(t for t, _ in kept_live))} 个")
    for topic, n in Counter(t for t, _ in kept_live).most_common():
        print(f"      {n:>2}  {topic}")
    print(f"\n   -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
