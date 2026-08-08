#!/usr/bin/env python
"""Normalize the targeted long-file supplement cards."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("mapping", type=Path)
    ap.add_argument("production", type=Path)
    ap.add_argument("trace", type=Path)
    args = ap.parse_args()
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    rows = []
    seen: set[str] = set()
    for line in args.source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        for card in row.get("candidate_cards") or []:
            if card["card_id"] in seen:
                raise SystemExit(f"duplicate card_id: {card['card_id']}")
            seen.add(card["card_id"])
            if card.get("card_kind") == "fact":
                card["validity"] = "current"
            if card.get("campus") in ("campus-wide", "全校"):
                card["campus"] = ""
            elif card.get("campus") in ("Ling Shui", "凌水校区"):
                card["campus"] = "凌水"
            elif card.get("campus") in ("Development Zone", "开发区校区"):
                card["campus"] = "开发区"
            elif card.get("campus") in ("Panjin", "盘锦校区"):
                card["campus"] = "盘锦"
            card["audience"] = "本科生"
        rows.append(row)
    traces = []
    for row in rows:
        out = json.loads(json.dumps(row, ensure_ascii=False))
        original = next((value for key, value in mapping.items() if row["source_id"].startswith(key)), None)
        if original:
            out["source_id"] = "idut-supplement:" + original.split(":", 1)[-1]
        traces.append(out)
    args.production.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    args.trace.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in traces), encoding="utf-8")
    print(json.dumps({"rows": len(rows), "cards": len(seen)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
