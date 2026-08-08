# -*- coding: utf-8 -*-
"""
C 类缺口主题提取 (从 chat 原始库补数据)
锚点 = 缺口文档 C 类 + 高频主题, 提取 recommend_data → kb_clean_c.csv
"""

import json
import time
import csv
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

# Live ehall session, kept outside the repository: it holds a logged-in
# session and must never be committed.  Override with EHALL_STATE.
EHALL_STATE = os.getenv("EHALL_STATE", str(Path.home() / "Desktop" / "state.json"))
OUT = str(Path(__file__).resolve().parents[1] / "work" / "kb_clean_c.csv")

ANCHORS = [
    "成绩查询",
    "选课退课",
    "绩点计算",
    "教务系统密码",
    "保研推免",
    "四六级报名",
    "在读证明",
    "毕业论文",
    "出国交换",
    "实习认定",
    "宿舍调换",
    "快递点",
    "校车时刻",
    "空调热水",
    "退费流程",
    "学费核对",
    "选课时间",
    "考试安排",
    "成绩单打印",
    "学生证补办",
]

JS_ASK = r"""
async (question) => {
    const fd = new FormData();
    fd.append('content', question);
    fd.append('history[0][role]', 'user');
    fd.append('history[0][content]', question);
    fd.append('history[1][role]', 'assistant');
    fd.append('history[1][content]', '');
    fd.append('compose_id', '5');
    fd.append('auth_tag', '本科生');
    fd.append('deep_search', '1');
    fd.append('internet_search', '2');
    fd.append('thinking_budget', '1000');
    fd.append('session_id', 'gap_fill');
    fd.append('chat_only_id', 'gap_fill');
    const resp = await fetch('/site/ai/compose_chat', {method: 'POST', body: fd});
    const text = await resp.text();
    const frames = text.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5));
    let recs = [];
    for (const fr of frames) {
        try { const o = JSON.parse(fr); if (o.d && o.d.recommend_data && o.d.recommend_data.length) recs = o.d.recommend_data; } catch(e) {}
    }
    return recs;
}
"""


def load_existing():
    existing = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    existing[row["url"]] = row
        except (OSError, csv.Error):
            pass
    return existing


def save(data):
    try:
        with open(OUT, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["title", "url", "desc", "query"])
            w.writeheader()
            for row in data.values():
                w.writerow(row)
    except OSError as ex:
        print(f"写入失败: {ex}")


def main():
    existing = load_existing()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="zh-CN", storage_state=EHALL_STATE
        )
        page = ctx.new_page()
        page.goto(
            "https://chat.dlut.edu.cn/page/front/Mwelapp/chat?app=5",
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(8000)
        for i, q in enumerate(ANCHORS):
            try:
                recs = page.evaluate(JS_ASK, q)
            except Exception as ex:
                print(f"[{i + 1}/{len(ANCHORS)}] {q} 异常: {str(ex)[:40]}")
                time.sleep(12)
                continue
            added = 0
            for item in recs or []:
                title = (item.get("title") or "").strip()
                url = (item.get("url") or "").strip()
                desc = (item.get("desc") or "").strip()
                key = url if url else f"__no_url__{title}"
                if key not in existing:
                    existing[key] = {
                        "title": title,
                        "url": url,
                        "desc": desc,
                        "query": q,
                    }
                    added += 1
            print(
                f"[{i + 1}/{len(ANCHORS)}] {q}: 召回 {len(recs or [])}, 新增 {added}, 累积 {len(existing)}"
            )
            save(existing)  # 增量保存
            if i < len(ANCHORS) - 1:
                time.sleep(8)
        browser.close()
    print(f"\n完成 → {OUT} ({len(existing)} 条)")


if __name__ == "__main__":
    main()
