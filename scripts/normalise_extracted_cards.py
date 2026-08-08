#!/usr/bin/env python
"""Fix the two extraction mistakes that a build cannot survive or cannot see.

Written as a script rather than done by hand because both have now arrived
twice: repairing a delivery in place does not help when the next delivery is
rebuilt from the extractor's own output.

1. A card with a literal quote is a fact card, whatever it is called.  Cards for
   "how to get in" were labelled navigation while carrying an evidence_quote
   that answers the question; navigation means "title and URL only", and the
   builder rejects a navigation card that has any body at all - BuildError,
   whole batch fails.

2. validity asks whether the content still holds, not whether a publication
   date could be found.  Standing rules and system paths were marked unknown
   because their page had no published_at, and historical because it had one.
   A card marked historical stays in the database and never surfaces again, and
   nothing reports it.
"""

from __future__ import annotations

import argparse
import io
import json
import re
from pathlib import Path

DATED = re.compile(r"20\d{2}\s*[-–—/年]|20\d{2}\s*级|20\d{2}\s*届|学年|本学期|上学期|下学期")


def normalise(rows: list[dict]) -> dict[str, list[str]]:
    log: dict[str, list[str]] = {"promoted": [], "emptied": [], "revalidated": []}
    for row in rows:
        body = row.get("clean_text", "")
        for card in row.get("candidate_cards") or []:
            quote = (card.get("evidence_quote") or "").strip()
            if card["card_kind"] == "navigation":
                if quote and quote in body:
                    card["card_kind"] = "fact"
                    card.setdefault("subject_key", card["card_id"].replace("card_", "")[:40])
                    if not card.get("subject_key"):
                        card["subject_key"] = card["card_id"].replace("card_", "")[:40]
                    if not card.get("fact_key"):
                        card["fact_key"] = "entry"
                    log["promoted"].append(card["card_id"])
                else:
                    if any(card.get(f) for f in ("summary", "evidence_quote", "source_locator")) \
                            or card.get("facts") or card.get("facets"):
                        log["emptied"].append(card["card_id"])
                    card["summary"] = ""
                    card["evidence_quote"] = ""
                    card["source_locator"] = ""
                    card["facts"] = {}
                    card["facets"] = []
            if card["validity"] == "unknown":
                # No stated expiry means it still holds; a year or a cohort in
                # the text is what makes something historical.
                card["validity"] = "historical" if DATED.search(quote or card.get("title", "")) else "current"
                log["revalidated"].append(f"{card['card_id']} unknown->{card['validity']}")
            elif card["validity"] == "historical" and not DATED.search(quote or card.get("title", "")):
                card["validity"] = "current"
                log["revalidated"].append(f"{card['card_id']} historical->current")
    return log


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows = [json.loads(line) for line in io.open(args.source, encoding="utf-8") if line.strip()]
    log = normalise(rows)
    io.open(args.output, "w", encoding="utf-8", newline="").write(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")

    cards = [c for r in rows for c in (r.get("candidate_cards") or [])]
    print(f"{len(rows)} 条源，{len(cards)} 张卡 -> {args.output}")
    for name, label in (("promoted", "导航卡转为事实卡（带逐字证据）"),
                        ("emptied", "导航卡清空多余字段"),
                        ("revalidated", "validity 改判")):
        if log[name]:
            print(f"  {label} {len(log[name])} 张")
            for item in log[name]:
                print(f"     {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
