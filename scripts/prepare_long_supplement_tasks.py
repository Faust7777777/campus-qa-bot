#!/usr/bin/env python
"""Prepare targeted supplement tasks for the two long, partially extracted sources."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


TARGETS = {
    "学籍管理规定": (
        "学籍管理规定中本科生休学、复学、退学、保留学籍、请假、旷考、补考的条件和办理要求；跳过已处理的绩点条款",
        "学籍管理规定补充条款（休学复学退学请假旷考补考）",
    ),
    "本科生成绩管理办法": (
        "本科生成绩管理办法中缓考申请、成绩复核与更改、作弊和旷考处理、学业警示条款；跳过已处理的绩点/重修/成绩记入成绩单",
        "本科生成绩管理办法补充条款（缓考复核作弊学业警示）",
    ),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("tasks", type=Path)
    ap.add_argument("mapping", type=Path)
    args = ap.parse_args()
    rows = [json.loads(x) for x in args.source.read_text(encoding="utf-8").splitlines() if x.strip()]
    mapping = {}
    with args.tasks.open("w", encoding="utf-8", newline="\n") as out:
        for row in rows:
            key = next((k for k in TARGETS if k in row.get("title", "")), None)
            if key is None:
                raise SystemExit(f"no supplement target for {row.get('title')}")
            query, title = TARGETS[key]
            original = row["source_id"]
            suffix = hashlib.sha256((original + ":supplement-batch3").encode()).hexdigest()[:20]
            source_id = f"kb_clean:idut_supplement_{suffix}"
            mapping[source_id] = original
            task = {
                "action": "verify_refresh_and_extract",
                "canonical_url": row.get("canonical_url", ""),
                "dataset": "kb_clean",
                "priority": 0,
                "published_at": row.get("published_at"),
                "seed_description": row.get("clean_text", ""),
                "seed_query": query,
                "source_id": source_id,
                "status": "pending",
                "title": title,
            }
            out.write(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n")
    args.mapping.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"source_count": len(rows), "task_count": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
