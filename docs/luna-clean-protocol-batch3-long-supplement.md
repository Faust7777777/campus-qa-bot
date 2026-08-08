# Luna Offline Cleaning Protocol

## Batch 3 decisions (2026-08-09)

## Long-file supplement tasks

These two tasks intentionally reuse captured `clean_text` under new supplement
source IDs and titles. Treat the amended task title as authoritative for output
identity, while using only literal substrings of the full captured text as evidence.
Generate up to four cards per source, grouped by the requested missing topics.
Do not emit the already-covered GPA formula, grade-management duplicate, or any
other card whose fact is already in the first+B merged set.

This is a new 11-source increment. Do not reprocess or append the two long files
from the handoff (`已抓未抽完_长文件.jsonl`); those are already represented in the
previous batches and are supplied only as context.

- Extract only undergraduate-applicable rules and procedures. In the housing
  regulation, skip graduate-only clauses and keep undergraduate/general campus
  clauses only.
- The 2026 first-half CET notice is a dated historical notice. Cards about its
  registration window, fee and 425-point requirement are `historical`. If the
  425-point threshold is stated as a standing eligibility rule independently of
  the dated notice, it may be a separate `current` card only when the quote itself
  supports that reading.
- The 2025 freshman medical-insurance notice is `historical`; do not present its
  payment window as current.
- The student-awards regulation may yield multiple distinct cards (scholarship
  amounts/eligibility, three-good student, outstanding student cadre, outstanding
  graduate) when each has its own direct evidence. Do not collapse unrelated
  award rules into one oversized card.
- The two GPA sources in the handoff context have already been handled in batch B;
  do not extract them here.
- A source containing the word “登录” is not automatically a login wall. Extract
  the business procedure when the captured text is a complete service description.

You are an offline data preparation worker. Process only the assigned JSONL input batch and write only its matching JSONL output file. Do not access the network, URLs, browsers, external tools, or any content outside this workspace. Do not modify inputs, protocols, source code, configuration, or other output files.

Every input in this workflow has action `verify_refresh_and_extract`. Treat `seed_description` as the captured source snapshot. Decode JSON normally; input text may be direct Unicode or ASCII `\uXXXX` escapes. Output must be valid UTF-8 JSONL.

## Scope

The target audience is undergraduate students of the Dalian University of Technology School of Economics and Management. General campus services useful to those students may be retained. Staff-only, graduate-only, international non-degree, primary or secondary school, unrelated news, commercial promotion, and obsolete event reports must not become fact cards. Mark them `out_of_scope` or produce no cards as appropriate. Never infer the audience from a generic mention of students. Never use model memory as evidence.

## One output per input

Write exactly one output object for every input `source_id`, with no duplicates and no prose outside JSONL. Preserve progress in the output file after each item. Each object must contain:

```json
{
  "source_id": "exact input source_id",
  "dataset": "kb_clean or web_plus_index",
  "canonical_url": "canonical input URL or empty string",
  "title": "source title",
  "official_domain": "URL hostname or empty string",
  "published_at": "source date or null",
  "fetched_at": null,
  "content_hash": "lowercase SHA-256 of the UTF-8 bytes of decoded clean_text",
  "clean_text": "cleaned captured snapshot",
  "fetch_status": "success|unresolved|out_of_scope",
  "candidate_cards": [],
  "unresolved_questions": []
}
```

Use `success` only when `clean_text` contains usable source evidence. Use `unresolved` when the snapshot is too thin, ambiguous, or contradicts its title or URL. Do not invent missing details. Keep the input URL; only normalize obvious URL syntax. The accepted source hosts are `dlut.edu.cn`, its subdomains, and `mp.weixin.qq.com`. If another host appears, keep the task unresolved and explain it in `unresolved_questions`.

## Candidate cards

Create zero to four small, self-contained cards per source. Each card must contain all fields below:

```json
{
  "card_id": "card_ plus a stable unique ASCII identifier",
  "title": "semantic title",
  "standard_question": "likely student question",
  "summary": "evidence-only summary",
  "evidence_quote": "one exact continuous substring of clean_text",
  "source_locator": "section or paragraph location",
  "generated_questions": ["two to five natural phrasings"],
  "aliases": ["common aliases"],
  "risk_level": "low|medium|high",
  "extraction_confidence": 0.0,
  "retrieval_text": "self-contained retrieval text",
  "keywords": [],
  "facts": {},
  "facets": ["Use only controlled Chinese facets: 资格, 申请, 材料, 流程, 时间, 期限, 地点, 联系, 入口, 培训, 费用, 工作量, 规则, 审批, 结果, 课程, 考试, 成绩, 账号, 密码, 设备, 报修, 医保, 安全, 退费, 住宿"],
  "campus": "Ling Shui, Development Zone, Panjin, campus-wide, or empty; use the original Chinese label",
  "audience": "\\u672c\\u79d1\\u751f",
  "validity": "current|historical|unknown",
  "parent_card_id": null,
  "subject_key": "stable topic key",
  "fact_key": "stable fact key",
  "source_authority": "formal_policy|service_hall|school_notice|news|other",
  "card_kind": "fact|navigation"
}
```

All time, money, eligibility, materials, steps, locations, and contact details must appear literally in `evidence_quote`. A fact-card quote must be an exact substring of `clean_text`. Preserve phrases equivalent to "subject to the latest notice". Never turn a one-time news event into a durable rule. Do not publish personal mobile numbers as durable facts. When direct evidence is incomplete, create a navigation card or no card. Do not rewrite a sentence and present the rewrite as a quote.

Because this workflow performs no live refresh, set `validity` to `unknown` unless the snapshot itself contains explicit effective dates or current-validity wording that covers the current date. Use `historical` for expired dated events or rules. Do not set `current` merely because a service page or title sounds active.

## Final checks

Before finishing, verify that every JSON line parses, input and output `source_id` sets are identical, IDs are unique, every content hash is correct, no source has more than four cards, and every fact-card quote is an exact substring of `clean_text`. Your final response must contain only the relative output path and counts for success, unresolved, out-of-scope, and items needing Codex review.
