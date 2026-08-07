from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


OUT_OF_SCOPE = re.compile(
    r"活动|比赛|讲座|会议|喜报|招聘会|招聘|竞赛|展览|演出|宣讲会|表彰|获奖|开幕|闭幕"
)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks", type=Path)
    ap.add_argument("current_output", type=Path)
    ap.add_argument("--model-tasks", type=Path, required=True)
    ap.add_argument("--prefilled", type=Path, required=True)
    args = ap.parse_args()

    current = {row["source_id"]: row for row in read_jsonl(args.current_output)}
    model: list[dict] = []
    prefilled: list[dict] = []
    counts = {"total": 0, "model": 0, "out_of_scope": 0, "failed": 0}
    for task in read_jsonl(args.tasks):
        counts["total"] += 1
        row = current.get(task["source_id"])
        if task["dataset"] == "web_plus_index" and row is not None:
            if row.get("fetch_status") != "success" or not row.get("clean_text"):
                # Preserve an already-known fetch failure without spending a model call.
                out = dict(row)
                out["candidate_cards"] = []
                prefilled.append(out)
                counts["failed"] += 1
                continue
            if OUT_OF_SCOPE.search(task.get("title", "")):
                out = dict(row)
                out["fetch_status"] = "out_of_scope"
                out["candidate_cards"] = []
                out["unresolved_questions"] = []
                prefilled.append(out)
                counts["out_of_scope"] += 1
                continue
            task = dict(task)
            task["action"] = "verify_refresh_and_extract"
            task["seed_description"] = row["clean_text"]
        model.append(task)
        counts["model"] += 1

    # The task package intentionally omits fetch failures; retain them for the
    # final merge so the regenerated card set still covers every fetched row.
    task_ids = {task["source_id"] for task in read_jsonl(args.tasks)}
    for source_id, row in current.items():
        if source_id not in task_ids and row.get("fetch_status") != "success":
            out = dict(row)
            out["candidate_cards"] = []
            prefilled.append(out)
            counts["failed"] += 1

    for path, rows in ((args.model_tasks, model), (args.prefilled, prefilled)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
