# 知识库补卡交接包

生成时间：2026-08-07。收件人：执行抓取与抽卡的一方（Luna / 抓取器驱动方）。
本包只讲**补卡**，不涉及在线检索层的改动。

---

## 一、任务

抓取目标清单已生成，直接可用：

```
work/luna_tasks_gapfill_20260807.jsonl      142 条，KnowledgeTask 契约校验 142/142
```

来源与构成：

```
ehall 办事大厅      19 条   ← 有入口、零正文，是最硬的缺口
kb_clean_c 去重后  123 条   ← query 列已对准缺口清单
──────────────────────────
                   142 条
```

覆盖 18 个缺口主题（之前 17 项为零）：出国交换 10、实习认定 10、空调热水 10、
保研推免 8、四六级 8、在读证明 7、毕业论文 7、校车时刻 7、成绩单打印 7、
学生证补办 7、成绩查询 6、宿舍调换 6、退费流程 6、教务系统密码 5、快递点 5、
学费核对 5、选课时间 5、选课退课 4，加 19 条办事大厅。

**站群四大站（business / sem / panjin / mba）一条都没有**，这是刻意的，见第三节。

---

## 二、最重要的一条：证据只能来自抓回来的正文

任务里的 `seed_description` 来自 `kb_clean_c.csv` 的 `desc` 列。它是**第三方整理
的答案摘要**（例如「登录教学管理系统即可查看」），不是官方页面原文。

> **`seed_description` 只用于判断这个 URL 值不值得抽卡。
> 绝不可写进 `evidence_quote` 或 `clean_text`。**

这条必须写进 Luna 提示词，因为**程序拦不住**：build 只检查
`evidence_quote in clean_text`，Luna 只要把 desc 同时当成 clean_text 就能通过。
一旦发生，卡片会挂着官方 URL、引用着那个页面上根本没有的文字——
**这比没有卡更坏，因为它看起来是权威的**。

抓不到正文就放弃这条，或降级成只有标题和 URL 的导航卡。不要用 desc 凑。

---

## 三、上一批（`fetch_cards_fast_20260807.jsonl`）的教训

1080 个来源 → 58 个有产出 → **67 张卡**，其中净可用增量 ≈ 0。

**抓错了地方。** 实际站点分布：business 349 / sem 200 / panjin 108 / mba 92，
而 ehall / jwc / ecard / career **各 0 条**。646 条 `out_of_scope` 是抓取器自己
在正确判断「学院新闻不是可作答材料」。

架构文档 116 行原文：

> 「Luna 按正式评测缺口和问题热度逐步将目录卡升级为事实卡，**无需先抓完全部文章**」

产出的 67 张卡实际是：开学典礼、获奖新闻、评选结果公示、答辩安排、学术讲座。
少数有用的又恰好是库里**已经过量**的主题——学生社团 ×3（已有 9 张）、
助学贷款 ×2（已有 7 张）、户口迁移 ×2（已有 9 张）、心理咨询 ×2（已有 6 张）。

20 个缺口主题里 17 项仍为零。

**结论：不要再抓 `web_plus_index` 站群目录。** 已抓的 983 条不重抓，按现状归档。

---

## 四、硬性契约（违反会 build 失败或卡被静默丢弃）

### 会静默丢卡的（最危险，不报错）

| 规则 | 后果 |
|---|---|
| **fact 卡 `validity` 不能是 `unknown`** | `review.py:377` 判 PENDING → 不进库。上一批 44 张 fact 卡里 **13 张**踩了这个 |
| **父子卡 `validity` 必须严格相等** | `parent_scope_covers_child` 要求完全一致，否则 build 报 parent scope mismatch |

fact 卡只能给 `current` 或 `historical`。判据：描述**办事流程/规则/联系方式**且
内容不随年度失效 → `current`；描述**某一届/某学年/某次截止**的具体安排 → `historical`。

### 会直接报错的

