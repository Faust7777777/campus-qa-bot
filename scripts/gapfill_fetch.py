# -*- coding: utf-8 -*-
"""
缺口补卡抓取执行器 (handoff-kb-gapfill-20260807)
读 work/luna_tasks_gapfill_20260807.jsonl (142 条)
- 静态页/微信: requests
- ehall SPA: Playwright + 登录态 (先试, 失败标 fetch_failed 不硬耗)
产出: work/fetch_output_gapfill_20260807.jsonl (LunaSourceResult, candidate_cards 空)

对齐专家契约:
- clean_text 截断 16000 (search_text 硬顶)
- content_hash = sha256(clean_text), 必须一致
- official_domain = URL host
- 不抓 web_plus_index 站群目录 (任务清单已排除)
"""

import json
import sys
import time
import hashlib
import re
import datetime
import os
import requests
from playwright.sync_api import sync_playwright

BASE = r"<REPO>"
TASKS = os.path.join(BASE, "work", "luna_tasks_gapfill_20260807.jsonl")
OUT = os.path.join(BASE, "work", "fetch_output_gapfill_20260807.jsonl")
STATE = r"<DESKTOP>\state.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}
MAX_TEXT = 16000
INTERVAL = 0.4


def extract_body(html):
    """多容器 + 全文本兜底, 返回清洗正文"""
    frag = None
    for cls in [
        "v_news_content",
        "vsb_content",
        "article-content",
        "article",
        "content",
        "js_content",
        "wp_articlecontent",
        "news_content",
        "TextContent",
        "main",
        "entry-content",
        "post-content",
        "text",
        "detail",
    ]:
        m = re.search(
            r'<(?:div|section|article)[^>]*?(?:class|id)=["\'][^"\']*'
            rf'{cls}[^"\']*["\'][^>]*>([\s\S]*?)</(?:div|section|article)>',
            html,
            re.I,
        )
        if m:
            tmp = re.sub(
                r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", m.group(1)
            )
            tmp = re.sub(r"<[^>]+>", " ", tmp).strip()
            if len(tmp) > 50 and (
                frag is None or len(tmp) > len(re.sub(r"<[^>]+>", " ", frag))
            ):
                frag = m.group(1)
    if frag is None:
        frag = html
    frag = re.sub(
        r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<!--[\s\S]*?-->", " ", frag
    )
    frag = re.sub(r"<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", frag, flags=re.I)
    frag = re.sub(r"<[^>]+>", " ", frag)
    frag = re.sub(r"[ \t\xa0]+", " ", frag)
    frag = re.sub(r"\n\s*\n+", "\n", frag)
    noise = re.compile(
        r"^(首页|上页|下页|尾页|上一条|下一条|当前位置|友情链接|版权所有|Copyright|地址|邮编|点击次数|浏览量|发布时间|发布者|来源|作者|编辑|返回|网站地图|联系我们|主办单位|协办单位|Tel|Email|邮箱)"
    )
    lines = [
        l.strip() for l in frag.split("\n") if l.strip() and not noise.match(l.strip())
    ]
    return "\n".join(lines)[:MAX_TEXT].strip()


def fetch_requests(url):
    headers = dict(HEADERS)
    if "mp.weixin.qq.com" in url:
        headers["Referer"] = "https://mp.weixin.qq.com/"
    try:
        r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        r.encoding = r.apparent_encoding or "utf-8"
        if r.status_code != 200:
            return r.status_code, ""
        return 200, extract_body(r.text)
    except (requests.RequestException, ValueError, TypeError):
        return 0, ""


def main():
    tasks = []
    try:
        with open(TASKS, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        tasks.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError as ex:
        print(f"读取任务失败: {ex}")
        return

    done_ids = set()
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            done_ids.add(json.loads(line)["source_id"])
                        except (json.JSONDecodeError, KeyError):
                            pass
        except OSError:
            pass

    ehall_tasks = [t for t in tasks if "ehall" in t.get("canonical_url", "")]
    rest_tasks = [t for t in tasks if "ehall" not in t.get("canonical_url", "")]
    print(f"任务: ehall {len(ehall_tasks)} / 其他 {len(rest_tasks)}")

    written = 0

    def write_rec(rec):
        nonlocal written
        try:
            with open(OUT, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1
        except OSError:
            pass

    def build_rec(t, status, text):
        return {
            "source_id": t["source_id"],
            "dataset": t.get("dataset", "kb_clean"),
            "canonical_url": t["canonical_url"],
            "title": t.get("title", ""),
            "official_domain": t["canonical_url"].split("/")[2]
            if "//" in t["canonical_url"]
            else "",
            "published_at": t.get("published_at"),
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()
            if text
            else "",
            "clean_text": text,
            "fetch_status": status,
            "candidate_cards": [],
            "unresolved_questions": [],
        }

    # 1) 非 ehall: requests
    for i, t in enumerate(rest_tasks):
        if t["source_id"] in done_ids:
            continue
        status, text = fetch_requests(t["canonical_url"])
        fstatus = "success" if status == 200 and len(text) >= 30 else "fetch_failed"
        write_rec(build_rec(t, fstatus, text if fstatus == "success" else ""))
        if (i + 1) % 20 == 0:
            print(f"  其他 {i + 1}/{len(rest_tasks)}, 已写 {written}")
        time.sleep(INTERVAL)

    # 2) ehall: Playwright 试抓
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="zh-CN", storage_state=STATE)
        page = ctx.new_page()
        try:
            page.goto(
                "https://ehall.dlut.edu.cn/",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            page.wait_for_timeout(3000)
        except Exception:
            pass
        for i, t in enumerate(ehall_tasks):
            if t["source_id"] in done_ids:
                continue
            try:
                resp = page.goto(
                    t["canonical_url"], wait_until="domcontentloaded", timeout=15000
                )
                page.wait_for_timeout(2500)
                status = resp.status if resp else 0
                text = page.evaluate('document.body ? document.body.innerText : ""')
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                clean = "\n".join(lines)[:MAX_TEXT].strip()
                if status == 200 and len(clean) >= 30:
                    fstatus = "success"
                else:
                    fstatus = "fetch_failed"
                    clean = ""
            except Exception:
                fstatus, clean = "fetch_failed", ""
            write_rec(build_rec(t, fstatus, clean))
            print(f"  ehall {i + 1}/{len(ehall_tasks)}: {fstatus} | {t['title'][:25]}")
            time.sleep(1.0)
        browser.close()

    print(f"\n完成: 共写 {written} 条 → {OUT}")


if __name__ == "__main__":
    main()
