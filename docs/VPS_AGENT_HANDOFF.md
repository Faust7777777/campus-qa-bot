# VPS Agent 交接说明

交接日期：2026-08-06  
目标：把已审核的离线资产交给 VPS 上的 agent；VPS agent 只负责 QQ/NapCat 登录接入，不能替代本地资料生产、审核、构库、评测或发布流程。

## 目标主机（已核对）

- 地址：`<VPS_HOST>`
- 用户：`opc`
- 主机：Oracle Linux Server 9.7，`aarch64`，4核
- 内存：22GiB；核对时约15GiB已用、约7.3GiB可用
- 根盘：98G，总使用约60G
- Docker：29.5.2，`opc` 通过 `sudo` 操作
- 现有 Compose 网络：`qq-mc-bridge_default`
- 现有容器：`qq-mc-bridge`、`qq-mc-napcat`、`<other-service>`、`<other-service>`、`<other-service>`、`<other-service>`
- 现有项目：`<VPS_HOME>/qq-mc-bridge`

SSH 私钥不在交接包中，也不复制到 VPS；使用本地 WSL 的 SSH 配置完成传输。

## 交接包内容

压缩包内包含：

- 完整 `campus-qa-bot` 源码、测试、Docker/Compose 草案和锁文件；
- `docs/智能体架构设计.md`、`README.md`、`CONTEXT.md`、安全审计和本说明；
- `work/luna_final_reviewed_20260806.jsonl` 及最终审核报告；
- `work/evaluation_20260807.jsonl` 及冻结记录；300题固定评测集，SHA-256 为 `184c8bebab44d6f41e62939482b9e3677803a29d987f5083795d4d540c2a8334`；原始草案和报告同时保留用于 provenance；
- Luna 各批次、Source Catalog、审核决策和可复验报告；
- 原始 `kb_clean.csv`、`kb_faculty.csv`（隔离审计用）和 `web_plus_index.csv`；
- 每个文件的 SHA-256 清单。

不包含：任何 `.env`、API key、SSH 私钥、模型网关凭据、Docker socket 或运行时 `releases/current.json`。

最终审核输入：3227 张卡，`approved=3024`、`downgraded=190`、`rejected=13`、`pending=0`、`conflict=0`。  
最终 JSONL SHA-256：`7bead5d33b2b3022f2da59088bb12cbba842985c56c6a1dbb80ef739f6e64f20`。

## VPS agent 允许做的事

1. 在不改动现有 Compose 文件的前提下，读取本交接包和校验 SHA-256。
2. 由人工完成 QQ 登录/扫码，并确认 `qq-mc-napcat` 正常运行。
3. 在 NapCat 中新增一个 HTTP Client，目标为 `http://campus-qa-bot:8080/onebot/v11/http`，与 `ONEBOT_V11_ACCESS_TOKEN` 使用同一个随机 token。
4. 如需启动 Bot，只能使用本项目 Compose，并先确认模型网关、发布版和允许群号已配置；首次新增 HTTP Client 可能短暂重启 NapCat，需提前告知。
5. 做最小链路冒烟：白名单群收到一次测试问句，确认 Bot 回复；非白名单群和私聊不应触发。

## VPS agent 禁止做的事

- 不替换、合并、重建或修改 `qq-mc-bridge` / `qq-mc-napcat`；不改现有 NapCat 上报 `http://bridge:8088/onebot`。
- 不把 `kb_faculty.csv` 复制到线上 `releases`，不把它送进 Embedding、检索或回答模型。
- 不使用随机/零向量，不跳过审核、评测、manifest 校验，不手工创建 `current.json`。
- 不允许 Bot 联网抓网页、在线写知识库或使用模型常识补答。
- 不上传私钥、`.env` 或 API key 到仓库、压缩包、日志或聊天记录。

## 解包与校验（建议）

```bash
mkdir -p <VPS_HOME>/campus-qa-handoff
tar -xzf campus-qa-handoff-*.tar.gz -C <VPS_HOME>/campus-qa-handoff
cd <VPS_HOME>/campus-qa-handoff
sha256sum -c SHA256SUMS
python3 -m compileall -q campus-qa-bot/src campus-qa-bot/scripts
```

校验失败时停止，不覆盖已有文件，不启动 Compose，并把失败文件名和摘要返回给本地维护者。

## 启动前门槛

大工网关已验证可供四个角色统一使用：`http://aigw.dlut.edu.cn/v1`，模型依次为 `Qwen3.5-9B`、`bge-m3`（1024维）、`Qwen3-Reranker-8B`、`Qwen3.5-35B-A3B`。300题文件已冻结为固定评测集；交接后默认仍只解包、不启动 `campus-qa-bot`，因为构库和完整评测尚未完成。后续必须：

1. 在运行时环境注入轮换后的大工网关 API key（不写入包、仓库或日志）和随机 OneBot token；
2. 用最终审核 JSONL 构库，运行冻结的固定评测并生成 `evaluation_report.json`；
3. 通过 `ReleaseManager` 校验后才可原子激活版本；
4. 再单独安排 NapCat HTTP Client 接入和链路冒烟。

回滚只切换经 `ReleaseManager` 验证过的旧版本指针，不直接改 SQLite，不删除旧版本。
