from __future__ import annotations

"""Freeze a reviewed evaluation draft without changing its bytes.

The runtime evaluator consumes the JSONL itself.  This small command creates a
byte-for-byte copy and a separate approval record so that a human confirmation
does not silently regenerate or reorder the cases.
"""

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from luna_kb.pipeline.evaluate import load_evaluation_set


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze(source: Path, output: Path, report: Path, approval: str) -> dict[str, object]:
    source_hash = sha256(source)
    items = load_evaluation_set(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output)
    output_hash = sha256(output)
    if source_hash != output_hash:
        raise RuntimeError("frozen evaluation set is not byte-identical to the source")
    counts: dict[str, int] = {}
    for item in items:
        kind = str(item["kind"])
        counts[kind] = counts.get(kind, 0) + 1
    record: dict[str, object] = {
        "frozen": True,
        "approved_by": approval,
        "frozen_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_draft": str(source),
        "source_draft_sha256": source_hash,
        "evaluation_set": str(output),
        "evaluation_set_sha256": output_hash,
        "question_count": len(items),
        "kind_counts": dict(sorted(counts.items())),
        "byte_identical_to_source": True,
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze a confirmed evaluation-set draft")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--approval", default="user-confirmed")
    args = parser.parse_args()
    print(json.dumps(freeze(args.source, args.output, args.report, args.approval), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
