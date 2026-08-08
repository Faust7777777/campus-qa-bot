# i大工抽卡批最终交接（2026-08-08）

## 完成情况

- retry batch 已完成：4/4 源成功，新增 5 张卡。
- 合并后共 15 条源、18 张卡：12 条 `success`、3 条 `unresolved`。
- 卡片：14 张 fact、4 张 navigation；fact 中 13 张 `current`、1 张 `historical`。
- `evidence_quote` 逐字命中同一源 `clean_text`：100%。
- 无重复 `source_id`/`card_id`；`campus` 仅使用空字符串、凌水、开发区、盘锦；`audience` 全为本科生。
- 未抽取“统一身份认证初始密码/身份证后六位”事实卡。

## 交付文件

- 原始 ID 交付（便于追溯）：`work/idut_cards_complete_20260808.jsonl`
- 生产命名空间版本（`kb_clean:`）：`work/idut_cards_production_20260808.jsonl`
- 生产 ID 映射：`work/idut_production_id_mapping_20260808.json`
- retry 原始收集结果：`work/idut_retry_collected_20260808.jsonl`
- retry 校验报告：`work/idut_retry_validation_report_20260808.json`

生产版本已经通过 `LunaSourceResult` 结构加载检查。没有覆盖旧知识库、旧 draft 或生产数据库。

## 尚未形成事实卡的 3 条源

1. 校园门户 CampusPortal：快照是英文不完整登录/入口文本，无法验证一网通办入口。
2. 学生证打印自助：正文介绍综合证明打印平台，但没有“学生证补打”的直接证据。
3. 2024 级本科新生校园网络认证：标题与正文不一致，正文实际是数字经济学域简介。

这三条保留为 `unresolved`，没有用标题、摘要或常识补卡。

## 审核说明

标准 `review` 结果为 16 张 pending、2 张 downgraded。pending 主要来自平台来源人工审核和政策卡人工审核阈值；这不是抽卡失败。按当前口径，直接交付生产版本的候选卡，真人在群内发现问题时再指出即可。

