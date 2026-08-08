#!/usr/bin/env python
"""Map handoff ``idut`` IDs to the production ``kb_clean`` namespace."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("mapping", type=Path)
    args = ap.parse_args()
    rows = []
    mapping = {}
    for line in args.source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        original = row["source_id"]
        suffix = hashlib.sha256(original.encode("utf-8")).hexdigest()[:20]
        production = f"kb_clean:idut_{suffix}"
        row["source_id"] = production
        mapping[production] = original
        rows.append(row)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    args.mapping.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(rows), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
