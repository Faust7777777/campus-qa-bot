# Luna 抽卡交接：batch3 + 长文件补充（2026-08-09）

## 完成结果

- batch3 新增 11 条源：10 条 `success`、1 条 `unresolved`，20 张卡。
- 交接包中的两份“已抓未抽完”长文件已补抽：2 条 supplement 源、8 张卡。
- 与前两批合并后：34 条源、53 张卡；29 条 `success`、5 条 `unresolved`。
- 合并卡片：48 fact、5 navigation；fact validity 为 42 current、6 historical。
- 全部 fact 证据逐字命中 `clean_text`，hash、source/card ID、校区、受众审计通过；没有个人手机号事实卡。

## 长文件补抽内容

- 《本科生成绩管理办法》：缓考申请、成绩复核、补考安排、旷考/作弊处理。
- 《学籍管理规定》：休学、复学、退学警告/退学、补考与旷考处理。
- 已跳过前批已经处理的 GPA、重修和成绩记入成绩单卡。

## 交付文件

- [batch3 生产版](../work/batch3_production_20260809.jsonl)
- [长文件补充生产版](../work/batch3_long_supplement_production_20260809.jsonl)
- [34 源合并生产版](../work/idut_cards_all_production_20260809.jsonl)
- [主批收集结果](../work/batch3_collected_20260809.jsonl)
- [长文件补充收集结果](../work/batch3_long_supplement_collected_20260809.jsonl)
- [合并审核诊断](../work/idut_cards_all_review_report_20260809.json)

## 仍 unresolved 的 5 条

1. 校园门户快照不完整；
2. 学生证打印正文没有学生证补打证据；
3. 2024 新生网络源标题与正文不一致；
4. 统一身份认证初始密码源仅含安全敏感规则；
5. 2026 上半年四六级通知缺少报名入口、时间、资格和费用。

这些条目均未用标题或模型常识硬补事实卡。

