# Luna 抽卡交接包

生成时间：2026-08-07。收件人：Luna（候选卡抽取）。
本包只讲**从已抓正文抽卡**。抓取已完成，不需要再联网。

---

## 一、输入

```
work/luna_input_gapfill_20260807.jsonl      74 条来源
```

每条是 `LunaSourceResult`，`clean_text` 已就绪、`candidate_cards` **为空**，你的任务
就是把它填上。字段说明：

| 字段 | 状态 |
|---|---|
| `clean_text` | **已清洗、已修复编码**，这是唯一的证据来源 |
| `seed_query` | 这条是冲哪个缺口抓的（见第五节），**用它判断该不该抽卡** |
| `seed_description` | 第三方搜索摘要，**只用于判断值不值得抽，绝不可当证据**（见第二节） |
| `title` / `canonical_url` / `content_hash` | 已校验，不要改 |

正文长度：中位数 1364 字，54 条 ≥1000 字，最短 318 字。

### 这 74 条已经过滤掉了什么

原始抓取 142 条任务、131 条「成功」，但成功里混着：

```
42  登录墙/权限页    ← 办事大厅等需登录页面，抓到的是「统一身份认证」表单
59  编码乱码        ← 微信文章 UTF-8 被按 Mac Roman 解码，已修复 56 条
 3  站外噪声        ← 附属学校小学分班通知、高考查分推文
12  正文过短        ← <300 字，抽不出流程
```

**办事大厅（ehall）34 条已整体放弃**，不必等、不会补。原因见第六节。

---

## 二、最重要的一条：证据只能来自 `clean_text`

`seed_description` 是第三方整理的答案摘要（例如「登录教学管理系统即可查看」），
**不是官方页面原文**。

> **`evidence_quote` 必须是 `clean_text` 的字面子串。
> 绝不可用 `seed_description` 拼证据。**

程序拦不住这件事：build 只检查 `evidence_quote in clean_text`。如果你把
`seed_description` 同时写进 `clean_text`，检查照样通过，但产出的卡会**挂着官方 URL、
引用着那个页面上根本没有的文字**——这比没有卡更坏，因为它看起来是权威的。

`clean_text` 里找不到答案，就**不要抽事实卡**，降级成导航卡（只有标题和 URL）。

---

## 三、会静默丢卡的三条（最危险，不报错）

| 规则 | 后果 |
|---|---|
| **fact 卡 `validity` 不能是 `unknown`** | `review.py:377` 判 PENDING → 不进库。上一批 44 张 fact 卡里 **13 张**踩了这个，白抽 |
| **fact 卡必须有 `subject_key` 和 `fact_key`** | 缺任一个判 PENDING |
| **父子卡 `validity` 必须严格相等** | `parent_scope_covers_child` 要求完全一致，否则 build 直接失败 |

### validity 怎么判（这是上一批最大的问题）

fact 卡**只能**给 `current` 或 `historical`：

- **`current`** —— 描述**办事流程 / 条件 / 材料 / 地点 / 联系方式 / 规则**，
  内容不随年度失效。带日期不等于过期：
  「图书馆自 2025 年 5 月 8 日起调整开馆时间」描述的就是**现行**开馆时间 → `current`
- **`historical`** —— 描述**某一届 / 某学年 / 某次截止**的具体安排：
  「2025 级新生报到」「2025-2026 学年助学贷款额度」「7 月 9-10 日申报」→ `historical`

判据参考 `scripts/audit_fact_validity.py`（已实现同一套规则）：

```
标题含 20XX级 / 20XX届 / 20XX年度 / （20XX年） / 20XX-20XX学年 / 第N届   → historical
标题含 寒假/暑假/节假日/N月上中下旬/补测                                  → historical
标题含 暂停/施工/临时/延期/举办/召开/历史通知                              → historical
正文含 第N条（规章条款）                                                 → current
正文含 截止 / N月N日前 / N月N日-N日 / 考试时间：                          → historical
其余含 流程/办法/规定/条件/材料/入口/地点/联系/电话/开馆                    → current
```

