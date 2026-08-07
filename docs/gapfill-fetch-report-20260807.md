# 缺口补卡抓取交付报告

> 生成时间：2026-08-07
> 对应交接包：`docs/handoff-kb-gapfill-20260807.md`
> 交付物：`work/fetch_output_gapfill_20260807.jsonl`（LunaSourceResult 数组，142 条）

---

## 1. 执行结果

| 类别 | 任务数 | 成功 | 失败 |
| --- | --- | --- | --- |
| ehall 办事大厅（SPA） | 34 | **34** | 0 |
| 微信文章 | 60 | **60** | 0 |
| 静态指南（jxyxbzzx/pjteach/teach/workflow/ecard 等） | 48 | 37 | 11 |
| **合计** | **142** | **131（92%）** | **11** |

## 2. 关键突破

**ehall 34 条 SPA 全部抓取成功**——requests 拿不到（空壳/CAS 跳转），改用
**Playwright + 已授权登录态**（`state.json`）在页面上下文渲染后取 `innerText`，
visitService 页面正文完整可用。此前批次 ehall 为 0 的缺口已闭合。

## 3. 契约合规（专家验收口径）

| 检查项 | 结果 |
| --- | --- |
| content_hash == sha256(clean_text) | ✅ 0 不匹配 |
| official_domain == URL host | ✅ 0 不一致 |
| source_id 含 faculty | ✅ 0 |
| clean_text 长度 > 16000 | ✅ 0（截断 16000 生效） |
| source_id 唯一 | ✅ 142/142 |
| candidate_cards | 全部为空（抽卡由接收方执行） |
| seed_description 作证据 | ✅ 未使用（证据仅来自抓回正文） |

## 4. 失败 11 条（源站问题，不可修）

| 域名 | 条数 | 性质 |
| --- | --- | --- |
| pjteach | 4 | 学生证补办办法 ×2、选课通知 ×2（404） |
| spap | 2 | 空调热水（重复 URL） |
| physics / dlutir / eda / gach / math | 各 1 | 404/反爬/失效 |

均为源站已删除或反爬拦截，非提取器问题。其中「学生证补办办法」在任务清单
中有静态替代页（ehall 学生证补办已成功），可由接收方按导航卡降级。

## 5. 交付说明

- 输出字段：source_id / dataset / canonical_url / title / official_domain /
  published_at / fetched_at / content_hash / clean_text / fetch_status /
  candidate_cards[] / unresolved_questions[]
- 抽卡动作（candidate_cards 填充 + validity 判定）按专家契约由接收方执行，
  需遵守：fact 卡 validity 只能 current/historical；evidence 必须为
  clean_text 字面子串
- 上一批教训已遵守：未抓 web_plus_index 站群目录；A 类主题未重复抓；
  未标 unknown；未用 seed_description 凑证据
