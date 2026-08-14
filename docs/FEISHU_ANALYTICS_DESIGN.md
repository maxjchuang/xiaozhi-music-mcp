# 小智使用行为分析与飞书仪表盘设计

## 1. 目标

为小智音乐 MCP 增加一套不依赖 Agent、浏览器自动化或人工抄录的使用行为分析链路，记录会话、点歌、播放结果、使用频率、操作时间和故障信息，并通过飞书多维表格的原生仪表盘查看统计结果。

这套能力必须满足两个原则：

1. 音乐播放是核心链路，飞书未登录、网络异常或接口故障都不能阻止 MCP 和音乐 Provider 启动或播放。
2. 统计数据先可靠写入本地，再异步同步到飞书；短暂离线不能导致事件丢失或重复。

## 2. 总体架构

```text
EchoEar 固件 / 小智云端 / 音乐 MCP
               │
               │ 标准化遥测事件
               ▼
       本地 SQLite 事件存储
        ├─ 事件去重与状态管理
        ├─ 会话、播放记录投影
        └─ 离线队列与失败重试
               │
               │ 后台批量同步
               ▼
           飞书 CLI 适配器
        ├─ Device Flow 登录检查
        ├─ 表结构校验
        └─ 批量新增或更新
               │
               ▼
       飞书多维表格及仪表盘
```

运行时不依赖 Codex、Agent、页面操作或 Cookie。程序通过 `lark-cli` 的 Device Flow 获取用户授权，由 CLI 持久化并刷新登录凭证，再通过结构化 Base 命令或通用 API 命令访问飞书。

## 3. 数据来源与边界

### 3.1 MCP 侧可直接采集

- 音乐搜索请求、搜索词和 Provider 尝试顺序；
- 搜索成功、失败及各 Provider 的失败原因；
- 解析出的歌曲、歌手、专辑、时长和来源；
- 媒体代理登记结果、首个媒体请求和代理错误；
- MCP 工具执行耗时和错误。

### 3.2 固件侧需要主动上报

- 唤醒成功、触摸唤醒和开始聆听；
- ASR 用户原话和助手回复；
- 播放真正开始、停止、自然结束和换歌；
- 音频欠载、解码错误、网络错误；
- 设备 ID、固件版本和运行状态。

仅凭当前 MCP 日志无法完整获得普通对话、实际播放结束原因和设备音频状态。因此固件遥测接入是完整统计的后续必要环节；第一版可以先覆盖 MCP 能观测到的音乐行为。

## 4. 标准事件模型

所有来源统一生成事件，至少包含：

| 字段 | 含义 |
| --- | --- |
| `event_id` | 全局唯一 ID，用作飞书幂等键 |
| `event_type` | 事件类型 |
| `occurred_at` | 设备或服务产生事件的时间 |
| `received_at` | MCP 接收事件的时间 |
| `device_id` | 脱敏后的设备标识 |
| `session_id` | 一轮会话标识 |
| `trace_id` | 一次点歌或工具调用链路标识 |
| `source` | firmware、mcp、provider 或 proxy |
| `payload` | 经过大小限制和脱敏的扩展 JSON |
| `schema_version` | 事件结构版本 |

首批事件类型：

- `wake_detected`
- `listening_started`
- `user_utterance`
- `assistant_response`
- `music_search_started`
- `music_search_succeeded`
- `music_search_failed`
- `playback_requested`
- `playback_started`
- `playback_stopped`
- `playback_completed`
- `playback_failed`
- `song_switched`
- `audio_underrun`
- `network_error`

事件只能追加，业务汇总由投影逻辑生成。事件结构升级时通过 `schema_version` 保持兼容。

## 5. 本地可靠存储

SQLite 是统计链路的事实来源，至少包含以下逻辑表：

- `events`：原始标准事件；
- `sessions`：按 `session_id` 聚合的会话记录；
- `playbacks`：按 `trace_id` 聚合的音乐播放记录；
- `sync_outbox`：待发送、已发送、重试和死信状态；
- `sync_state`：飞书记录 ID、游标和最近成功时间。

写入流程采用本地事务：先保存事件和 outbox，再返回业务调用。同步 Worker 独立批量消费 outbox，失败时指数退避，不占用音乐解析和音频代理线程。

同一个 `event_id` 只能写入一次；飞书记录通过幂等键新增或更新，确保进程重启和人工补传不会生成重复数据。

## 6. 飞书多维表格结构

### 6.1 会话记录

一轮对话一条记录，主要字段包括：

- 会话 ID、设备 ID；
- 开始时间、结束时间、使用时段；
- 唤醒方式、对话轮数；
- 用户原话、助手回复；
- 是否触发音乐、会话结果；
- 固件版本、MCP 版本。

### 6.2 音乐播放记录

一次点歌播放链路一条记录，主要字段包括：

- 链路 ID、会话 ID、设备 ID；
- 用户点歌原话和最终搜索词；
- 歌曲名、歌手、专辑和 Provider；
- 搜索结果、搜索耗时和开始播放耗时；
- 歌曲总时长、实际播放时长和完成率；
- 是否发生欠载、结束原因和错误信息；
- 请求时间、开始时间和结束时间。

### 6.3 原始事件

用于诊断和补算，主要字段包括：

- 事件 ID、事件类型和时间；
- 来源、设备 ID、会话 ID、链路 ID；
- 脱敏后的事件摘要和扩展数据；
- 本地同步时间、结构版本。

原始事件可以设置较短保留期，会话和播放汇总长期保留。

## 7. 仪表盘

