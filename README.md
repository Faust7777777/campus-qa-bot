# Campus QA Bot

面向商学院本科生 QQ 群的证据辅助答疑机器人。运行时只读取已发布的只读知识库，不抓网页、不在线修改知识卡；关键检索环节故障会明确提示，回答模型可以在证据基础上自然改写，低置信度草案也直接发送。

本地实现、Luna 清洗合并和安全审计已完成；资产会以只读交接包形式放到 VPS，暂不启动 Bot 或改动现有 NapCat / Minecraft Bridge 服务。

## 模型网关

四个模型角色统一使用大连理工大学大模型网关 `http://aigw.dlut.edu.cn/v1`，不要把 API key 写入仓库、交接包或日志。已用脱敏测试 Key 验证：网关的模型目录包含 `Qwen3.5-9B`、`bge-m3`、`Qwen3-Reranker-8B` 和 `Qwen3.5-35B-A3B`；`bge-m3` 的 `/embeddings` 返回 1024 维有效向量，`Qwen3-Reranker-8B` 的 `/rerank` 和两个 Chat 模型的 `/chat/completions` 均返回 200。模型页面只展示 Chat 端点并不代表 Embedding 路由不可用，具体探测记录见 [docs/model-gateway-validation.md](docs/model-gateway-validation.md)。

`.env.example` 已按该网关填写默认模型名；实际部署只需在 VPS 的运行时环境注入新 API key。网关额度、分组价格和可用性仍以控制台为准。

## 本地开发

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

项目数据生产链与运行时隔离：Luna 只负责离线抓取、清洗和候选卡生成；Codex 负责审核、冲突处理、评测和只读版本发布。

## 检索与证据口径

- 线上采用精确匹配、加权 BM25、中文三元组、sqlite-vec 四路召回，再经 RRF 和 Reranker；任一路关键依赖异常都明确报错，不降级为模型常识回答。
- 受众由程序固定为本科生；指定校区查询仍保留空校区和“全校”卡。
- 回答正文默认最多300字，模型不得生成链接；允许基于已选卡忠实改写、压缩和跨卡组织，输出 `confidence`、`needs_review` 与可选来源提示；这些只写入内部质量记录，不会阻塞发送。
- 程序只硬校验答案非空、长度、模型不得伪造 URL、引用卡必须来自检索结果；不再用字符串包含关系判断“是否忠实改写”。
- 默认 `LUNA_ANSWER_MODE=draft`；`strict` 仅用于审计回归，不是生产口径。
- 父卡上下文不仅要同源，还必须在构库时覆盖子卡的时效、校区和受众证据作用域；运行时会按当前查询再验一次，防止旧版或手工修改的数据库把历史/异校区父文带进回答。
- RRF 仍保留Top50，但会先装载完整候选池，再在固定12张预算内优先覆盖独立事实；同一事实最多保留2个不同来源交给 Reranker 判断，并预留1张干净导航卡。不会因前24名重复卡过多而饿死后续独立事实，也不会增加远端精排调用量。
- 只有无原文导航卡命中时，程序直接给出无事实的官方页面提示，不调用回答模型。
- `kb_faculty.csv` 只用于隔离审计和负样本评测，不进入候选卡、Embedding 或生产库。

### 回答质量口径

回答是“证据辅助草案”，不是逐字转录。程序保留真实来源、模型置信度和 `quality_notes`，但草案直接发送；你在群里指出问题后再修知识卡或提示词。评测输出质量报告而非把每个改写错误都变成发布阻断。伪造链接和 faculty 泄漏仍是硬安全门。

## Luna 批处理

已有 URL 和描述的 `verify_refresh_and_extract` 任务先做禁网清洗；缺正文或缺 URL 的任务另建工作区联网处理，不能混批。

```powershell
campus-qa-kb luna-prepare `
  --tasks work\luna_tasks.jsonl `
  --workspace work\luna_workspace_v5 `
  --instructions docs\luna-worker-protocol.md `
  --clean-instructions docs\luna-clean-protocol.md `
  --batch-size 5 `
  --max-batch-chars 12000 `
  --utf8-json `
  --lanes core_kb secondary_kb `
  --actions verify_refresh_and_extract

