#!/usr/bin/env python
"""Reconcile two reviewed sets that re-fetched the same source.

review-merge refuses when one source_id carries two different revisions, and it
is right to: silently taking either one can drop evidence that existing cards
depend on.  But refusing outright would also throw away a strictly better
re-fetch, which is what four of these are - the same URL and title, extracted
more completely the second time (788 chars -> 4657).

The rule is decided by evidence rather than by size or recency:

  adopt the new revision  when every existing card's evidence_quote is still a
                          literal substring of the new clean_text.  Nothing
                          that already worked can break, and the richer text
                          supports the new cards as well.

  keep the old revision   otherwise.  One of these re-fetches came back shorter
                          and lost the text three existing cards quote, so it
                          is a worse capture, not a newer one, and its card is
                          dropped instead.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

# contracts.py caps a source at four candidate cards.
MAX_CARDS_PER_SOURCE = 4


def load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True, help="existing reviewed set")
    parser.add_argument("--incoming", type=Path, required=True, help="newly reviewed set")
    parser.add_argument("--out-base", type=Path, required=True)
    parser.add_argument("--out-incoming", type=Path, required=True)
    parser.add_argument(
        "--evaluation-set",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "work/evaluation_20260807.jsonl",
        help="its gold card ids must survive; the release is unevaluatable without them",
    )
    args = parser.parse_args()

    gold_ids: set[str] = set()
    if args.evaluation_set.is_file():
        for line in args.evaluation_set.read_text(encoding="utf-8").splitlines():
            if line.strip():
                gold_ids.update(json.loads(line).get("expected_card_ids") or [])

    base, incoming = load(args.base), load(args.incoming)
    by_base, by_incoming = defaultdict(list), defaultdict(list)
    for row in base:
        by_base[row["source"]["source_id"]].append(row)
    for row in incoming:
        by_incoming[row["source"]["source_id"]].append(row)

    adopted, rejected, overflow = [], [], []
    for sid in sorted(set(by_base) & set(by_incoming)):
        new_text = by_incoming[sid][0]["source"]["clean_text"]
        survives = all(
            (not r["card"].get("evidence_quote"))
            or r["card"]["evidence_quote"] in new_text
            for r in by_base[sid]
        )
        if survives:
            # Re-point every card at the richer capture.  The candidate lineage
            # has to hold both sets of cards or the merge rejects each row for
            # citing a source that does not list it.
            merged = json.loads(json.dumps(by_incoming[sid][0]["source"], ensure_ascii=False))
            rows_here = by_base[sid] + by_incoming[sid]

            # A source may carry at most MAX_CARDS_PER_SOURCE candidates, so a
            # richer re-fetch can overflow the lineage.  Order of precedence:
            #
            #   1. cards the frozen evaluation names as gold.  That set is a
            #      published contract; dropping one makes the release
            #      unevaluatable, which is exactly what happened when this rule
            #      only weighed card kind.
            #   2. fact cards, which can answer a question, over navigation
            #      cards, which only point at the page they all already share.
            rows_here.sort(
                key=lambda r: (
                    r["card"]["card_id"] not in gold_ids,
                    r["card"]["card_kind"] != "fact",
                )
            )
            keep, drop = rows_here[:MAX_CARDS_PER_SOURCE], rows_here[MAX_CARDS_PER_SOURCE:]
            for row in drop:
                row["_drop"] = True
                overflow.append((sid, row["card"]["title"], row["card"]["card_kind"]))

            lineage = {r["card"]["card_id"]: r["card"] for r in keep}
            merged["candidate_cards"] = list(lineage.values())
            for row in keep:
                row["source"] = json.loads(json.dumps(merged, ensure_ascii=False))
            adopted.append((sid, len(by_base[sid]), len(by_incoming[sid])))
        else:
            for row in by_incoming[sid]:
                row["_drop"] = True
            rejected.append((sid, len(by_incoming[sid])))

    kept_base = [r for r in base if not r.pop("_drop", False)]
    kept_incoming = [r for r in incoming if not r.pop("_drop", False)]
    args.out_base.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept_base), encoding="utf-8"
    )
    args.out_incoming.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept_incoming), encoding="utf-8"
    )

    print(f"重叠 source_id: {len(set(by_base) & set(by_incoming))}")
    print(f"\n采用新版（旧卡证据全部存活）: {len(adopted)}")
    for sid, old_n, new_n in adopted:
        print(f"   {sid}  旧卡 {old_n} 张保留，新增 {new_n} 张")
    print(f"\n保留旧版（新抓丢了旧卡的证据）: {len(rejected)}")
    for sid, n in rejected:
        print(f"   {sid}  丢弃新卡 {n} 张")
    if overflow:
        print(f"\n血缘超过 {MAX_CARDS_PER_SOURCE} 张，让位给事实卡: {len(overflow)}")
        for _sid, title, kind in overflow:
            print(f"   [{kind}] {title[:44]}")
    print(f"\n-> {args.out_base}  ({len(kept_base)})")
    print(f"-> {args.out_incoming}  ({len(kept_incoming)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
