# -*- coding: utf-8 -*-
"""
Luna 任务抓取执行器 (fetch_and_extract / fetch_and_classify_current)

读取 luna_tasks.jsonl 中指定的 action 任务 → requests 抓取 → 清洗正文 →
按 luna-worker-protocol 输出 JSONL（增量保存，失败标 fetch_failed）

用法:
  python3 fetch_executor.py fetch_and_extract [limit]
  python3 fetch_executor.py fetch_and_classify_current [limit]
"""

import json
import sys
import time
import hashlib
import re
import datetime
import os
from pathlib import Path
import requests

BASE = str(Path(__file__).resolve().parents[1])
TASKS = os.path.join(BASE, "work", "luna_tasks.jsonl")
OUT = os.path.join(BASE, "work", "fetch_output_{action}.jsonl")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
INTERVAL = 0.4  # 秒, 礼貌抓取

# 常见正文容器（按优先级）
CONTAINERS = [
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
    "show-content",
]


def extract_main_body(html):
    """从 HTML 提取正文文本（多容器回退 + 微信专用 + 通用兜底）"""
    # 微信文章专用: js_content
    m = re.search(
        r'<div[^>]*class=["\'][^"\']*js_content[^"\']*["\'][^>]*>([\s\S]*?)</div>\s*(?:<script|<div[^>]*id=["\']js_pc_)',
        html,
    )
    if not m:
        m = re.search(
            r'<div[^>]*id=["\']js_content["\'][^>]*>([\s\S]*?)<div[^>]*id=["\']js_sg_bar',
            html,
        )
    if not m:
        m = re.search(
            r'<div[^>]*class=["\'][^"\']*rich_media_content[^"\']*["\'][^>]*>([\s\S]*?)</div>',
            html,
        )
    if m:
        frag = m.group(1)
    else:
        # 常规容器尝试: 取内容最长的容器(避免误匹配导航/空容器)
        frag = None
        best_len = 50
        for cls in CONTAINERS:
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
                if len(tmp) > best_len:
                    frag = m.group(1)
                    best_len = len(tmp)
        if frag is None:
            # 兜底: 全文本清洗 + 去导航噪声行
            frag = re.sub(
                r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<!--[\s\S]*?-->",
                " ",
                html,
            )
            frag = re.sub(
                r"<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", frag, flags=re.I
            )
            frag = re.sub(r"<[^>]+>", " ", frag)
            frag = re.sub(r"[ \t\xa0]+", " ", frag)
            frag = re.sub(r"\n\s*\n+", "\n", frag)
            lines = [l.strip() for l in frag.split("\n") if l.strip()]
            noise = re.compile(
                r"^(首页|上页|下页|尾页|上一条|下一条|当前位置|友情链接|版权所有|Copyright|地址|邮编|点击次数|浏览量|发布时间|发布者|来源|作者|编辑|返回|网站地图|联系我们|主办单位|协办单位|Tel|Email|邮箱)"
            )
            frag = "\n".join(l for l in lines if not noise.match(l))
            return frag[:8000].strip()
    # 去掉脚本/样式/注释
    frag = re.sub(
        r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<!--[\s\S]*?-->", " ", frag
    )
    frag = re.sub(r"<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", frag, flags=re.I)
    frag = re.sub(r"<[^>]+>", " ", frag)
    frag = re.sub(r"[ \t\xa0]+", " ", frag)
    frag = re.sub(r"\n\s*\n+", "\n", frag)
    # 去常见页脚噪声
    frag = re.sub(
        r"(责任编辑|审核|发布人|编辑|阅读原文|Copyright|版权所有)[：:].*", "", frag
    )
    return frag.strip()


def fetch_url(url):
    """抓取 URL, 返回 (status, clean_text)"""
    headers = dict(HEADERS)
    if "mp.weixin.qq.com" in url:
        headers["Referer"] = "https://mp.weixin.qq.com/"
    try:
        r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        r.encoding = r.apparent_encoding or "utf-8"
        if r.status_code != 200:
            return r.status_code, ""
        text = extract_main_body(r.text)
        return 200, text
    except (requests.RequestException, ValueError, TypeError):
        return 0, ""


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "fetch_and_extract"
    retry_failed = "--retry-failed" in sys.argv
    try:
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    except ValueError:
        limit = 0

    tasks = []
    try:
        with open(TASKS, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("action") == action and d.get("canonical_url"):
                    tasks.append(d)
    except OSError as ex:
        print(f"读取任务失败: {ex}")
        return
    if limit > 0:
        tasks = tasks[:limit]
    print(f"任务数: {len(tasks)} ({action})")

    out_path = OUT.format(action=action)
    done_ids = set()
    failed_ids = set()
    try:
        with open(out_path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec["fetch_status"] == "success":
                        done_ids.add(rec["source_id"])
                    else:
                        failed_ids.add(rec["source_id"])
                except (json.JSONDecodeError, KeyError):
                    pass
    except OSError:
        pass
    if retry_failed:
        # 重跑失败: 清空失败记录, 允许重写
        tasks = [t for t in tasks if t["source_id"] in failed_ids]
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                lines = [
                    l
                    for l in f
                    if not (
                        (lambda r: r.get("source_id") in failed_ids)(json.loads(l))
                        if l.strip()
                        else False
                    )
                ]
            with open(out_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except (OSError, json.JSONDecodeError):
            pass

    skipped = 0
    results_written = 0
    try:
        out = open(out_path, "a", encoding="utf-8")
    except OSError as ex:
        print(f"打开输出失败: {ex}")
        return
    with out:
        for i, t in enumerate(tasks):
            sid = t["source_id"]
            if sid in done_ids:
                skipped += 1
                continue
            url = t["canonical_url"]
            status, text = fetch_url(url)
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            if status == 200 and len(text) >= 30:
                fstatus = "success"
            else:
                fstatus = "fetch_failed"
                text = ""
            rec = {
                "source_id": sid,
                "dataset": t.get("dataset", ""),
                "canonical_url": url,
                "title": t.get("title", ""),
                "official_domain": url.split("/")[2] if "//" in url else "",
                "published_at": t.get("published_at"),
                "fetched_at": now,
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()
                if text
                else "",
                "clean_text": text,
                "fetch_status": fstatus,
                "candidate_cards": [],
                "unresolved_questions": [],
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            results_written += 1
            if (i + 1) % 10 == 0 or i == len(tasks) - 1:
                print(
                    f"  进度 {i + 1}/{len(tasks)} (跳过 {skipped}, 写出 {results_written})"
                )
            time.sleep(INTERVAL)

    print(f"完成: 写出 {results_written}, 跳过已处理 {skipped} → {out_path}")


if __name__ == "__main__":
    main()
