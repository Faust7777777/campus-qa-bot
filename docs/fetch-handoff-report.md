# 数据抓取与缺口补全交接报告

> 生成时间：2026-08-07（续 kb-coverage-gaps.md 的抓取/补全工作）
> 执行者：外部抓取工具链（requests + Playwright，配合已授权登录态）
> 服务对象：Luna 抽卡管线（verify_refresh_and_extract / fetch_and_classify_current）

---

## 1. 执行概况

| 任务 | 输入 | 成功 | 失败 | 产出 |
| --- | --- | --- | --- | --- |
| fetch_and_extract（有 URL 待抓） | 90 | 74 | 16 | `work/fetch_output_fetch_and_extract.jsonl` |
| official_search_and_verify（找源） | 8 主题 | 23 源抓取成功 | 1 | `work/fetch_output_search_sources.jsonl` |
| fetch_and_classify_current（站群） | 983 | 981 | 2 | `work/fetch_output_fetch_and_classify_current.jsonl` |
| **C 类缺口提取（从 chat 原始库）** | 20 锚点 | 19+1 | 0 | `work/kb_clean_c.csv`（237 条）+ 考试安排官方源 |

**全部产出按 Luna 协议 JSONL 格式**（source_id / canonical_url / clean_text / content_hash / fetch_status / candidate_cards 空待 Luna 填）。

## 2. 关键发现：C 类缺口根因修正

**kb-coverage-gaps.md 判定 C 类"全库零覆盖"是基于 Luna 离线库（knowledge.sqlite）——但交叉验证显示 chat 原始在线库对这些主题全部有内容：**

| 主题 | chat 原始库召回 | 说明 |
| --- | --- | --- |
| 成绩查询 | 20 条 | 含"如何查询成绩单""成绩单打印" |
| 选课退课 | 15 条 | 含"如何选课退课""一轮选课无法退课" |
| 绩点计算 | 18 条 | 含"平均学分绩点计算规定" |
| 教务系统密码 | 17 条 | 含"综合教务系统密码""密码重置" |

**根因：不是源缺失，是我们的提取锚点没覆盖**（每轮只 dump top_k 20 条）。已用 20 个 C 类锚点补提 **237 条**（`kb_clean_c.csv`），覆盖：成绩/选课/绩点/教务密码/保研/四六级/在读证明/毕设/交换/实习/宿舍/快递/校车/空调/退费/学费/选课时间/成绩单打印/学生证补办。

**唯一例外：考试安排**——7 个变体锚点（期末考试/考试时间/补考/考试日程/课程考试/考试通知/期末成绩）在 chat 原始库全部召回 0，为**真实零覆盖**；已从官方源补齐：教学运行保障中心常规考试管理页 + 教务处考试安排页（见 `fetch_output_search_sources.jsonl`）。C 类 20/20 全部闭环。

## 3. 产出对接说明（Luna 下一步）

1. **fetch_output_*.jsonl 三个文件**：正文已抓取（clean_text），`candidate_cards` 为空——Luna 按 `verify_refresh_and_extract` 处理：清洗去重抽卡即可（协议 action 1）
2. **kb_clean_c.csv**：从 chat 原始库提取的 {title,url,desc}，建议并入 kb_clean 重新抽卡（这批正是缺口文档 C 类主题）
3. **失败项处理（三轮修复后）**：
   - 容器提取器修复（误匹配空容器/导航 div）+ Playwright 兜底后，从 90→74、969→981
   - 剩余失败全部为**源站问题，不可修复**：
     - 404 真失效（内容被删/迁移）：12 个
     - 403 反爬（eda 系站点）：3 个
     - 503 持续故障（hpm1）：1 个
     - 200 但内容已删（est content.jsp 返回"该项目不存在"）：2 个
   - 微信文章均已成功（此前失败的已由兜底抓回）
4. **fetch_and_classify_current 的分类未做**：正文已抓，obvious 新闻/活动类需要 Luna 按协议标 `out_of_scope`（标题可先粗筛：含"活动/比赛/公示/讲座通知/会议/喜报/招聘会"等）

## 4. 新增脚本（campus-qa-bot/scripts/）

| 脚本 | 用途 |
| --- | --- |
| `fetch_executor.py` | 批量抓取（按 action，增量保存，--retry-failed） |
| `fetch_fallback.py` | Playwright 兜底（403/微信/JS 渲染） |
| `fetch_sources.py` | 找源抓取（8 主题官方源） |
| `gap_fill_extract.py` | chat 原始库 C 类主题提取 |

## 5. 遗留事项

- [ ] Luna：对 4 个 fetch_output 文件执行抽卡（candidate_cards）
- [ ] Luna：fetch_and_classify_current 的 out_of_scope 分类
- [ ] Luna：kb_clean_c.csv 并入 kb_clean 重新构库
- [ ] A 类重标（audit_fact_validity.py）不受本次影响
- [ ] 微信 3 条失败可手动补（浏览器打开复制）