`navigation` 卡可以用 `unknown`（活库里的导航卡大多是 unknown，正常可检索）。

---

## 四、会直接报错的契约

| 规则 | 说明 |
|---|---|
| `audience` 必须是 `本科生` | 字段值。**服务对象是研究生的不要抽卡**（判据是服务对象不是主题——推免/保研的对象是应届本科生，要抽） |
| `campus` 必须在允许集合内 | `""` / `全校` / `凌水` / `开发区` / `盘锦` 及其组合 |
| `catalog_only` 来源不能出 fact 卡 | 本批全是 `success`，不受影响 |
| `card_id` 全局唯一 | 不能和现有库冲突 |

### 长度上限

```
title ≤ 500        summary ≤ 2000        evidence_quote ≤ 6000
retrieval_text ≤ 6000                    search_text() 合计 ≤ 16000
generated_questions ≤ 20 条              aliases ≤ 30 条
keywords ≤ 50 条                         facets ≤ 20 条        单值 ≤ 500
```

---

## 五、抽什么：74 条按缺口分布

```
 7  保研推免      7  实习认定      7  空调热水      6  四六级报名
 5  宿舍调换      5  快递点        5  校车时刻      4  选课退课
 4  教务系统密码   4  退费流程      4  成绩单打印    3  成绩查询
 3  在读证明      3  学费核对      2  毕业论文      2  出国交换
 2  选课时间      1  学生证补办
```

**用 `seed_query` 对照正文**：这条正文如果**回答不了**它对应的那个问题，就别硬抽事实卡。
宁可少抽，也不要为了凑数把无关正文做成卡——上一批 67 张卡里净可用增量接近 0，
就是这么来的。

**验收看的是 `fact + current` 的净增量，不是卡片总数。**

### 已经过量、不要再抽的主题

活库里这些主题已经饱和，再抽只会挤占检索预算：

```
学生社团 9 张    助学贷款 7 张    户口迁移 9 张    心理咨询 6 张
```

---

## 六、办事大厅（ehall）为什么放弃

已登录验证过，不是权限问题：

```
visitService（34 条）  开放期外 →「服务暂时不可用」
                      开放期内 → 表单，不是说明文字
serviceCatalog / allService / serviceDetail  → 全部 404
首页 1083 字          → 导航框架 + 统计数字，零业务正文
```

办事大厅是**事务办理系统**，不承载「怎么办」的说明。办事指南在**各归口部门自己的
网站**上（教务处、学生处、网信中心、后勤处、场馆中心……），而 `kb_clean_c.csv`
抓的 123 条本来就是这些站点——ehall 是多余的一层。

**不要为 ehall 生成任何卡片。**

---

## 七、交付与验收

交付一份 `LunaSourceResult` JSONL（同输入格式，`candidate_cards` 填好）。

验收会跑（全部离线、零网关）：

```
scripts/audit_fetch_output.py     登录墙 / 乱码 / 噪声 / 正文过短 / 正文重复
契约校验                          LunaSourceResult.from_dict + validate()
build 门                          官方域 / 域名一致 / faculty / dataset /
                                  audience / evidence 字面子串 / subject+fact key /
                                  fact 卡 validity 不为 unknown / card_id 去重
缺口命中                          按 seed_query 统计 fact+current 的净增量
```

合并进库后还要跑 `scripts/run_smoke_set.py --check-only`：新卡的
`generated_questions` 可能正好逐字覆盖某道验收冒烟题，那题会悄悄失效。

---

## 八、不要做的

- 不要用 `seed_description` 当证据
- 不要给 fact 卡标 `validity=unknown`
- 不要为 ehall 生成卡片
- 不要抽服务对象是研究生的事项（推免/保研除外，那是本科生的事）
- 不要在学生社团 / 助学贷款 / 户口迁移 / 心理咨询上继续加卡
- 不要为了数量抽卡——`clean_text` 答不了 `seed_query` 就降级成导航卡或跳过
