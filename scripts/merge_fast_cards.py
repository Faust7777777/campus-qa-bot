from __future__ import annotations

import argparse
import glob
import json
from collections import Counter
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("shard_root", type=Path)
    ap.add_argument("prefilled", type=Path)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    rows: dict[str, dict] = {}
    for path in sorted(args.shard_root.parent.glob(args.shard_root.name + "_shard_*/outputs/batch_*.jsonl")):
        for line in path.open(encoding="utf-8"):
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"non-object output: {path}")
                rows[row["source_id"]] = row
    for line in args.prefilled.open(encoding="utf-8"):
        if line.strip():
            row = json.loads(line)
            rows[row["source_id"]] = row
    ordered = [rows[k] for k in sorted(rows)]
    cards = [c["card_id"] for row in ordered for c in row.get("candidate_cards", [])]
    dup_cards = sorted(k for k, n in Counter(cards).items() if n > 1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in ordered:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({
        "source_count": len(ordered),
        "card_count": len(cards),
        "duplicate_card_ids": dup_cards,
        "status_counts": dict(sorted(Counter(r.get("fetch_status") for r in ordered).items())),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
