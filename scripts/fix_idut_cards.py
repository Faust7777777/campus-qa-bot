from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    changed_validity = 0
    changed_campus = 0
    rows = []
    for line in args.source.open(encoding="utf-8"):
        if not line.strip():
            continue
        row = json.loads(line)
        for card in row.get("candidate_cards", []):
            if card.get("card_kind") == "fact" and card.get("validity") == "unknown":
                card["validity"] = "current"
                changed_validity += 1
            if card.get("campus") == "campus-wide":
                card["campus"] = ""
                changed_campus += 1
            elif card.get("campus") == "Panjin":
                card["campus"] = "盘锦"
                changed_campus += 1
            elif card.get("campus") == "Ling Shui":
                card["campus"] = "凌水"
                changed_campus += 1
        rows.append(row)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"rows": len(rows), "validity_fixed": changed_validity, "campus_fixed": changed_campus}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
