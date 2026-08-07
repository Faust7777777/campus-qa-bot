#!/usr/bin/env python
"""Check a fetch output before it goes to card extraction.  Offline, no gateway.

Every failure mode here was found by hand in the 2026-08-07 batch, after that
batch was reported as 131/142 successful.  A fetch reports success on anything
that returns a body, so a login wall, a permission page and a mojibake page all
count as wins until somebody reads them.  This reads them.

Checks, in order of how much damage they do if missed:

  login wall / permission page
      A 200 with a body that is really the identity provider.  Detected by
      content, and by the giveaway that many "different" pages share one body:
      34 ehall services all returned the same 156 characters.  These are the
      worst failure because they pass every build gate - official host, hash
      matches, evidence is a literal substring of clean_text - and produce
      cards that cite an official URL while quoting a login form.

  mojibake
      UTF-8 decoded as something else.  59 WeChat articles arrived as Mac Roman
      and would have carried that into evidence_quote and the index.  Repairable
      in place, so this reports the repair rather than just the damage.

  off-topic noise
      The affiliated primary school's class-assignment notices answer to
      "成绩查询"; a "四六级可以查分辣" hype post answers to "四六级报名".

  thin content
      Under a few hundred characters there is rarely a procedure to extract.

Exit code is non-zero if anything in the first category is present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

WALL = re.compile(
    r"统一身份认证|登录帮助|微信扫码登录|立即登录|找回密码"
    r"|您没有访问当前栏目的权限|系统提示|请先登录|用户登录"
)
NOISE = re.compile(r"附校|附属学校|小一年级|一年级新生|阳光分班|高考成绩|中考|考研倒计时|录取开放日")
THIN_CHARS = 300


def is_mojibake(text: str) -> bool:
    head = (text or "")[:300]
    if not head:
        return False
    suspects = sum(
        1 for ch in head if "À" <= ch <= "ÿ" or "Ā" <= ch <= "ɏ"
    )
    return suspects > len(head) * 0.15


def repair_mojibake(text: str) -> str | None:
    """WeChat arrived as UTF-8 read through Mac Roman; try the usual suspects."""

    for codec in ("mac_roman", "cp1252", "latin-1"):
        try:
            fixed = text.encode(codec).decode("utf-8")
        except Exception:
            continue
        if not is_mojibake(fixed):
            return fixed
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Luna results JSONL")
    parser.add_argument(
        "--repair-to",
        type=Path,
        help="write a copy with mojibake fixed and unusable sources dropped",
    )
    args = parser.parse_args()

    rows = [
        json.loads(line)
        for line in args.source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ok = [r for r in rows if r.get("fetch_status") == "success"]
    print(f"sources {len(rows)}   success {len(ok)}   "
          f"cards {sum(len(r.get('candidate_cards') or []) for r in rows)}")

    walls, garbled, noise, thin, good = [], [], [], [], []
    repaired = 0
    for row in ok:
        text = row.get("clean_text") or ""
        if is_mojibake(text):
            fixed = repair_mojibake(text)
            garbled.append(row)
            if fixed:
                row["clean_text"] = fixed
                text = fixed
                repaired += 1
        if WALL.search(text[:400]):
            walls.append(row)
        elif NOISE.search(row.get("title", "") + text[:200]):
            noise.append(row)
        elif len(text) < THIN_CHARS:
            thin.append(row)
        else:
            good.append(row)

    # Identical bodies across supposedly different pages is the strongest
    # signal that a renderer captured a shell rather than the content.
    bodies = Counter(hashlib.sha256((r.get("clean_text") or "").encode()).hexdigest() for r in ok)
    shells = [(h, n) for h, n in bodies.items() if n > 1]

    print(f"\n  login wall / permission page   {len(walls):>4}   <- blocks acceptance")
    print(f"  mojibake                       {len(garbled):>4}   (repaired {repaired})")
    print(f"  off-topic noise                {len(noise):>4}")
    print(f"  thin (<{THIN_CHARS} chars)             {len(thin):>4}")
    print(f"  usable                         {len(good):>4}")
    print(f"\n  distinct bodies {len(bodies)} / {len(ok)}")
    for digest, count in sorted(shells, key=lambda kv: -kv[1])[:4]:
        sample = next(
            r for r in ok
            if hashlib.sha256((r.get("clean_text") or "").encode()).hexdigest() == digest
        )
        print(f"    {count:>3}x  {(sample.get('clean_text') or '')[:56]!r}")

    if walls:
        print("\n  walls by host:")
        for host, count in Counter(
            urlsplit(r["canonical_url"]).hostname for r in walls
        ).most_common():
            print(f"    {count:>3}  {host}")

    missing_query = sum(1 for r in rows if not r.get("seed_query"))
    if missing_query:
        print(f"\n  seed_query not carried through: {missing_query}/{len(rows)}"
              " - gap coverage cannot be measured without it")

    if args.repair_to:
        keep = good
        args.repair_to.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8"
        )
        print(f"\n  wrote {len(keep)} usable sources -> {args.repair_to}")

    return 1 if walls else 0


if __name__ == "__main__":
    raise SystemExit(main())
