# EchoEar 播放遥测协议

本文定义 `xiaozhi-music-mcp` 与 EchoEar 固件之间的播放遥测 v1 契约。产品口径见 [飞书数据分析第二阶段方案](FEISHU_ANALYTICS_PHASE2_DESIGN.md)。

## 1. 发现遥测入口

媒体清单 `manifest.json` 保持 `schema_version: 1`，新增以下可选字段：

```json
{
  "trace_id": "一次搜索链路 ID",
  "duration_ms": 30000,
  "song_duration_ms": 213000,
  "playback_access": "preview",
  "telemetry_url": "http://局域网地址:8765/media/临时令牌/telemetry"
}
```

旧固件可以忽略新增字段。`telemetry_url` 使用与媒体相同的高熵短期令牌，只允许写入该媒体的播放事件；歌曲、Provider、播放类型、时长和 `trace_id` 由 MCP 根据媒体注册信息覆盖，固件不能伪造这些字段。

## 2. 批量上报

```http
POST /media/<token>/telemetry
Content-Type: application/json
```

```json
{
  "schema_version": 1,
  "device_id": "设备原始 ID",
  "session_id": "小智会话 ID",
  "events": [
    {
      "event_id": "boot-id:sequence",
      "event_type": "playback_started",
      "playback_id": "boot-id:playback-sequence",
      "sequence": 1,
      "monotonic_ms": 123456,
      "payload": {
        "audible_played_ms": 0
      }
    }
  ]
}
```

约束：

- 单请求最多 64 个事件、请求体最多 64 KiB；
- `event_id` 全局稳定，用于重试去重；
- `playback_id` 标识一次实际播放，同一搜索结果重复播放必须使用不同 ID；
- `monotonic_ms` 用于处理乱序，播放时长不能依赖设备墙上时间；
- 设备 ID 入库前由 MCP 单向哈希；
- 服务返回 `202`，响应包含 `accepted` 和 `duplicates`。

## 3. 支持的事件

- `playback_started`
- `playback_paused`
- `playback_resumed`
- `playback_stopped`
- `playback_completed`
- `playback_failed`
- `song_switched`
- `audio_underrun`
- `decode_error`
- `network_error`
- `wake_detected`
- `listening_started`
- `listening_stopped`
- `user_utterance`
- `assistant_response`

会话事件可与当前播放事件一起批量发送，不要求 `playback_id`，但必须携带
`session_id`。由于入口来自当前媒体清单，这一版只覆盖音乐播放期间及紧邻播放的
会话；设备冷启动后第一次点歌前的全量会话统计需要后续增加独立的服务发现入口。

终止事件应携带最终汇总：

```json
{
  "audible_played_ms": 7420,
  "elapsed_since_start_ms": 8100,
  "pause_total_ms": 0,
  "underrun_count": 1,
  "underrun_total_ms": 80,
  "end_reason": "user_stopped"
}
```

固件必须在第一段 PCM 真正交给音频 codec 后产生 `playback_started`。`audible_played_ms` 按实际输出样本累计。播放期间只写入内存队列，建议在播放终止后批量发送，避免 HTTP 与音频流竞争。

## 4. 兼容与故障处理

- 遥测 URL 缺失时固件继续播放，不报错；
- `404` 表示媒体令牌过期，不重试；
- `400` 表示协议错误，记录串口诊断后停止重试；
- `503` 可以短暂退避后重试；
- 遥测失败不得停止、暂停或延迟音乐；
- MCP 将代理首次媒体请求记录为 `media_stream_requested`，只有固件事件才表示真实 `playback_started`。