pwsh -NoProfile -File scripts\run_luna_cleaning.ps1 `
  -Workspace work\luna_workspace_v5 `
  -MaxBatches 37 `
  -Model gpt-5.6-luna `
  -BatchTimeoutMinutes 12
```

运行状态写入 `state/status.jsonl`，确定性报告写入 `reports/`，每批日志写入 `logs/`。runner 对每批设置硬超时，输出不通过时只做一次禁网契约修复。

全部批次结束后重新校验并合并；默认有任一缺失或失败批次就拒绝生成全集：

```powershell
campus-qa-kb luna-collect `
  --workspace work\luna_workspace_v5 `
  --output work\luna_v5_collected.jsonl
```

只有阶段性检查时才可加 `--allow-partial`；partial 文件不能进入正式审核和发布。

已知 URL 的 `fetch_failed` 不重复撞同一死链接。v6 全量收集后，将失败项转换为新的官方搜索任务，再交给 Luna 独立处理：

```powershell
campus-qa-kb rescue-search-tasks `
  --tasks work\luna_tasks.jsonl `
  --results work\luna_v6_collected.jsonl `
  --output work\luna_v6_rescue_tasks.jsonl
```

转换后保留原 `source_id`，清空待抓 URL，并把原链接标为“仅供定位、不可作证据”。

站群文章无需全部抓取即可先形成只读导航目录：

```powershell
campus-qa-kb catalog `
  --web <DESKTOP>\智能体交接包\data\web_plus_index.csv `
  --output work\source_catalog_2026_v2_reviewed.jsonl `
  --report work\source_catalog_2026_v2_review.json `
  --as-of-year 2026
```

目录卡只有官方标题和链接，没有正文、事实或模型摘要；命中时走程序生成的导航回复。Luna 后续仅按评测缺口和高频问题抓取文章，将少量目录卡升级为带逐字证据的事实卡。`review-merge` 只允许同一 URL 的 `catalog_only → success` 单向升级；URL 漂移或两个不同正文修订会 fail-closed。

GitHub 项目对比与不部署完整 RAG 平台的依据见 [docs/github-project-review.md](docs/github-project-review.md)。

## 当前验证

```powershell
python -m pytest -q
python scripts\benchmark_retrieval.py `
  --output work\benchmark_v2 `
  --cards 3652 `
  --dimension 1024 `
  --iterations 80
```

当前154项测试通过；3652卡、1024维的四路并行本地召回在Windows复测中 P95 为29.9ms，包含Top50装载和12张候选分配的完整本地检索前端 P95 为31.7ms；2个并发请求的200次压力检查无结果不一致，双请求批次 P95 为58.1ms。该数字不是ARM64 VPS承诺值，1.5核容器仍需实机复测。模型响应体硬限制2MiB，离线 Embedding 每批最多32卡，会话历史最多保留2048个活跃用户键，消息去重表最多保留4096个 ID；构库和评测以同一份不可变输入字节同时解析与计算摘要。正式评测逐题生成评测账本，发布门从账本重算题型、Recall、最终引用、克制率和延迟，不接受手写 passing 汇总；账本不保存 faculty 正文或问题原文。faculty 评测还必须绑定交接包中固定85条隔离集的已批准 SHA-256，替换文件、空行或重复行不能获得发布资格。构库、评测和运行时绑定同一非密钥模型配置及代码哈希，启动就绪会探测 Planner、Embedding、Reranker、Answer 四个模型契约，任一不一致会拒绝启动。大工网关四模型端点已于2026-08-06实测通过；300题评测集已由用户确认并按原始字节冻结为 `work/evaluation_20260807.jsonl`，SHA-256 为 `184c8bebab44d6f41e62939482b9e3677803a29d987f5083795d4d540c2a8334`，冻结记录为 `work/evaluation_20260807_freeze.json`。正式 `knowledge.sqlite` 尚未发布：构库和评测仍需运行时注入轮换后的网关密钥。Luna 最终审核已闭合：3227 张卡（approved 3024、downgraded 190、rejected 13、pending 0、conflict 0），审核 JSONL SHA-256 为 `7bead5d33b2b3022f2da59088bb12cbba842985c56c6a1dbb80ef739f6e64f20`。VPS 只需接收资产、注入运行时密钥并完成 QQ/NapCat 登录接入。
