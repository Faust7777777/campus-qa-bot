from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("tasks", type=Path)
    args = ap.parse_args()
    rows = [json.loads(line) for line in args.source.open(encoding="utf-8") if line.strip()]
    with args.tasks.open("w", encoding="utf-8", newline="\n") as out:
        for row in rows:
            task = {
                "action": "verify_refresh_and_extract",
                "canonical_url": row.get("canonical_url", ""),
                "dataset": row.get("dataset", "kb_clean"),
                "priority": 0,
                "published_at": row.get("published_at"),
                "seed_description": row.get("clean_text", ""),
                "seed_query": row.get("seed_query", ""),
                "source_id": row["source_id"],
                "status": "pending",
                "title": row.get("title", ""),
            }
            out.write(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"source_count": len(rows), "task_count": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
