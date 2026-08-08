#!/usr/bin/env python
"""Append incremental B to the first production IDUT candidate set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def read(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base", type=Path)
    ap.add_argument("increment", type=Path)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    base, inc = read(args.base), read(args.increment)
    source_ids = [r["source_id"] for r in base]
    card_ids = [c["card_id"] for r in base for c in r.get("candidate_cards", [])]
    for row in inc:
        if row["source_id"] in source_ids:
            raise SystemExit(f"duplicate source_id: {row['source_id']}")
        for card in row.get("candidate_cards", []):
            if card["card_id"] in card_ids:
                raise SystemExit(f"duplicate card_id: {card['card_id']}")
            card_ids.append(card["card_id"])
        source_ids.append(row["source_id"])
    rows = base + inc
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(json.dumps({"base_sources": len(base), "increment_sources": len(inc), "sources": len(rows), "cards": len(card_ids)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
