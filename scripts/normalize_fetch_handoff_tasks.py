from __future__ import annotations

import argparse
import json
from pathlib import Path

from luna_kb.contracts import stable_id


def normalize(src: Path, dst: Path) -> dict[str, int]:
    counts = {"total": 0, "search_rewritten": 0}
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("r", encoding="utf-8") as inp, dst.open(
        "w", encoding="utf-8", newline="\n"
    ) as out:
        for line_no, line in enumerate(inp, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            counts["total"] += 1
            # Search-source fetches are KB material, not a separate card type.
            # Put them in the same kb_clean lane and derive a deterministic ID
            # from the canonical URL, matching the normal task ID convention.
            if str(row.get("source_id", "")).startswith("search:"):
                url = str(row.get("canonical_url") or "").strip()
                identity = url or "\x1f".join(
                    [str(row.get("title") or ""), str(row.get("seed_description") or "")]
                )
                row["dataset"] = "kb_clean"
                row["source_id"] = f"kb_clean:{stable_id('src', identity)}"
                counts["search_rewritten"] += 1
            out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("src", type=Path)
    parser.add_argument("dst", type=Path)
    args = parser.parse_args()
    print(json.dumps(normalize(args.src, args.dst), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
