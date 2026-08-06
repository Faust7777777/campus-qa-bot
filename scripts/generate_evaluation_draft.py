from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

from luna_kb.contracts import canonicalize_url, normalized_text


ANSWERABLE_COUNT = 167
HISTORICAL_COUNT = 33
NO_ANSWER_COUNT = 40
OUT_OF_SCOPE_COUNT = 30
FACULTY_COUNT = 30


NO_ANSWER_QUESTIONS = [
    "本科生如何申请2027年春季去月球基地实习？",
    "商学院本科生如何办理2028年人工智能专业跨校联合培养？",
    "本科生怎样申请学校提供的夜间无人机驾驶证？",
    "商学院本科生如何办理校内宠物饲养许可证？",
    "本科生申请2030年校级元宇宙创业基金需要哪些材料？",
    "本科生如何预约校内核磁共振设备做个人体检？",
    "商学院本科生如何申请每周六免费校车去机场？",
    "本科生毕业后能否把学籍保留到2035年？",
    "本科生如何办理把课程成绩永久隐藏的申请？",
    "商学院本科生怎样申请校内商业店铺经营许可？",
    "本科生如何办理宿舍安装家用充电桩？",
    "本科生申请2027年全额海外交换奖学金的截止日期是什么？",
    "商学院本科生如何申请校级私人游艇驾驶培训？",
    "本科生能否把一门课程替换为自学并直接记满分？",
    "本科生如何申请学校发放的个人购车补贴？",
    "商学院本科生如何办理跨校区长期免费停车证？",
    "本科生申请2028年诺贝尔奖提名需要走什么流程？",
    "本科生如何查询个人未来十年的课程排课表？",
    "商学院本科生怎样申请校内直播间长期使用权？",
    "本科生能否申请将所有考试改为线上口试？",
    "本科生如何办理在宿舍开设餐饮档口？",
    "商学院本科生申请校内广告位需要哪些材料？",
    "本科生如何申请学校提供的私人法律顾问？",
    "本科生怎样办理校园无人驾驶汽车测试许可？",
    "本科生如何申请2029年校内商业贷款？",
    "商学院本科生如何办理跨校区每日专车？",
    "本科生如何领取学校发放的个人证券交易额度？",
    "本科生申请把毕业证寄往火星的手续是什么？",
    "本科生如何办理学校内部的宠物医疗报销？",
    "商学院本科生怎样申请校内自助售货机经营权？",
    "本科生如何查询尚未制定的2032年收费标准？",
    "本科生如何申请课程免修但不参加任何考核？",
    "商学院本科生如何办理校园内无人机航拍商业许可？",
    "本科生如何申请校内演唱会商业赞助额度？",
    "本科生怎样申请学校提供的个人移民咨询？",
    "商学院本科生如何申请未公布的2028年招生计划？",
    "本科生如何把宿舍床位转让给校外人员？",
    "本科生申请校内长期存放私人车辆需要哪些材料？",
    "本科生如何申请学校承担个人旅游费用？",
    "商学院本科生怎样办理校内个人公司注册？",
]

