from __future__ import annotations

import argparse
import json
from pathlib import Path


IDS = {
    "idut-src:4baf821a2fc98744",
    "idut-src:b206ad5744bef3f2",
    "idut-src:c0c0691b777dc02a",
    "idut-src:3eeeb6f98821dfb5",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    rows = [json.loads(line) for line in args.source.open(encoding="utf-8") if line.strip()]
    selected = [row for row in rows if row["source_id"] in IDS]
    args.output.open("w", encoding="utf-8").write("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in selected))
    print(json.dumps({"selected": len(selected), "source_ids": [r["source_id"] for r in selected]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