| 规则 | 检查点 |
|---|---|
| `canonical_url` 必须是官方域 | `dlut.edu.cn` 子域或 `mp.weixin.qq.com` |
| `official_domain` 必须等于 URL 的 host | 不一致直接拒 |
| `content_hash` 必须匹配 `clean_text` | 上一批有 2 条不匹配（retry 中间态脏数据） |
| `evidence_quote` 必须是 `clean_text` 的**字面子串** | 上一批 1 条违反 |
| `audience` 必须是 `本科生` | 字段值，不看内容 |
| `catalog_only` 的来源不能出 fact 卡 | |
| `source_id` 不能含 `faculty` | |
| fact 卡必须有 `subject_key` 和 `fact_key` | 缺了判 PENDING |

### 长度上限

```
title ≤ 500        summary ≤ 2000       evidence_quote ≤ 6000
retrieval_text ≤ 6000                   search_text() ≤ 16000
generated_questions ≤ 20 条             aliases ≤ 30 条
keywords ≤ 50 条                        facets ≤ 20 条    单值 ≤ 500
```

`clean_text` 截断阈值建议 **16000**（上一批是 8000，可能截掉 evidence 引用段）。
**不要去掉上限**——`search_text()` 有 16000 的硬顶。

---

## 五、抓取执行注意

- **ehall 19 条是 SPA**（`visitService?service_id=...`），`requests` 大概率只拿到
  空壳。**先试 2 条**，抓不到就转找对应的静态「办理指南」页
  （`jxyxbzzx` / `workflow` / `teach` 域下那些就是这类）。
  这 19 条价值最高也最难抓，不要在上面硬耗。
- **60 条 `mp.weixin.qq.com`**：`_official_host` 显式放行，但公众号文章会失效，
  抓到即定版，`content_hash` 存好。
- **研究生服务已剔除 7 条**（「（研究生）学生证补办」「宿舍调整申请（研究生）」
  「专业学位硕士学费」等）。判据是**服务对象**不是主题——
  **保研推免 8 条全部保留**，推免的对象就是应届本科生。
- **待修**：retry 中间态脏数据导致的 hash 不一致（上一批 2 条）。

---

## 六、交付与验收

交付一份 Luna 协议格式的 JSONL（`LunaSourceResult` 数组，同上批格式即可）。

验收会跑这些（全部离线、零网关）：

```
契约校验      LunaSourceResult.from_dict + validate()
build 门      官方域 / 域名一致 / faculty / dataset / fetch_status /
              audience / evidence 字面子串 / subject+fact key /
              fact 卡 validity 不为 unknown / card_id 去重 / 与现有库冲突
缺口命中      按 20 个缺口主题统计 fact 卡与 nav 卡数量
```

**验收看的是 `fact + current` 的净增量**，不是卡片总数。上一批 67 张卡里
只有 4 张是 `fact+current`，且都与缺口无关。

合并后还要跑 `scripts/run_smoke_set.py --check-only`：新卡的
`generated_questions` 可能正好逐字覆盖某道验收冒烟题，那题会悄悄失效。

---

## 七、优先级

若额度或时间不够，按这个顺序砍：

1. **教务四项**：成绩查询、选课退课、教务系统密码、绩点计算 —— 覆盖最大提问量
2. **ehall 前 4**：本科生专项奖学金申请、校园卡充值、就业指导预约、心理测评
3. **生活类**：快递点、校车时刻、宿舍调换、空调热水
4. 其余

完整缺口分析见 `docs/kb-coverage-gaps.md`。

---

## 八、不要做的

- 不要再抓 `web_plus_index` 站群目录（business / sem / panjin / mba / news）
- 不要用 `seed_description` 当证据
- 不要给 fact 卡标 `validity=unknown`
- 不要抓服务对象是研究生的事项
- 不要为了填满数量而抽卡——**净可用增量是 `fact+current`，不是卡片总数**
- A 类主题（户口迁移、评奖评优、学生证/玉兰卡、食堂、教务系统、重修补考、
  图书馆开馆时间）**不用抓**：卡已经在库里，只是被标成 historical，
  `scripts/audit_fact_validity.py` 重标即可拿回 41 张
