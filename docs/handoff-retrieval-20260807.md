# 检索层交接包

生成时间：2026-08-07。承接 `campus_qa_retrieval_handoff_20260807.md`。
分支 `retrieval-selection-fix`（8 个 commit，`main` 无提交，用户规定不许直接提 main）。
测试：`uv run --extra dev pytest`（`.venv` 里没装 pytest）。

本文所有数字都是跑出来的，不是推断。推断的地方会写明「未验证」。

---

## 一、原交接包的根因排序是错的，已证伪

原包把 `RerankCandidateAllocator` 列为第一嫌疑人。实测：**候选分配器几乎不丢
正确卡（198/200 存活）**。它不是凶手。

真凶在 `StrongRetriever.retrieve()`：`if fact_cards:` 在导航卡之前无条件判定，
不比较分数。只要有任何一张 fact 卡越过 `min_rerank_score=0.35`，导航卡就出局，
哪怕它精排分更高、首阶段第一。

而知识库对正常查询只有 **292 张导航卡 + 28 张事实卡**（见第三节），
固定评测 200 道正例里 **147 道（73.5%）的标准答案是导航卡**。

> **72.5% 的正例结构性不可达。** 十题冒烟里 7 题「相关性不足」是同一个 bug 的
> 7 次触发，不是 7 个问题。

快速通道没有制造这个 bug。`query_fastpath.py` 把「怎么申请」的 `申请` 面拆掉后
`required_facets=[]`，只是掀掉了遮丑布——在那之前导航卡是靠「证据面缺失」这条
**失败分支**歪打正着才出场的。**不要回退快速通道**，那是把病灶重新盖上。

---

## 二、已完成的改动

| commit | 内容 |
|---|---|
| `1b67a47` | 取消 fact-first 硬序，改按「精排置信档位 → 首阶段名次」选卡 |
| `f712497` | 导航配额 1→3、budget 12→16；导航回答独立状态；重试 5→2；发布链哈希拆分；`.gitattributes` |
| `782b0bb` | 修正 reranker 窄带注释（原数字被实测推翻） |
| `3ed47ba` | 事实卡 validity 重标审计工具 |
| `6daaf2d` | 缺口清单 `docs/kb-coverage-gaps.md` |
| `fa088fa` | 非循环冒烟集 + 收窄重标待判 |
| `42be2ab` | 实测证伪「导航卡文档过短被压分」 |
| `71e45ab` | 离线构库单独的重试预算 |

### 选卡逻辑（核心）

精排分先按 `RERANK_TIE_MARGIN=0.05` 折叠成置信档，再按 `(档位, 首阶段名次)`
字典序排。窄带 → 全落一档 → 首阶段独立决定；精排真能分开 → 档位分裂 → 精排照样
压过首阶段。**没有权重常数要调**，在「信首阶段」和「信精排」之间按精排自己表现出
的区分度自动滑动。档位相同时**绝不按卡类破平**，那会把刚删掉的 fact-first 悄悄
装回来。

拒答分支数量前后都是 4 个，没有新增任何 `InsufficientEvidence` 路径。

### 离线夹具实测（`scripts/replay_gold_survival.py`，零网关、秒级）

```
                                  BEFORE      现在
gold 进首阶段池                  200/200    200/200
gold 活过 allocator              198/200    200/200
gold 被选中（宽松，出现在答案里）   9/200    200/200
gold 排第一（严格）                3/200    198/200   = 99.0%
  └ navigation gold                0/147    147/147
  └ fact gold                      9/53      53/53
```

夹具用对抗性 reranker（窄带 + 按文档长度排序，导航卡因无正文永远最短），
所以 99% 是**下界**。宽松/严格两个口径都报，因为 P2 改成输出 3 条链接后
「gold 出现在答案里」会天然变好看，不能当改进读。

### 十题真实网关复测

```
最慢 48.16s → 6.22s     p95 4.53s     10 秒内 9/10 → 10/10
分阶段：answer_model 5.28s（大头）/ planner 2.1–2.5s / embedding 0.5–0.9s
        reranker 0.4–0.8s / 本地 SQLite 四路召回 0.01–0.04s
```

奖学金（主线案例）和校园网已修复。但**严格口径 top-1 主题正确只有 5–6/10**，
没到 ≥8/10 的门。剩下的错**全部错在第一阶段**——选卡层是忠实的，瓶颈已经转移。

---

