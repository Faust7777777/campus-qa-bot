# 交接包索引

本目录是 2026-08-06 本地收尾后的唯一交接源。VPS agent 只做 QQ/NapCat 登录和 HTTP Client 接入；资料生产、审核、构库、评测和发布仍按 `docs/智能体架构设计.md` 执行。

## 目录

- `campus-qa-bot/`：源码、测试、Compose、文档和完整 `work/` 离线产物。
- `legacy_handoff/`：最初交接包及三份 CSV：`kb_clean.csv`、`kb_faculty.csv`、`web_plus_index.csv`。
- `source_materials/`：结构探测报告和用户提供的原理 PDF，仅作设计参考。
- `SHA256SUMS`：压缩包根目录下生成的全量文件摘要；解包后先校验。

## 重要资产

- 最终审核 JSONL：`campus-qa-bot/work/luna_final_reviewed_20260806.jsonl`
- 最终审核报告：`campus-qa-bot/work/luna_final_review_report_20260806.json`
- 最终审核摘要：3227 张卡；approved 3024、downgraded 190、rejected 13、pending 0、conflict 0。
- 最终审核 JSONL SHA-256：`7bead5d33b2b3022f2da59088bb12cbba842985c56c6a1dbb80ef739f6e64f20`
- 正式评测集：`campus-qa-bot/work/evaluation_20260807.jsonl`；300题，SHA-256 为 `184c8bebab44d6f41e62939482b9e3677803a29d987f5083795d4d540c2a8334`；它与已确认草案逐字节一致，冻结记录为 `evaluation_20260807_freeze.json`。原始草案及报告仍保留作 provenance。
- 隔离集：`legacy_handoff/data/kb_faculty.csv`，只用于审计，禁止进入 `releases/`。

## 当前不能做的事

大工网关已经实测可用：`bge-m3` 的 `/v1/embeddings` 返回1024维向量，四个运行时角色的模型契约均已通过。300题固定评测集已经冻结，但交接包不含网关 API key，构库和评测必须由人工在运行时注入轮换后的密钥。在评测门通过并原子激活前，仍不能启动正式 Bot；不要使用随机/零向量或模型常识绕过门槛。
