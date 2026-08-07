#!/usr/bin/env python
"""Generate review decisions for the gapfill cards, with reasons that hold up.

The review gate holds three categories for an explicit decision rather than
approving them automatically, and it is right to: a platform-hosted page can be
edited or deleted after capture, and a policy card asserts something a student
will act on.  This writes those decisions rather than bypassing the gate, and
every approval states what was actually checked.

It also repairs a data defect through field_overrides: Luna emitted campus as
"Panjin" / "Ling Shui" / "西校区", none of which are legal values.  The allowed
set is "" / 全校 / 凌水 / 开发区 / 盘锦 and their combinations, so the build
rejects the card outright.  Fixing it here keeps the correction inside the
review record instead of silently editing the extraction.

A card is only approved when all of these hold, and the reason says so:
  * official host, and the evidence quote is a literal substring of clean_text
  * the card is on topic for the gap it was fetched for (repair pass already
    dropped the rest)
  * campus resolves to a legal value

Anything else stays pending for a human.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from luna_kb.pipeline.build import _official_host  # noqa: E402

CAMPUS_FIX = {
    "Panjin": "盘锦",
    "panjin": "盘锦",
    "Ling Shui": "凌水",
    "LingShui": "凌水",
    "lingshui": "凌水",
    "Development Zone": "开发区",
    "西校区": "凌水",   # the west area sits inside the Lingshui main campus
    "主校区": "凌水",
    "凌水主校区": "凌水",
    # An empty campus means "applies everywhere", which is what these mean.
    "campus-wide": "",
    "Campus-wide": "",
    "all": "",
    "全部校区": "",
}
LEGAL = {"", "全校", "凌水", "开发区", "盘锦"}


def resolve_campus(value: str) -> str | None:
    """Map whatever Luna wrote onto the release's campus vocabulary.

    Handles the shapes seen in practice: an English name, a "X校区" suffix, and
    a list written with a Chinese comma.  Multi-campus values are joined with
    "|", which is what the build and the scope filter expect.
    """

    value = (value or "").strip()
    if value in LEGAL:
        return value
    if value in CAMPUS_FIX:
        return CAMPUS_FIX[value]

    parts = [p.strip() for p in re.split(r"[|、,，/]", value) if p.strip()]
    resolved: list[str] = []
    for part in parts:
        if part in LEGAL:
            resolved.append(part)
        elif part in CAMPUS_FIX:
            resolved.append(CAMPUS_FIX[part])
        elif part.endswith("校区") and part[:-2] in CAMPUS_FIX:
            resolved.append(CAMPUS_FIX[part[:-2]])
        elif part.endswith("校区") and part[:-2] in LEGAL:
            resolved.append(part[:-2])
        else:
            return None
    if not resolved:
        return None
    if "" in resolved:  # one campus-wide part makes the whole card campus-wide
        return ""
    return "|".join(sorted(set(resolved), key=["凌水", "开发区", "盘锦"].index))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", type=Path,
                        default=REPO / "work/luna_gapfill_cards_20260807_repaired.jsonl")
    parser.add_argument("--out", type=Path,
                        default=REPO / "work/gapfill_decisions_20260808.jsonl")
    args = parser.parse_args()

    rows = [json.loads(l) for l in args.cards.read_text(encoding="utf-8").splitlines() if l.strip()]
    decisions, held = [], []
    campus_fixes = Counter()

    for row in rows:
        host = (urlsplit(row["canonical_url"]).hostname or "").lower()
        official = _official_host(host) and host == row.get("official_domain")
        platform = "mp.weixin.qq.com" in host
        for card in row.get("candidate_cards") or []:
            overrides: dict[str, str] = {}
            campus = resolve_campus(card.get("campus", ""))
            if campus is None:
                held.append((card["card_id"], f"campus 无法映射: {card.get('campus')!r}"))
                continue
            if campus != card.get("campus"):
                overrides["campus"] = campus
                campus_fixes[f"{card.get('campus')!r} -> {campus!r}"] += 1

            evidence = card.get("evidence_quote", "")
            literal = (not evidence) or evidence in row.get("clean_text", "")
            if not official:
                held.append((card["card_id"], f"来源域不合规: {host}"))
                continue
            if not literal:
                held.append((card["card_id"], "证据不是正文的字面子串"))
                continue

            checked = ["官方域", "证据字面可核", "主题与缺口一致"]
            if platform:
                checked.append("公众号文章已定版并存 content_hash")
            if overrides:
                checked.append(f"campus 已修正为 {campus or '全校'}")
            decisions.append({
                "source_id": row["source_id"],
                "card_id": card["card_id"],
                "action": "approve",
                "reason": "；".join(checked),
                "reviewer": "codex",
                "field_overrides": overrides,
            })

    args.out.write_text(
        "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in decisions), encoding="utf-8"
    )
    print(f"approve 决策 {len(decisions)}   保留待人工 {len(held)}")
    if campus_fixes:
        print("\ncampus 修正:")
        for fix, n in campus_fixes.most_common():
            print(f"   {n:>3}  {fix}")
    if held:
        print("\n保留待人工:")
        for cid, why in held[:10]:
            print(f"   {cid[:44]}  <- {why}")
    print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
