# 视频号服务接入

## 使用流程

1. 在 `laike-service` 创建平台为 `WECHAT_CHANNELS`、支持云端任务的授权码。
2. 在视频号 App 填写服务地址、软件代码和授权码，完成 Shizuku 授权。App 与 Shizuku 都需要允许后台运行。
3. 在后台“视频号任务”选择设备，保存采集入口、关键词、视频数量、筛选词、评论页数与资料访问人数。
4. 在“设备与下发”启动、同步配置或停止任务。保存配置只更新设置，启动时生成参数快照。
5. 在任务记录查看进度与结果，在视频号任务页面查看最近采集数据。

绑定设备时验证授权平台，WebSocket 重新连接时验证授权、软件和设备。每次下发任务使用授权码确定目标设备及平台。

## 外部接口

沿用 `X-API-Key` 认证。请求编号在调用方保持唯一；重发相同请求返回相同任务编号，修改参数或设备后需要新的请求编号。

| 接口 | 用途 |
| --- | --- |
| `POST /api/v1/external/tasks` | 创建启动、停止、同步或指定视频号操作 |
| `GET /api/v1/external/tasks/{taskId}` | 查询状态与结构化 `result` |
| `GET /api/v1/external/tasks?licenseKey=…` | 查询授权码的任务记录 |
| `GET /api/v1/external/content-events?licenseKey=…` | 按游标读取 VIDEO、COMMENT、USER |
| `GET /api/v1/external/content-events/stream` | 现有全局追加事件流 |
| `GET /api/v1/external/content-events/changes` | 现有字段更新事件流 |

使用已保存设置启动采集。

```json
{
  "licenseKey": "<授权码>",
  "requestId": "channels-collection-001",
  "action": "START",
  "callbackUrl": "https://<接收服务>/task-callback"
}
```

`callbackUrl` 可省略。停止使用 `STOP`，同步配置使用 `SYNC_CONFIG`。同步配置完成后，手机保持当前操作状态。

指定操作使用 `WECHAT_CHANNELS_TASK`。

```json
{
  "licenseKey": "<授权码>",
  "requestId": "channels-comment-001",
  "action": "WECHAT_CHANNELS_TASK",
  "taskConfig": {
    "task": "link_comment_video",
    "options": {
      "link": "https://weixin.qq.com/sph/<作品标识>",
      "text": "<评论内容>"
    }
  }
}
```

| task | options |
| --- | --- |
| `collect_channels` | `entry`、`keyword`、`count`、`videoFilter`、`commentFilter`、`commentOptions` |
| `link_like_video` | `link`，执行作品点赞及推荐 |
| `link_follow_author` | `link` |
| `link_message_author` | `link`、`text` |
| `link_comment_video` | `link`、`text` |
| `link_like_comment` | `link`、`nickname` |
| `link_reply_comment` | `link`、`nickname`、`text` |
| `link_message_commenter` | `link`、`nickname`、`text` |
| `inspect_messages` | `like`、`maxMessages`、`replyText` |

采集入口为 `recommend` 或 `search`，搜索需要 `keyword`。`count` 为 1 至 500。`commentOptions` 包含 `unlimited`、`visitProfiles`、`maxPages`、`maxReplyExpansions`、`maxProfiles`。评论和私信文字最多 500 字。

结果通过 `result.success`、`result.message` 和各操作的确认字段说明执行情况。采集结果另有 `processedCount`、`requestedCount`、`commentCount`、`profileCount` 和 `upload`。`commentCollectionsComplete` 表示是否取得全部评论，按页数限制结束时可以为 `false`。

平台限制、资料不可访问、权限不足等情况通过任务失败结果返回。手机执行过程中保留操作记录和上传确认，终止状态收到服务端确认后清除待发送回执。

## 构建与运行

服务端使用 Java 25，沿用现有数据库、Redis 和启动配置。数据库初始化增加 `remote_command.result_json`，事件复用 `content_discovery_event` 及现有变更记录。

Android CI 使用固定宿主版本，依次应用 `patches/maa-tauri-android.patch` 和 `patches/wechat-remote-host.patch`。新增 Kotlin 与 Rust 源码位于 `plugins/wechat/host/`。正式版本使用已有签名配置及 `WECHAT_CHANNELS` 更新渠道。

本地验收构建通过 Tauri 参数 `--config '{"bundle":{"android":{"debugApplicationIdSuffix":".integration"}}}'` 指定独立包标识。发布构建使用项目正常的包标识和签名。

联调证据保存在 Git 忽略的 `data/laike-service-live/`。验收状态见 [checklist.md](checklist.md)。
