#!/usr/bin/env python
"""Apply the handoff-level decisions to incremental batch B."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("mapping", type=Path)
    ap.add_argument("production", type=Path)
    ap.add_argument("original", type=Path)
    args = ap.parse_args()
    reverse = json.loads(args.mapping.read_text(encoding="utf-8"))
    rows: list[dict] = []
    removed: list[str] = []
    seen_cards: set[str] = set()
    for line in args.source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        kept = []
        for card in row.get("candidate_cards") or []:
            # The undergraduate grade-management notice repeats only the GPA
            # formula; the student-status regulation has the same formula plus
            # the complete score-to-GPA table, so retain the latter as the
            # authoritative card and drop the near-duplicate.
            if card.get("card_id") == "card_idut_f197_gpa_formula":
                removed.append(card["card_id"])
                continue
            if card.get("card_id") == "card_idut_4f2855a2f1e1c4ff9814_gpa":
                # The first batch already carries the general GPA formula.
                # Keep this stronger regulation source for the missing
                # score-to-point table as a separate, non-duplicate fact.
                card["card_id"] = "card_idut_4f2855a2f1e1c4ff9814_grade_point_mapping"
                card["title"] = "成绩与绩点对应关系"
                card["standard_question"] = "各分数对应的绩点是多少？"
                card["summary"] = "学校规定了百分制成绩与成绩绩点的对应区间：100分为5.0，90－99分对应4.0-4.9，80－89分对应3.0-3.9，70－79分对应2.0-2.9，60－69分对应1.0-1.9，0－59分为0。"
                card["retrieval_text"] = "本科生百分制成绩与成绩绩点对应关系：100分5.0；90－99分4.0-4.9；80－89分3.0-3.9；70－79分2.0-2.9；60－69分1.0-1.9；0－59分为0。"
                card["facts"] = {"成绩绩点对应关系": {"100": "5.0", "90－99": "4.0-4.9", "80－89": "3.0-3.9", "70－79": "2.0-2.9", "60－69": "1.0-1.9", "0－59": "0"}}
                card["facets"] = ["成绩", "规则"]
                card["generated_questions"] = ["各分数对应的绩点是多少？", "90分到99分的绩点范围是多少？", "绩点对照表在哪里？"]
                card["aliases"] = ["成绩绩点", "绩点对照表", "GPA对照"]
                card["subject_key"] = "undergraduate_grade_point_mapping"
                card["fact_key"] = "grade_point_mapping"
                # Narrow the quote to the mapping section, which remains an
                # exact continuous substring of this source's clean_text.
                marker = "（一）成绩与绩点对应关系如下："
                start = row["clean_text"].find(marker)
                end = row["clean_text"].find("第三十一条", start)
                if start >= 0 and end > start:
                    card["evidence_quote"] = row["clean_text"][start:end].rstrip()
            if card.get("card_id") in seen_cards:
                raise SystemExit(f"duplicate card_id: {card['card_id']}")
            seen_cards.add(card["card_id"])
            if card.get("card_kind") == "fact" and card.get("validity") == "unknown":
                card["validity"] = "current"
            campus = card.get("campus")
            if campus == "campus-wide":
                card["campus"] = ""
            elif campus == "Panjin":
                card["campus"] = "盘锦"
            elif campus == "Ling Shui":
                card["campus"] = "凌水"
            kept.append(card)
        row["candidate_cards"] = kept
        rows.append(row)

    # Collected output already uses temporary kb_clean IDs.  Keep that as the
    # production artifact, while also emitting a traceable original-ID copy.
    production_rows = rows
    original_rows = []
    for row in rows:
        copy_row = json.loads(json.dumps(row, ensure_ascii=False))
        copy_row["source_id"] = reverse.get(row["source_id"], row["source_id"])
        original_rows.append(copy_row)
    args.production.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in production_rows),
        encoding="utf-8",
    )
    args.original.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in original_rows),
        encoding="utf-8",
    )
    print(json.dumps({"rows": len(rows), "cards": len(seen_cards), "removed": removed}, ensure_ascii=False))


if __name__ == "__main__":
    main()
