# 模型网关实测记录

实测日期：2026-08-06  
网关：`http://aigw.dlut.edu.cn/v1`  
凭据：使用临时测试令牌，仅记录结果，不记录令牌本身。

## 模型目录

网关 `/v1/models` 返回并确认了以下运行时模型：

| 角色 | 模型 | 调用端点 | 结果 |
|---|---|---|---|
| 查询规划 | `Qwen3.5-9B` | `/v1/chat/completions` + `chat_template_kwargs.enable_thinking=false` | HTTP 200，纯 JSON |
| Embedding | `bge-m3` | `/v1/embeddings` | HTTP 200，1024维、有限、非零 |
| Reranker | `Qwen3-Reranker-8B` | `/v1/rerank` | HTTP 200，返回2条排序结果 |
| 回答 | `Qwen3.5-35B-A3B` | `/v1/chat/completions` + `chat_template_kwargs.enable_thinking=false` | HTTP 200 |

同一目录还提供 `Qwen3-Embedding-8B`、`Qwen3-VL-Embedding-8B` 和 `Qwen3-VL-Reranker-8B`，但当前发布契约固定使用上表模型，避免在构库后切换向量空间或精排分布。

## 结论与约束

- 模型详情页只展示 `chat/completions` 不能作为 Embedding 不可用的证据；实际 `/embeddings` 契约探测通过才算可用。
- Qwen3.5 在 JSON mode 下默认会先输出可见思考文本；运行时统一发送 `chat_template_kwargs: {"enable_thinking": false}`，否则严格 JSON 解析会 fail-closed。
- `bge-m3` 的向量空间固定为1024维。切换到其他 Embedding 模型必须重新构库、评测并生成新的发布证明，不能在线替换。
- 网关 API key 不得写入 `.env.example`、源码、发布目录、压缩包、日志或聊天记录；部署时通过 VPS 运行时环境注入。
- 网关额度或任一端点异常时，服务按现有 fail-closed 规则停止检索/回答，不使用随机向量、零向量或模型常识降级。
