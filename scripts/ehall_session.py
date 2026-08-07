#!/usr/bin/env python
"""Extend a saved CAS session to cover ehall, then prove it actually worked.

Why this exists
---------------
The first ehall attempt used a storage_state captured on chat.dlut.edu.cn.  It
held a live sso.dlut.edu.cn session but no ehall cookie, so every headless page
load bounced to the identity-provider wall and all 34 services came back as the
same 156-character login page.  Nobody noticed because the fetch reported 34/34
success: a login wall is a 200 with a body.

So this does two things the previous run did not:
  * visits ehall through the existing SSO session, which lets CAS redirect and
    set the ehall cookie without a fresh login (no captcha, no rate-limit risk)
  * refuses to report success unless the rendered page stops looking like the
    wall - the check that would have caught the problem the first time

Run with --probe first.  It fetches a single service and prints what it got, so
a wasted batch costs one request instead of thirty-four.

The session file holds live credentials.  It lives outside the repository and
must never be committed or printed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

STATE = Path(r"<DESKTOP>\state.json")
PROBE_URL = (
    "https://ehall.dlut.edu.cn/fp/visitService"
    "?service_id=7a37a6d4-8a65-488c-bae8-1013f197865e"
)  # 本科生专项奖学金申请办理 - the question that started all of this

WALL = re.compile(r"统一身份认证|登录帮助|微信扫码登录|立即登录|找回密码")


def looks_like_wall(text: str) -> bool:
    return bool(WALL.search(text[:400]))


def extract_text(page) -> str:
    return re.sub(r"\s+\n", "\n", page.inner_text("body")).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="fetch one service and stop")
    parser.add_argument("--refresh", action="store_true", help="open a window to log in by hand")
    parser.add_argument("--out", type=Path, default=Path("work/ehall_rendered.jsonl"))
    parser.add_argument("--urls", type=Path, help="JSONL with canonical_url per line")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    if not STATE.is_file() and not args.refresh:
        print(f"no session at {STATE}; run with --refresh first", file=sys.stderr)
        return 2

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.refresh)
        ctx = browser.new_context(
            storage_state=str(STATE) if STATE.is_file() else None, locale="zh-CN"
        )
        page = ctx.new_page()

        # One visit through CAS is enough to mint the ehall cookie the saved
        # chat session never had.
        page.goto(PROBE_URL, wait_until="networkidle", timeout=60_000)
        if args.refresh:
            print("[*] log in in the window, land on the service page, then press enter")
            input("    ")
            page.reload(wait_until="networkidle", timeout=60_000)

        text = extract_text(page)
        walled = looks_like_wall(text)
        print(f"probe: {len(text)} chars, wall={walled}")
        print("---- first 400 chars ----")
        print(text[:400])
        print("-------------------------")
        if walled:
            print(
                "\nstill behind the wall. Re-run with --refresh and log in by hand.",
                file=sys.stderr,
            )
            ctx.storage_state(path=str(STATE))
            browser.close()
            return 1

        ctx.storage_state(path=str(STATE))  # now carries the ehall cookie too
        print(f"\nsession extended and saved. Judge the text above before batching:")
        print("  is it the application guide (conditions/materials/deadline),")
        print("  or just the form shell? If it is a form, ehall is the wrong")
        print("  target and these 34 should become static guide pages instead.")

        if args.probe or not args.urls:
            browser.close()
            return 0

        urls = [
            json.loads(line)["canonical_url"]
            for line in args.urls.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        urls = [u for u in urls if "ehall.dlut.edu.cn" in u]
        seen: set[str] = set()
        rows = []
        for index, url in enumerate(urls, 1):
            try:
                page.goto(url, wait_until="networkidle", timeout=60_000)
                body = extract_text(page)
            except Exception as exc:
                rows.append({"canonical_url": url, "status": "error", "detail": str(exc)[:200]})
                print(f"  [{index}/{len(urls)}] ERROR {url[-24:]}")
                continue
            status = "wall" if looks_like_wall(body) else "ok"
            seen.add(body[:200])
            rows.append({"canonical_url": url, "status": status, "clean_text": body})
            print(f"  [{index}/{len(urls)}] {status:<4} {len(body):>6} chars  {url[-24:]}")

        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
        ok = sum(1 for r in rows if r["status"] == "ok")
        print(f"\n{ok}/{len(rows)} rendered, {len(seen)} distinct bodies -> {args.out}")
        if len(seen) <= 1 < len(rows):
            print("every body is identical - that is a shell, not content", file=sys.stderr)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
