# GitHub RAG / 智能体项目选型复核

复核日期：2026-08-06  
结论适用范围：商学院本科生 QQ 群答疑机器人

## 1. 约束先于项目热度

- VPS 为 Oracle Linux ARM64、4核、约22GiB内存；现有服务实测约占14GiB，不能把剩余内存全部交给 Bot。
- NapCat 与 Minecraft Bridge 保持原样；当前阶段不部署、不改 NapCat。
- 运行时必须四路召回、Reranker、可追溯来源和 fail-closed，不能在某一路故障时退回模型常识；回答允许忠实改写，不把逐字字符串门禁当成产品能力。
- 知识量是几千篇文章、几千张卡，不预期快速增长到百万级文档。
- Luna 负责离线抓取与清洗，线上 Bot 不抓网页、不写知识库。

因此，比较标准不是“平台功能最多”，而是：ARM64 可用性、额外常驻内存、能否保持证据约束、是否侵入现存服务、是否给当前规模带来真实收益。

## 2. 复核结果

GitHub 元数据为复核当日快照，star 仅反映生态热度，不作为架构决定依据。

| 项目 | GitHub 快照 | 官方部署/结构信号 | 对本项目的判断 |
|---|---:|---|---|
| [Dify](https://github.com/langgenius/dify) | 151,459 stars；1.16.1 | 官方最低2核/4GiB；默认 Compose 含 API、worker、Web、PostgreSQL/MySQL、Redis、sandbox、plugin daemon、nginx及向量库选项 | 适合多人可视化编排，不适合做当前线上检索内核。会重复已有查询规划、检索、审核和发布链，并增加多个常驻进程。 |
| [RAGFlow](https://github.com/infiniflow/ragflow) | 86,902 stars；v0.26.4 | 官方最低4核/16GB/50GB；默认依赖 Elasticsearch、MySQL、MinIO、Redis；官方镜像当前不提供 ARM64 | 明确排除。单它的最低内存就超过安全余量，且 ARM64 还需自行构建。 |
| [QAnything](https://github.com/netease-youdao/QAnything) | 14,058 stars；v2.0.0 | 官方 RAM ≥20GB；使用 Milvus、MinIO、MySQL 等；最新 release 仍为2024-08 | 明确排除。资源需求接近整台 VPS，且维护节奏与当前需求不匹配。 |
| [MaxKB](https://github.com/1Panel-dev/MaxKB) | 22,410 stars；v2.10.4-lts | 官方给出单条 `docker run`；数据层为 PostgreSQL + pgvector；构建工作流覆盖 linux/arm64 | 不替换当前运行时。若以后要给非技术人员一个知识库管理后台，它是最值得单独做隔离 PoC 的候选。 |
| [LightRAG](https://github.com/HKUDS/LightRAG) | 38,544 stars；v1.5.5 | 图谱+向量双层检索；抽取阶段需要额外 LLM；默认本地存储只适合开发，生产建议 PostgreSQL、MongoDB 或 OpenSearch 等后端 | 不采用图谱运行时。校园办事信息主要是程序型事实、时效和范围过滤，图谱抽取成本大于收益；可借鉴增量更新和段落切分。 |
| [Kotaemon](https://github.com/Cinnamon/kotaemon) | 25,690 stars；v0.12.0 | 提供全文+向量+Reranker、文档预览和细粒度引用；官方测试 linux/arm64 镜像 | 适合作为文档 QA 和引用交互参考，不适合直接承接 QQ 消息策略、单来源证明和只读发布门槛。 |
| [Haystack](https://github.com/deepset-ai/haystack) | 26,117 stars；v3.0.0 | 通用检索/工作流编排库 | 借鉴模块接口和评测方法，不引入为线上依赖；当前实现规模不足以抵消框架抽象与迁移成本。 |
| [LlamaIndex](https://github.com/run-llama/llama_index) | 51,405 stars；v0.14.23 | 通用文档、索引和 Agent 生态 | 同上。适合作为连接器/索引策略资料库，不替换已受测的轻量内核。 |
| [sqlite-vec](https://github.com/asg017/sqlite-vec) | 7,978 stars；v0.1.9 | 纯C、无外部依赖，覆盖Linux/Windows/macOS/Raspberry Pi，可在 `vec0` 中保存向量及过滤 metadata；官方明确仍是pre-v1 | 继续采用并锁定0.1.x；它匹配ARM64和几千卡规模，但schema兼容、扩展加载和精确结果必须由本项目测试兜底，不能把pre-v1当稳定平台。 |
| [txtai](https://github.com/neuml/txtai) | 12,799 stars；v9.12.0 | 将稀疏/稠密向量、关系数据库、图和工作流放进统一框架 | 借鉴“RAG不只向量检索”和SQL/多通道评测思路；当前不引入其Embedding数据库和Agent工作流，避免与已受测的四路召回、发布证明重复。 |

## 3. 决策

继续使用自研轻量运行时：只读 SQLite + FTS5 + 中文三元组 + sqlite-vec 四路召回，RRF 后接独立 Reranker，再进入逐句证据校验。它不是因为“SQLite 一定比 OpenSearch 强”，而是当前几千卡规模下可以用更少内存实现可复现的精确检索，同时把范围过滤、来源所有权和发布证明写成确定性约束。

不把 Dify、RAGFlow、QAnything、LightRAG 或 Kotaemon 部署到现有 VPS。MaxKB 只保留为未来管理后台候选，且必须在隔离环境做 ARM64、空载内存、导入/导出和权限 PoC 后再谈接入。

## 4. 借鉴而不搬运

- 从 Dify 借鉴：模型配置管理与工作流可观测性，不引入其整套运行时。
- 从 RAGFlow / Kotaemon 借鉴：混合召回、Reranker、命中片段可视化和低相关度告警。
- 从 LightRAG 借鉴：增量更新、内容哈希和语义段落切分；不做知识图谱抽取。
- 从 MaxKB 借鉴：后续人工审核后台、批量导入导出和任务状态界面。
- 从 Haystack / LlamaIndex 借鉴：离线评测组织和 Adapter 设计，不把核心证据链委托给通用框架默认行为。
- 从 sqlite-vec 借鉴并直接使用：单文件向量索引和metadata预过滤；同时把pre-v1兼容风险锁进schema版本、构库检查和启动健康检查。
- 从 txtai 借鉴：稀疏+稠密+关系过滤的统一检索观念，不引入其更宽的图谱与Agent运行时。

## 5. 与参考文档的真实差距

当前系统对“已经清洗并发布的材料”更强：四路召回、Reranker、可追溯来源、faculty 隔离和失败关闭都可测试。它对“刚发布、尚未离线抓取的新网页”仍弱于参考系统的实时站群全文检索，这是明确缺口，不用降级话术掩盖。

补偿路径是 Source Catalog + Luna 增量升级：2898张目录卡先提供官方页面导航，2026年的167篇候选由 v8 抓正文并升级事实卡；后续根据固定评测缺口和真实高频问题继续抓取。线上仍不临时读网页，也不让模型根据目录标题猜正文。

## 6. 可核验来源

- 各仓库主页与 release：上表项目链接及其 Releases 页面。
- Dify 最低配置：[README](https://github.com/langgenius/dify/blob/main/README.md)；服务结构：[docker-compose.yaml](https://github.com/langgenius/dify/blob/main/docker/docker-compose.yaml)。
- RAGFlow 最低配置、依赖和 ARM64 说明：[README](https://github.com/infiniflow/ragflow/blob/main/README.md)。
- QAnything 20GB RAM 与依赖：[README](https://github.com/netease-youdao/QAnything/blob/qanything-v2/README.md)。
- MaxKB 单容器启动、PostgreSQL/pgvector：[README](https://github.com/1Panel-dev/MaxKB/blob/v2/README.md)；ARM64 构建：[workflows](https://github.com/1Panel-dev/MaxKB/tree/v2/.github/workflows)。
- LightRAG 存储和模型要求：[README](https://github.com/HKUDS/LightRAG/blob/main/README.md)。
- Kotaemon 混合检索、引用与 ARM64：[README](https://github.com/Cinnamon/kotaemon/blob/main/README.md)。
