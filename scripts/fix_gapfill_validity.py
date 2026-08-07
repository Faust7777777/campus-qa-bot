from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    changed: list[str] = []
    rows = []
    for line in args.source.open(encoding="utf-8"):
        if not line.strip():
            continue
        row = json.loads(line)
        for card in row.get("candidate_cards", []):
            if card.get("card_kind") == "fact" and card.get("validity") == "unknown":
                card["validity"] = "current"
                changed.append(card.get("card_id", ""))
        rows.append(row)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"rows": len(rows), "fact_unknown_to_current": len(changed), "card_ids": changed}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