第一版仪表盘包含：

- 今日、近 7 日和近 30 日会话次数；
- 每日活跃会话与点歌次数趋势；
- 按小时或时段统计的使用分布；
- 点歌成功率和实际开始播放率；
- 热门歌曲、歌手和搜索词 Top 10；
- Provider 使用占比和成功率；
- 平均搜索耗时、平均开始播放耗时；
- 平均播放完成率和结束原因分布；
- 网络、Provider、代理、解码和音频欠载错误趋势；
- 设备与固件版本分布。

仪表盘使用飞书原生组件，表结构和组件初始化由确定性脚本执行，不需要 Agent 参与日常运行。

## 8. 登录、授权与启动策略

### 8.1 飞书 CLI 登录状态机

启动前检查以下状态：

1. 未安装 CLI：提示安装 `lark-cli`；
2. CLI 尚未初始化：交互式执行 `lark-cli config init --new`；
3. 没有用户登录态：通过 Device Flow 完成登录，不需要 OAuth 回调地址；
4. Token 过期：由 `lark-cli auth status --verify` 验证并刷新；
5. Base 授权范围不足：执行 `lark-cli auth login --domain base` 增量授权；
6. 无权访问目标多维表格：给出明确诊断；
7. 表结构不匹配：提示执行初始化或迁移命令。

项目不读取或保存飞书 Access Token、Refresh Token、App ID 和 App Secret，凭证生命周期完全交给 `lark-cli`。项目只保存目标 Base Token、表 ID 和仪表盘 ID，这些配置也不得提交到 Git。

### 8.2 服务降级

默认配置：

```dotenv
FEISHU_ANALYTICS_ENABLED=true
FEISHU_AUTH_REQUIRED_ON_START=false
```

- 在交互式 `start` 中，未登录时主动引导授权；用户可以明确跳过，音乐服务继续启动；
- 在 LaunchAgent 等非交互式启动中，不打开浏览器，记录 `AUTH_REQUIRED` 并暂停同步；
- 飞书恢复授权或网络恢复后，Worker 自动补传本地队列；
- 只有显式设置 `FEISHU_AUTH_REQUIRED_ON_START=true` 时，授权失败才阻止 MCP 启动。

## 9. 配置与管理命令

计划扩展统一管理脚本：

```bash
bash scripts/music_service.sh auth status
bash scripts/music_service.sh auth login
bash scripts/music_service.sh auth logout
bash scripts/music_service.sh analytics init
bash scripts/music_service.sh analytics status
bash scripts/music_service.sh analytics sync
bash scripts/music_service.sh analytics retry
bash scripts/music_service.sh analytics test
bash scripts/music_service.sh start
```

- `auth status`：检查本地登录、Token 有效期、授权范围和 Base 权限；
- `auth login`：调用飞书 CLI Device Flow 完成登录；
- `auth logout`：调用飞书 CLI 清除本机用户登录态；
- `analytics init`：创建或校验数据表、字段和仪表盘；
- `analytics status`：显示本地队列、失败数和最近同步时间；
- `analytics sync`：立即同步一批记录；
- `analytics retry`：重新投递死信事件；
- `analytics test`：写入测试事件并验证飞书记录可读。

部署向导负责询问是否启用行为分析、检查并完成 CLI 登录、选择或创建 Base，并把非敏感配置写入本地环境文件。

## 10. 隐私与数据治理

默认使用脱敏模式：

```dotenv
ANALYTICS_TRANSCRIPT_MODE=masked
```

支持：

- `off`：不保存用户原话和助手回复，只记录事件及统计字段；
- `masked`：保存脱敏文本，过滤手机号、地址、Token 和 Cookie 等敏感信息；
- `full`：明确启用后保存完整 ASR 文本。

同时要求：

- 设备 ID 单向哈希；
- 不上传原始音频；
- 限制文本和错误载荷长度；
- 支持按会话 ID 查询和删除；
- 本地原始事件按配置定期清理；
- 日志不得输出 CLI 登录凭证、Base Token 或完整用户隐私字段。

## 11. 验收标准

### 核心链路

- 未配置飞书、Token 失效或飞书断网时，音乐搜索与播放仍然可用；
- 统计事件写入不显著增加 MCP 工具响应时间；
- 服务重启后未同步事件仍可继续发送。

### 授权链路

- 首次交互式启动能引导完成飞书登录；
- Access Token 能自动刷新；
- 非交互式启动不会卡在登录提示；
- 授权不足和 Base 无权限时有可操作的错误信息。

### 数据链路

- 重复同步不会在飞书产生重复事件；
- 离线期间的数据能在恢复后补传；
- 会话和播放汇总与原始事件一致；
- `analytics test` 能端到端验证本地落库、同步和读取。

### 可视化

- 仪表盘能按日期、设备、Provider 和结果筛选；
- 使用频率、热门歌曲、成功率、耗时和错误分布可正确展示；
- 空数据和新增枚举值不会导致图表初始化失败。

## 12. 分阶段交付

1. **基础设施**：事件模型、SQLite、outbox、隐私过滤和单元测试；
2. **音乐行为接入**：搜索、Provider、媒体登记等 MCP 侧事件；
3. **飞书授权与同步**：CLI 登录检查、批量同步、重试和管理命令；
4. **Base 与仪表盘初始化**：表结构、迁移、图表和端到端测试；
5. **固件遥测接入**：唤醒、ASR、实际播放状态和设备故障；
6. **运营完善**：数据保留、删除、告警、每日汇总和多设备分析。
