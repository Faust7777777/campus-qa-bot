# 安全审计记录

审计日期：2026-08-06  
审计范围：`campus-qa-bot` 源码、Docker/Compose 草案、离线资料契约、最终审核 JSONL 及交接资产。

## 结论

本地审计通过，未发现已提交的私钥、`.env`、API key 或可直接利用的 SQL/命令注入路径。线上仍保持 fail-closed：发布版校验、SQLite 只读 immutable、四路检索和模型契约任一失败都不会退化为模型常识回答。

本次审计修复了两个边界问题：

1. OneBot 接入 token 之前可以省略。现在 `ONEBOT_V11_ACCESS_TOKEN` 缺失或仍为示例值时，NoneBot 启动门拒绝接收事件；NapCat HTTP Client 必须使用同一随机 token。
2. 消息去重表之前只有时间过期，没有数量上限。现在默认最多保留4096个 message ID，超过上限淘汰最旧项，防止消息洪峰造成短期内存增长。

## 检查项

| 类别 | 结果 |
|---|---|
| 访问控制 | 仅白名单群、仅群聊；私聊、其他群、机器人自身消息忽略；`#` 不绕过证据门。 |
| OneBot 认证 | 启动强制 token；Compose 不发布宿主机端口，只加入已有 `qq-mc-bridge_default` 网络。 |
| 注入 | SQLite 查询使用参数绑定或固定 SQL；FTS token 经过转义；没有 shell/eval/动态 SQL 路径。 |
| 外部 URL | 构库和审核只接受 `dlut.edu.cn` 子域及人工审核的 `mp.weixin.qq.com`；来源域名与 URL 必须一致。 |
| 模型输入输出 | 远端响应体硬上限2MiB、Embedding 单批32卡、语义文本上限16000字符；回答允许改写但拒绝模型生成 URL，并记录待复核信号。 |
| 资源耗尽 | 并发2、队列50、答案/历史/缓存有界、去重表4096项、容器1GiB/1.5核/pids128。 |
| 数据边界 | `kb_faculty.csv` 只作隔离审计和负样本评测；构库检查 faculty 行为0，生产检索和 Embedding 不读取它。 |
| 发布完整性 | manifest、SQLite、审核输入、评测账本、模型配置和运行时代码哈希绑定；运行时 immutable 只读打开。 |
| 容器隔离 | 非 root 用户、read-only rootfs、tmpfs 无执行、drop all capabilities、no-new-privileges、无 Docker socket。 |
| 日志泄漏 | 公共错误只返回随机错误编号；API key 不写入报告、清单或异常消息。 |

## 验证命令

```powershell
python -m pytest -q                 # 154 passed
python -m compileall -q src scripts
rg -n --hidden -g '!work/**' -g '!.venv/**' -g '!.git/**' \
  "-----BEGIN|sk-[A-Za-z0-9]|AIza|AKIA[0-9A-Z]{16}" .
```

`pip-audit` 在本地环境未安装，因此依赖漏洞数据库扫描尚未执行；VPS/CI 首次构建必须根据 `uv.lock` 或镜像 SBOM 补做依赖扫描。该项是发布前检查，不构成线上降级理由。

## 交接前残余风险与门槛

- 大连理工大学网关已实测 `bge-m3` `/v1/embeddings`、`Qwen3-Reranker-8B` `/v1/rerank`、Planner/Answer Chat 端点均返回有效响应；API key 不进入交接包。网关额度、分组价格、HTTP 可达性和上游模型健康仍是运行时外部依赖，异常时必须 fail-closed，不得用零向量、随机向量或模型常识替代。
- 300题评测集已按用户确认的草案逐字节冻结；评测会生成质量报告，只有伪造链接和 faculty 泄漏等硬安全问题阻断原子发布 `current.json`。
- ARM64 镜像尚未实测；本地 Windows 延迟数据不是 VPS 承诺值。
- VPS agent 只允许登录 QQ、配置 NapCat HTTP Client 和链路冒烟，不得改动 Bridge、NapCat 上报目标、审核 JSONL、模型配置或发布指针。
- 私钥、`.env`、API key 永不进入交接包；SSH 私钥继续留在本地 WSL `~/.ssh`。
