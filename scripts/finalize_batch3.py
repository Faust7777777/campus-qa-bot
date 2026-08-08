#!/usr/bin/env python
"""Apply batch3 validity/campus decisions and emit production + trace copies."""

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
    rows = []
    seen: set[str] = set()
    for line in args.source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        is_historical_source = "2025级新生医疗保险" in row.get("title", "")
        for card in row.get("candidate_cards") or []:
            if card.get("card_id") in seen:
                raise SystemExit(f"duplicate card_id: {card['card_id']}")
            seen.add(card["card_id"])
            if card.get("card_kind") == "fact":
                card["validity"] = "historical" if is_historical_source else "current"
                if card.get("card_id") == "card_8345-dates":
                    # A dated 2026 selection window is historical after the
                    # window closes; the contact/process card remains current.
                    card["validity"] = "historical"
            campus = card.get("campus")
            if campus in ("campus-wide", "全校"):
                card["campus"] = ""
            elif campus in ("Ling Shui", "凌水校区"):
                card["campus"] = "凌水"
            elif campus in ("Development Zone", "开发区校区"):
                card["campus"] = "开发区"
            elif campus in ("Panjin", "盘锦校区"):
                card["campus"] = "盘锦"
            if card.get("audience") != "本科生":
                card["audience"] = "本科生"
        rows.append(row)
    original_rows = []
    for row in rows:
        copy_row = json.loads(json.dumps(row, ensure_ascii=False))
        copy_row["source_id"] = reverse.get(row["source_id"], row["source_id"])
        original_rows.append(copy_row)
    args.production.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    args.original.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in original_rows), encoding="utf-8")
    print(json.dumps({"rows": len(rows), "cards": len(seen)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
