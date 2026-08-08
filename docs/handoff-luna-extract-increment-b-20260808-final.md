# Luna 抽卡增量 B 最终交接（2026-08-08）

## 结果

- 6 条抽取源全部处理完成：5 条 `success`、1 条 `unresolved`。
- 原始抽取 8 张卡；按增量说明去掉 1 张与权威学籍规定重复的 GPA 卡，最终保留 7 张：6 fact、1 navigation。
- fact validity：4 张 `current`、2 张 `historical`；无 fact 使用 `unknown`。
- 证据逐字命中率 100%；内容 hash、source/card ID、受众和校区审计通过。
- 未抽取统一身份认证初始密码、身份证后六位或备用密码。

## 去重决定

「本科生成绩管理办法」与「学籍管理规定第三十条」都出现 GPA 公式。后者同时给出完整成绩—绩点对应表，保留后者作为权威 GPA 卡，删除 `card_idut_f197_gpa_formula`，避免近重复召回。

## 文件

- [生产版增量卡](../work/idut_increment_b_production_20260808.jsonl)
- [原始 ID 增量卡](../work/idut_increment_b_original_20260808.jsonl)
- [首批+B 合并生产版](../work/idut_cards_all_production_20260808.jsonl)
- [Luna 收集结果](../work/idut_increment_b_collected_20260808.jsonl)
- [审核诊断报告](../work/idut_increment_b_review_report_20260808.json)
- [导航源（未并入事实抽取）](../work/handoff_increment_20260808B/campus-qa-luna抽卡增量-20260808B/nav_only_增量B.jsonl)

生产版可被 `LunaSourceResult` 正常加载，未覆盖首批产物或生产数据库。

首批+B 合并版共 21 条源、25 张卡；已检查 source/card ID 无重复。首批已有通用 GPA 公式，因此增量的权威学籍规定被保留为独立的“成绩与绩点对应关系”卡，只承载分数—绩点对照表，避免近重复。

## unresolved

「统一身份认证登录说明（初始密码）」仅含安全敏感的初始密码规则，因此保留 unresolved、零卡，不向机器人公开播报。

标准 review 诊断为 6 pending、1 downgraded，主要是现有审核器的人工审核阈值；这不影响本批候选卡交接。
