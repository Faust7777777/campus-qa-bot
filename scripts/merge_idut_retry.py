#!/usr/bin/env python
"""Merge the completed IDUT retry batch into the reviewed 2026-08-08 cards.

The Luna worker emits temporary ``kb_clean:`` IDs.  The handoff package uses
stable ``idut:``/``idut-src:`` IDs, so this script maps only the four retry
rows, applies the agreed field normalisation, and writes a new artifact.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base", type=Path)
    ap.add_argument("retry", type=Path)
    ap.add_argument("mapping", type=Path)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()

    rows = read_jsonl(args.base)
    retry = read_jsonl(args.retry)
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    existing = {row.get("source_id") for row in rows}
    added = 0
    for row in retry:
        emitted_id = row.get("source_id")
        stable_id = mapping.get(emitted_id)
        if not stable_id:
            raise SystemExit(f"retry source has no mapping: {emitted_id}")
        if stable_id in existing:
            raise SystemExit(f"duplicate source in base/retry: {stable_id}")
        row["source_id"] = stable_id
        for card in row.get("candidate_cards") or []:
            if card.get("card_kind") == "fact" and card.get("validity") == "unknown":
                card["validity"] = "current"
            campus = card.get("campus")
            if campus == "campus-wide":
                card["campus"] = ""
            elif campus == "Panjin":
                card["campus"] = "盘锦"
            elif campus == "Ling Shui":
                card["campus"] = "凌水"
        rows.append(row)
        existing.add(stable_id)
        added += 1

    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(json.dumps({"base_rows": len(rows) - added, "retry_rows_added": added, "output_rows": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
