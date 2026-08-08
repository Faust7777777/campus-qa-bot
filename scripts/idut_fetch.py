# -*- coding: utf-8 -*-
"""
蒸馏交接包抓取执行器 (campus-qa-蒸馏交接包-20260808)
抓取"仅导航链接"10 条对应的 13 个官方 URL 正文
产出: work/fetch_output_idut_20260808.jsonl
"""

import json
import time
import hashlib
import re
import datetime
import os
import requests
from playwright.sync_api import sync_playwright

BASE = r"C:\Users\15892\Desktop\campus-qa-bot"
OUT = os.path.join(BASE, "work", "fetch_output_idut_20260808.jsonl")
STATE = r"C:\Users\15892\Desktop\state.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}
MAX_TEXT = 16000

# 导航卡 -> 官方 URL (13 条)
SOURCES = [
    ("勤工助学岗位对接", "https://mp.weixin.qq.com/s/hiFNe__7JTlYP5emNpzJkA"),
    ("校园卡微信支付宝使用范围", "https://ecard.dlut.edu.cn/info/1013/1131.htm"),
    ("校园网账号错误DNS排查", "https://its.dlut.edu.cn/info/2050/63966.htm"),
    ("学生平均学分绩点计算方法", "https://mp.weixin.qq.com/s/vsUHRpBk_c_ofyoFyUYyaw"),
    ("学生证打印自助", "https://ecard.dlut.edu.cn/info/1013/1393.htm"),
    ("校园网认证页面打不开的处理办法", "https://its.dlut.edu.cn/info/2050/63966.htm"),
    ("成绩单打印平台介绍", "https://mp.weixin.qq.com/s/dswLYwYPZFNw-PR-Dpi5tQ"),
    ("账号充值后仍无法上网", "https://mp.weixin.qq.com/s/6bOYuXkGpJYqZFrss7BLxA"),
    (
        "2024级本科新生校园网络认证说明",
        "http://business.dlut.edu.cn/info/1241/3081.htm",
    ),
    ("勤工助学岗位对接(第二篇)", "https://mp.weixin.qq.com/s/cbnlY1_EHLzOdOttgi0I-w"),
    ("成绩单打印与重修办理", "https://drise.dlut.edu.cn/info/1241/4545.htm"),
    (
        "校园网充值",
        "https://ehall.dlut.edu.cn/fp/visitService?service_id=5a697a84-43a2-404",
    ),
    ("校园网账号出现在陌生设备时的处理", "https://its.dlut.edu.cn/info/2050/63966.htm"),
]


def extract_body(html):
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
            if len(tmp) > 50:
                frag = m.group(1)
                break
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


def main():
    done = set()
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            done.add(json.loads(line)["source_id"])
                        except (json.JSONDecodeError, KeyError):
                            pass
        except OSError:
            pass

    recs = []
    # 1) requests 抓普通站
    for title, url in SOURCES:
        sid = f"idut:{hashlib.sha256(url.encode()).hexdigest()[:16]}"
        if sid in done:
            continue
        headers = dict(HEADERS)
        if "mp.weixin" in url:
            headers["Referer"] = "https://mp.weixin.qq.com/"
        status, text = 0, ""
        if "ehall" not in url:
            try:
                r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
                r.encoding = r.apparent_encoding or "utf-8"
                if r.status_code == 200:
                    status = 200
                    text = extract_body(r.text)
            except (requests.RequestException, ValueError, TypeError):
                pass
        if status == 200 and len(text) >= 30:
            recs.append(
                {
                    "source_id": sid,
                    "dataset": "kb_clean",
                    "canonical_url": url,
                    "title": title,
                    "official_domain": url.split("/")[2],
                    "published_at": None,
                    "fetched_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "clean_text": text,
                    "fetch_status": "success",
                    "candidate_cards": [],
                    "unresolved_questions": [],
                }
            )
            print(f"  OK {title[:25]} ({len(text)}字)")
        else:
            print(f"  FAIL {title[:25]} (需Playwright: {url[:45]})")
        time.sleep(0.4)

    # 2) ehall 用 Playwright
    ehall = [(t, u) for t, u in SOURCES if "ehall" in u]
    if ehall:
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
            for title, url in ehall:
                sid = f"idut:{hashlib.sha256(url.encode()).hexdigest()[:16]}"
                if sid in done:
                    continue
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(2500)
                    status = resp.status if resp else 0
                    text = page.evaluate('document.body ? document.body.innerText : ""')
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    clean = "\n".join(lines)[:MAX_TEXT].strip()
                    if status == 200 and len(clean) >= 30:
                        recs.append(
                            {
                                "source_id": sid,
                                "dataset": "kb_clean",
                                "canonical_url": url,
                                "title": title,
                                "official_domain": "ehall.dlut.edu.cn",
                                "published_at": None,
                                "fetched_at": datetime.datetime.now(
                                    datetime.timezone.utc
                                ).isoformat(),
                                "content_hash": hashlib.sha256(
                                    clean.encode("utf-8")
                                ).hexdigest(),
                                "clean_text": clean,
                                "fetch_status": "success",
                                "candidate_cards": [],
                                "unresolved_questions": [],
                            }
                        )
                        print(f"  OK ehall {title[:25]} ({len(clean)}字)")
                    else:
                        print(f"  FAIL ehall {title[:25]}")
                except Exception:
                    print(f"  FAIL ehall {title[:25]}")
            browser.close()

    try:
        with open(OUT, "a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError as ex:
        print(f"写入失败: {ex}")
        return
    print(f"\n完成: 新增 {len(recs)} 条 → {OUT}")


if __name__ == "__main__":
    main()
