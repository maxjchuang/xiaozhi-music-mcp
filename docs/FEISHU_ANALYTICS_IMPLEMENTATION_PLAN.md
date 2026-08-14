# 飞书行为分析代码实施计划

本计划把 [设计文档](FEISHU_ANALYTICS_DESIGN.md) 拆成可独立验证、可安全回滚的代码切片。音乐解析与播放始终是核心链路，统计模块只能降级，不能反向阻塞核心链路。

## 里程碑 A：本地可靠采集

- [x] 定义带版本的标准事件模型和全局唯一 `event_id`；
- [x] 实现 `off`、`masked`、`full` 三种文本隐私模式；
- [x] 对 Token、Cookie、手机号和邮箱进行基础脱敏；
- [x] 使用 SQLite WAL 保存事件和事务 outbox；
- [x] 实现幂等写入、批量领取、卡死恢复、重试和死信；
- [x] 统计初始化或写入失败时自动降级，不向音乐调用抛错；
- [x] 增加本地存储和隐私单元测试。

验收：重复事件只保存一次；数据库不可写时音乐工具仍返回；断网事件保留在 pending 状态。

## 里程碑 B：MCP 音乐行为接入

- [x] 为每次点歌生成贯穿搜索和媒体代理的 `trace_id`；
- [x] 记录搜索开始、成功、失败、Provider 和耗时；
- [x] 记录设备首次请求音频，避免 Range 请求重复计数；
- [ ] 接入播放停止、自然结束、换歌和音频欠载；
- [ ] 根据 `trace_id` 生成独立的播放汇总投影。

后两项需要固件通过本地遥测入口上报真实播放状态，不能用代理日志猜测。

验收：一次成功点歌至少形成 search started、search succeeded、playback started 三个同链路事件。

## 里程碑 C：飞书 CLI 登录

- [x] 使用 `lark-cli` Device Flow，不依赖 Agent、浏览器控制或回调服务器；
- [x] 使用 `auth status --verify` 检查用户身份、Token 和 Base 权限；
- [x] Token 保存与刷新完全交由飞书 CLI；
- [x] 项目不读取或保存 App Secret、Access Token 和 Refresh Token；
- [x] 增加 `auth status/login/logout` 命令；
- [x] 交互式启动引导登录，后台启动只报告 `AUTH_REQUIRED`；
- [x] 默认授权故障不阻止 MCP 启动。

验收：Access Token 过期可静默刷新；Refresh Token 失效后前台重新登录；后台启动不等待输入。

## 里程碑 D：飞书同步与初始化

- [x] 通过 `lark-cli base` 和 `lark-cli api` 调用飞书，不使用 Agent、Cookie 或页面自动化；
- [x] 使用稳定批次 `client_token` 保证批量新增幂等；
- [x] 成批提交、本地原子确认和失败退避；
- [x] 未登录时释放批次且不消耗死信重试次数；
- [x] 增加 `analytics init/status/sync/retry/test` 命令；
- [x] 初始化“原始事件”表、字段和首版仪表盘；
- [x] 使用真实 CLI 登录态完成端到端验证；
- [ ] 根据真实数据校准仪表盘组件布局和日期粒度。

验收：`analytics test` 写入一条可在飞书读取的测试记录；重复执行同步不产生重复数据。

## 里程碑 E：部署与运维

- [x] 把统计配置加入 `.env.example`；
- [x] 后台 Worker 随 MCP 启停；
- [x] `music_service.sh start` 执行授权预检；
- [ ] 部署向导增加飞书配置与首次授权步骤；
- [x] README 补充 CLI 安装、登录和验证流程；
- [ ] 完整回归测试后提交并通过 PR 合入主分支。

## 里程碑 F：固件遥测与完整用户行为

- [ ] 在 MCP 音频代理增加带本地密钥的遥测接收端点；
- [ ] 固件上报唤醒、聆听、ASR、助手回复和实际播放状态；
- [ ] 增加会话与播放投影表，并同步到飞书业务表；
- [ ] 实现按会话查询、删除和本地保留期清理；
- [ ] 增加多设备、固件版本和错误告警视图。

该里程碑需要同时修改 EchoEar 固件仓库，单独提 PR，并在真实设备上验证不会影响音频实时性。
