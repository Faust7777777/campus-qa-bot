# -*- coding: utf-8 -*-
"""
Playwright 兜底抓取器
处理 requests 抓不到的: 403 反爬(站群) / 微信文章 / JS 渲染页
输入: fetch 输出文件中 fetch_status != success 的记录
输出: 同文件更新(成功覆盖失败标记, 仍失败保留)
"""

import json
import time
import hashlib
import datetime
import os
from playwright.sync_api import sync_playwright

BASE = r"<REPO>"
OUT = os.path.join(BASE, "work", "fetch_output_fetch_and_extract.jsonl")


def read_records():
    recs = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            d = json.loads(line)
                            recs[d["source_id"]] = d
                        except (json.JSONDecodeError, KeyError):
                            continue
        except OSError as ex:
            print(f"读取失败: {ex}")
    return recs


def write_records(recs):
    try:
        with open(OUT, "w", encoding="utf-8") as f:
            for d in recs.values():
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
    except OSError as ex:
        print(f"写入失败: {ex}")


def main():
    recs = read_records()
    targets = [r for r in recs.values() if r["fetch_status"] != "success"]
    print(f"待兜底: {len(targets)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="zh-CN",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = ctx.new_page()
        ok = 0
        for i, r in enumerate(targets):
            url = r["canonical_url"]
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2500)
                status = resp.status if resp else 0
                text = page.evaluate('document.body ? document.body.innerText : ""')
                if status == 200 and text and len(text) >= 30:
                    # 简化清洗
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    clean = "\n".join(lines)
                    r["clean_text"] = clean
                    r["content_hash"] = hashlib.sha256(
                        clean.encode("utf-8")
                    ).hexdigest()
                    r["fetch_status"] = "success"
                    r["fetched_at"] = datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat()
                    ok += 1
            except Exception:
                pass  # 保留原失败标记
            if (i + 1) % 5 == 0:
                write_records(recs)
                print(f"  进度 {i + 1}/{len(targets)}")
            time.sleep(0.8)

        browser.close()

    write_records(recs)
    still_fail = [r for r in recs.values() if r["fetch_status"] != "success"]
    print(f"兜底成功 {ok}, 仍失败 {len(still_fail)}")


if __name__ == "__main__":
    main()