## 三、必须知道的反直觉事实

**1. 活库只有 320 张卡，不是 3214。**

```
              current  historical  unknown
navigation        23        2776       269
fact              28         118         -
```

正常查询过滤 `validity != 'historical'` → 292 nav + 28 fact。**这是导航机器人**，
91% 是导航卡。292 张活导航卡里 269 张是 `unknown`，一旦离线侧把 unknown 规范化成
historical，生产库当场塌到 23 张。

**2. 固定评测集是循环的。** `work/evaluation_20260807.jsonl` 200 道正例的问题文本
**200/200 逐字等于 gold 卡自己的 `standard_question`/`generated_questions`**，而
这些字段进了索引。`recall_at_50`/`recall_at_5` 恒 ~100%，**测不出任何检索质量变化**。
只有 `answer_card_match_rate` 还有效。替代品见第五节。

**3. fact 卡 `validity=unknown` → `review.py:377` 判 PENDING → 不进库。**
上一批 Luna 产出 44 张 fact 卡里 13 张踩了这个，白抓。**事实卡只能给
current 或 historical。**

**4. `parent_scope_covers_child` 要求 `parent_validity == child_validity` 严格相等。**
重标 validity 时父子卡必须一起动，否则 build 直接失败。

**5. `luna_final_reviewed_*.jsonl` 不带 embedding**（0/3227），重 build 会重新调
3214 次 embedding，且 `built_at` 是时间戳，`knowledge_sha256` 必然变。

**6. 网关是时间窗限流，不是配额耗尽。** 全量 3214 张卡（约 101 个串行请求）成功
构过库。实测 32 条一批 embedding 1.52s。连发十题会 429，隔几分钟自愈，
**embedding 端点最先被限，rerank 端点不受影响**。

---

## 四、已证伪的假设（别再走这些路）

| 假设 | 结论 | 证据 |
|---|---|---|
| allocator 丢了正确卡 | ❌ | 198/200 存活 |
| 导航卡因文档过短被压分 | ❌ **反的** | 导航卡 34 字/0.927，事实卡 190 字/0.875，长度-分数相关 **−0.116** |
| 补长导航卡文档能改善 | ❌ | `+standard_question` 均值 −0.0117、翻转 top-1 5/6（帮 2 次坏 2 次）；`+retrieval_text` −0.0182 更差，还要放松安全测试 |
| 2776 张 historical 导航卡值得捞 | ❌ | 全是学院新闻归档（business/sem/ip/panjin/news/mba），捞回来只加噪声 |
| ehall 入口被误标 historical | ❌ | 零张，它们没有 publishTime，本来就在 `unknown`（活的） |
| 网关额度耗尽 | ❌ | 见上，是时间窗限流 |
| 用「研究生\|硕士\|推免」过滤研究生内容 | ❌ 自伤 | 会砍掉 8 条**保研推免**——推免的对象就是应届本科生。正确判据是**服务对象**，不是主题 |

`_rerank_document` 的证伪结论已写进它的 docstring，下一个想「修」它的人会先看到。

---

## 五、工具

| 工具 | 用途 | 成本 |
|---|---|---|
| `scripts/replay_gold_survival.py` | 离线重放评测集，跑真实 `retrieve()`，只关向量通道 + 对抗性 reranker。三道门有退出码 | 秒级，零网关 |
| `scripts/audit_fact_validity.py` | 重标提议。读标题**和证据文本**：`第N条` → 常设；证据里有截止日期 → 过期 | 秒级，零网关 |
| `scripts/run_smoke_set.py --check-only` | **循环性检查**：任何问题逐字命中索引就拒跑（投毒验证过，exit=2） | 秒级，零网关 |
| `scripts/run_smoke_set.py` | 44 题真实验收（13 回归护栏 / 25 缺口 / 6 负例），退出码只认回归 | 需要 key |
| `evaluation/smoke_non_circular.jsonl` | 群里口语问法，期望值是主题关键词不是 card id，所以卡还没建也能先列 | — |

> **补完卡必须重跑循环性检查。** 新卡的 `generated_questions` 可能正好覆盖某道
> 冒烟题，那题就悄悄失效了。

---

## 六、当前进度与待办

### 已就绪

- **抓取目标清单**：`work/luna_tasks_gapfill_20260807.jsonl`，142 条，
  契约校验 142/142。ehall 19 + kb_clean_c 去重后 123，覆盖全部 18 个缺口主题，
  **站群四大站一条没有**。