OUT_OF_SCOPE_QUESTIONS = [
    "教职工年度考核在哪办理？",
    "教职工如何申请因公出国？",
    "博士生论文预答辩怎么报销？",
    "研究生国家奖学金评审材料在哪？",
    "MBA学员如何申请学位？",
    "国际学生签证延期怎么办？",
    "访问学者如何申请校内工位？",
    "教师职称评审需要什么材料？",
    "离退休人员如何办理福利？",
    "校外社会人员如何报名教职工培训？",
    "小学教师招聘什么时候报名？",
    "大工附属高中教师招聘条件是什么？",
    "企业如何在校园投放广告？",
    "校外商家如何申请校园摊位？",
    "哪家奶茶店给大工学生打折？",
    "大连旅游路线怎么安排？",
    "购买电脑哪个品牌好？",
    "申请银行信用卡需要什么？",
    "校外租房合同怎么签？",
    "游戏充值有什么优惠活动？",
    "股票投资怎么开户？",
    "如何在淘宝购买教材？",
    "校外驾校报名流程是什么？",
    "社会人员如何进入学校参加会议？",
    "校友如何申请校友卡？",
    "教工家属入园申请怎么办？",
    "博士后进站需要哪些材料？",
    "教师退休体检怎么安排？",
    "外籍教师居留许可怎么办理？",
    "校外公司如何发布招聘公告？",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _case(case_id: str, kind: str, question: str, card: dict[str, Any] | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": case_id,
        "question": question.strip(),
        "kind": kind,
        "expected_card_ids": [],
        "expected_urls": [],
        "history": [],
    }
    if card is not None:
        item["expected_card_ids"] = [str(card["card_id"])]
        item["expected_urls"] = [canonicalize_url(str(card["canonical_url"]))]
    return item


def _load_cards(path: Path) -> tuple[list[dict[str, Any]], str]:
    cards: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("review_status") not in {"approved", "downgraded"}:
            continue
        card = record.get("card") or {}
        source = record.get("source") or {}
        question = str(card.get("standard_question") or "").strip()
        url = str(source.get("canonical_url") or "").strip()
        card_id = str(card.get("card_id") or "").strip()
        if not question or not url or not card_id:
            continue
        card_copy = dict(card)
        card_copy["canonical_url"] = canonicalize_url(url)
        card_copy["source_id"] = str(source.get("source_id") or "")
        cards.append(card_copy)
    return cards, _sha256(path)


def _dedupe_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    seen_questions: set[str] = set()
    result: list[dict[str, Any]] = []
    for card in cards:
        card_id = str(card["card_id"])
        question_key = normalized_text(str(card["standard_question"]))
        if card_id in seen_ids or not question_key or question_key in seen_questions:
            continue
        seen_ids.add(card_id)
        seen_questions.add(question_key)
        result.append(card)
    return result


def _stable_order(cards: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    ordered = list(cards)
    random.Random(seed).shuffle(ordered)
    return ordered


def _overlap_score(left: str, right: str) -> float:
    def grams(value: str) -> set[str]:
        compact = normalized_text(value)
        return {compact[index : index + 2] for index in range(max(0, len(compact) - 1))}

    lhs = grams(left)
    rhs = grams(right)
    if not lhs or not rhs:
        return 0.0
    return len(lhs & rhs) / len(lhs | rhs)


def _load_faculty(path: Path) -> tuple[list[dict[str, str]], str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return rows, _sha256(path)


def generate(reviewed_path: Path, faculty_path: Path, output_path: Path, report_path: Path, seed: int) -> None:
    cards, reviewed_sha256 = _load_cards(reviewed_path)
    cards = _dedupe_cards(cards)
    faculty, faculty_sha256 = _load_faculty(faculty_path)
    if len(NO_ANSWER_QUESTIONS) != NO_ANSWER_COUNT:
        raise ValueError("no-answer template count drifted")
    if len(OUT_OF_SCOPE_QUESTIONS) != OUT_OF_SCOPE_COUNT:
        raise ValueError("out-of-scope template count drifted")
    if len(faculty) < FACULTY_COUNT:
        raise ValueError("faculty isolation set is too small")

    current_or_unknown = [
        card for card in cards if card.get("validity") in {"current", "unknown"}
    ]
    historical = [card for card in cards if card.get("validity") == "historical"]
    # Facts are more useful for the positive set; navigation cards remain in the
    # pool because the runtime has an explicit, auditable navigation answer.
    current_or_unknown = sorted(
        current_or_unknown,
        key=lambda card: (card.get("card_kind") != "fact", str(card.get("card_id"))),
    )
    historical = sorted(
        historical,
        key=lambda card: (card.get("card_kind") != "fact", str(card.get("card_id"))),
    )
    current_or_unknown = _stable_order(current_or_unknown, seed)
    historical = _stable_order(historical, seed + 1)
    if len(current_or_unknown) < ANSWERABLE_COUNT:
        raise ValueError("not enough current/unknown positive cards for answerable quota")
    answerable_cards = current_or_unknown[:ANSWERABLE_COUNT]
    selected_ids = {str(card["card_id"]) for card in answerable_cards}
    historical_cards = [card for card in historical if str(card["card_id"]) not in selected_ids][:HISTORICAL_COUNT]
    if len(answerable_cards) != ANSWERABLE_COUNT or len(historical_cards) != HISTORICAL_COUNT:
        raise ValueError("not enough unique positive cards for the requested quotas")

    rows: list[dict[str, Any]] = []
    provenance: dict[str, dict[str, Any]] = {}
    for index, card in enumerate(answerable_cards, 1):
        case_id = f"draft-answerable-{index:03d}"
        rows.append(_case(case_id, "answerable", str(card["standard_question"]), card))
        provenance[case_id] = {
            "origin": "reviewed_card.standard_question",
            "card_id": card["card_id"],
            "source_id": card.get("source_id", ""),
            "validity": card.get("validity", ""),
            "card_kind": card.get("card_kind", ""),
        }
    for index, card in enumerate(historical_cards, 1):
        case_id = f"draft-historical-{index:03d}"
        rows.append(_case(case_id, "historical", str(card["standard_question"]), card))
        provenance[case_id] = {
            "origin": "reviewed_card.standard_question",
            "card_id": card["card_id"],
            "source_id": card.get("source_id", ""),
            "validity": card.get("validity", ""),
            "card_kind": card.get("card_kind", ""),
        }
    for index, question in enumerate(NO_ANSWER_QUESTIONS, 1):
        case_id = f"draft-no-answer-{index:03d}"
        rows.append(_case(case_id, "no_answer", question))
        provenance[case_id] = {"origin": "manual_no_evidence_template"}
    for index, question in enumerate(OUT_OF_SCOPE_QUESTIONS, 1):
        case_id = f"draft-out-of-scope-{index:03d}"
        rows.append(_case(case_id, "out_of_scope", question))
        provenance[case_id] = {"origin": "manual_scope_boundary_template"}

    seen_faculty_questions: set[str] = set()
    faculty_used: list[int] = []
    for row_index, row in enumerate(faculty, 1):
        title = (row.get("title") or "").strip()
        query = (row.get("query") or "").strip()
        label = title or query
        if not label:
            continue
        question = (
            f"教职工专属事项“{label}”的办理流程是什么？"
            if not label.endswith(("？", "?"))
            else f"教职工专属问题：{label}"
        )
        key = normalized_text(question)
        if key in seen_faculty_questions:
            continue
        case_id = f"draft-faculty-boundary-{len(faculty_used) + 1:03d}"
        rows.append(_case(case_id, "faculty_boundary", question))
        provenance[case_id] = {
            "origin": "kb_faculty.csv",
            "faculty_row": row_index,
            "faculty_query": query,
        }
        seen_faculty_questions.add(key)
        faculty_used.append(row_index)
        if len(faculty_used) == FACULTY_COUNT:
            break
    if len(faculty_used) != FACULTY_COUNT:
        raise ValueError("could not produce unique faculty boundary questions")

    question_keys = [normalized_text(str(row["question"])) for row in rows]
    if len(question_keys) != len(set(question_keys)):
        raise ValueError("draft contains duplicate normalized questions")
    if len(rows) != ANSWERABLE_COUNT + HISTORICAL_COUNT + NO_ANSWER_COUNT + OUT_OF_SCOPE_COUNT + FACULTY_COUNT:
        raise ValueError("draft row count does not equal the requested formal minimum")

    card_by_id = {str(card["card_id"]): card for card in cards}
    missing_gold = [
        str(card_id)
        for row in rows
        if row["kind"] not in {"no_answer", "out_of_scope", "faculty_boundary"}
        for card_id in row["expected_card_ids"]
        if str(card_id) not in card_by_id
    ]
    url_mismatches = [
        row["id"]
        for row in rows
        if row["kind"] not in {"no_answer", "out_of_scope", "faculty_boundary"}
        and any(
            canonicalize_url(str(url))
            != card_by_id[str(card_id)]["canonical_url"]
            for card_id, url in zip(row["expected_card_ids"], row["expected_urls"], strict=True)
        )
    ]
    negative_overlap_candidates: list[dict[str, Any]] = []
    for row in rows:
        if row["kind"] not in {"no_answer", "out_of_scope", "faculty_boundary"}:
            continue
        best = max(
            (
                (_overlap_score(str(row["question"]), str(card["standard_question"])), card)
                for card in cards
            ),
            key=lambda value: (value[0], str(value[1]["card_id"])),
        )
        if best[0] >= 0.25:
            negative_overlap_candidates.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "score": round(best[0], 4),
                    "card_id": best[1]["card_id"],
                    "card_question": best[1]["standard_question"],
                }
            )

    set_sha256 = _write_jsonl(output_path, rows)
    report = {
        "draft": True,
        "generated_at": "2026-08-06",
        "method": "deterministic card-derived positives plus manually authored negative templates",
        "seed": seed,
        "reviewed_jsonl_sha256": reviewed_sha256,
        "faculty_csv_sha256": faculty_sha256,
        "evaluation_set_sha256": set_sha256,
        "question_count": len(rows),
        "kind_counts": dict(Counter(str(row["kind"]) for row in rows)),
        "faculty_rows_used": faculty_used,
        "gold_card_audit": {
            "missing_card_ids": sorted(set(missing_gold)),
            "url_mismatch_ids": sorted(set(url_mismatches)),
        },
        "negative_overlap_candidates": sorted(
            negative_overlap_candidates,
            key=lambda value: (-float(value["score"]), str(value["id"])),
        ),
        "needs_human_review": [
            "确认每个正例问题与 expected_card_ids/expected_urls 的金标准关系。",
            "逐题审阅 no_answer 与 out_of_scope 的边界，避免把可由生产卡回答的问题误标为负例。",
            "确认历史题的时间语义和 history 字段是否需要补充追问上下文。",
            "草案未执行模型检索评测，也不能直接用于 activate。",
        ],
        "provenance": provenance,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({k: report[k] for k in ("draft", "question_count", "kind_counts", "evaluation_set_sha256")}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a non-publishable fixed evaluation-set draft")
    parser.add_argument("--reviewed", type=Path, required=True)
    parser.add_argument("--faculty", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260806)
    args = parser.parse_args()
    generate(args.reviewed, args.faculty, args.output, args.report, args.seed)


if __name__ == "__main__":
    main()
