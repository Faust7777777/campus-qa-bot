from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


TARGETS = {
    "账号充值后仍无法上网": "校园网充值后仍无法上网怎么排查",
    "成绩单打印与重修办理": "重修怎么申请、成绩单怎么开",
    "学生平均学分绩点计算方法": "绩点怎么算",
    "勤工助学岗位对接": "勤工助学岗位在哪报名",
    "勤工助学岗位对接(第二篇)": "勤工助学岗位在哪报名",
    "成绩单打印平台介绍": "自助打印能打哪些证明、机器在哪",
    "校园网无线连接与认证故障处理办法": "连上校园网但打不开网页怎么办",
    "统一身份认证帮助页": "统一身份认证密码怎么找回、手机号怎么验证或换绑",
    "学生证打印自助": "学生证如何补打",
    "2024级本科新生校园网络认证说明": "2024级新生校园网首次认证",
    "校园门户CampusPortal介绍": "一网通办入口在哪",
    "信息化基础平台介绍": "一网通办/办事大厅能办哪些事",
    "统一身份认证服务介绍": "统一身份认证账号是什么、手机号怎么绑定",
    "图书馆入馆指南通知": "借书是否需要先通过入馆测试",
    "家庭经济困难学生认定申请表": "困难认定/助学金在智慧学工怎么提交",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("tasks", type=Path)
    ap.add_argument("mapping", type=Path)
    args = ap.parse_args()
    mapping = {}
    rows = [json.loads(line) for line in args.source.open(encoding="utf-8") if line.strip()]
    with args.tasks.open("w", encoding="utf-8", newline="\n") as out:
        for row in rows:
            original = row["source_id"]
            suffix = hashlib.sha256(original.encode("utf-8")).hexdigest()[:20]
            temp_id = f"kb_clean:idut_{suffix}"
            mapping[temp_id] = original
            target = next((v for k, v in TARGETS.items() if k in row["title"]), row["title"])
            task = {
                "action": "verify_refresh_and_extract",
                "canonical_url": row.get("canonical_url", ""),
                "dataset": "kb_clean",
                "priority": 0,
                "published_at": row.get("published_at"),
                "seed_description": row.get("clean_text", ""),
                "seed_query": target,
                "source_id": temp_id,
                "status": "pending",
                "title": row.get("title", ""),
            }
            out.write(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n")
    args.mapping.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"source_count": len(rows), "task_count": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