- **重标提议**：41 recover / 69 keep / 19 待人工。活 fact 卡 30 → 71。

### 待办顺序

1. 重标 validity（零成本，不用抓）→ `--write` 出新 JSONL
2. 抓 142 条 → 用第五节的验收脚本过一遍
3. 合并 → 循环性检查
4. **一次** build + evaluate + activate
5. 带 key 跑 44 题验收

### 抓取执行注意

- **ehall 19 条是 SPA**（`visitService?service_id=`），`requests` 大概率只拿到空壳。
  先试 2 条，抓不到就转找对应的静态「办理指南」页。这 19 条是缺口 B 类核心，也最难抓。
- 60 条 `mp.weixin.qq.com` 会失效，抓到即定版，`content_hash` 存好。
- **`seed_description` 只帮 Luna 判断值不值得抽卡，绝不能当 `evidence_quote`。**
  证据只能来自抓回来的正文。build 的 `evidence_quote in clean_text` 拦不住这种情况
  （Luna 只要把 desc 同时当 clean_text 就通过），所以要写进 Luna 提示词。
- 上批遗留待修：retry 中间态脏数据、`clean_text` 截断改 16000（**别去掉上限**，
  `evidence_quote`/`retrieval_text` 契约上限是 6000）。

### 需要用户拍板（我不替他决定）

1. **`answer_card_match_rate` 阈值 1.0 是否下调。** 建议等真实数字出来再谈——
   评测集循环，修好选卡后这个数可能直接冲到 0.95+，未必需要动产品口径。
   注意它只进 `quality_passed`，**不挡 activate**。
2. **重标后「带日期但现行」的卡可能给出过时信息**（如图书馆再次调整开馆时间）。
   这是「可能答错」vs「答不出」的取舍。证据里都带生效日期，回答模型会自然带出。

---

## 七、发布链（改过，行为和以前不同）

原来 `release_model_config()` 把 `runtime_code_sha256()`（哈希全部 `.py`）塞进
`build_report`，导致**改一行 QQ 适配器就作废一次知识库构建**，必须重 embed 3214 张卡。

现已拆开：

- `build_report` 只带决定产物内容的字段 + `build_code_sha256`（只哈希构建闭包，
  从 import 图算出来，有测试钉死：`attestation / contracts / errors /
  pipeline.build / scope_policy / vector` 六个模块）
- `evaluation_report` 带完整 `model_config`（含 `runtime_code_sha256`）+ `build_config`

保留的两条不变式，现在分开陈述：

```
evaluation_report.build_config == build_report.model_config   → 评测跑的是这个库
evaluation_report.model_config.runtime_code_sha256 == 当前     → 跑的代码就是评测过的代码
```

删掉的第三条「跑的代码 == 建库的代码」不是安全属性——运行时只读打开数据库，从不建库。

**结果：改运行时代码不再需要重 build，只需重评测。** 本轮后续三个 commit 都验证了
这一点（`build_code_sha256` 保持 `9e340d16` 不变）。

`.gitattributes` 固定 `*.py eol=lf`：`core.autocrlf=true` 且无 attributes 时，
同一 commit 在 Windows 和 Linux(VPS) 检出会产生**不同的代码哈希**，发布会莫名拒绝激活。

### 重试预算已按场景拆开

```
ONLINE_RETRY_POLICY   2 次，最坏等 1.5s   ← 群里有人等着
OFFLINE_RETRY_POLICY  6 次，最坏等 61s    ← 只有 _build 用
```

构库没有断点续传（embedding 只在内存里），一次没吸收的 429 会丢掉前面全部向量、
101 个请求从头再来。`_evaluate` **故意保持 online 预算**——它量的是用户能感知的
延迟，在那里吸收 30 秒卡顿会遮住它自己要守的门。有测试钉着。

---

## 八、还没做/不建议做

- **P1-2（补长 reranker 文档）**：已证伪，见第四节。不做。
- **主题锚点、reranker 分数校准**：原包的方案 A/C。选卡层现在是忠实的，
  真要做应等第一阶段的问题解决后再量。
- **第一阶段召回**：现在的瓶颈。真实问法下首阶段并不总对（`退费流程`、
  `本科生查看成绩` 的 fs#1 就是错的），而离线夹具因为评测集循环**看不见这一类问题**。
  这是补完卡之后的下一个战场。
