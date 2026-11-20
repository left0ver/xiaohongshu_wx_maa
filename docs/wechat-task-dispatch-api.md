# 微信视频号任务下发与数据采集接口文档（laike-service）

本文档描述外部业务系统 / 中台如何通过 `laike-service` 向「微信视频号助手」手机端下发自动化任务、接收状态回调以及增量拉取采集到的视频、评论与用户数据，并附带手机端与 `laike-service` 之间的内部通信协议以供维护和联调。

> **文档核对基准**：基于 `laike-service` 源码（`ExternalApiController`、`RemoteCommandService`、`WechatTaskConfig`、`WechatDiscoveryService`、`CommandCallbackService`、`TelemetryWebSocketHandler`）与 `xiaohongshu_wx_maa` 手机端源码（`WechatRemoteService.kt`、`remote_task.py`、`upload.py`）整理。

---

## 1. 接入概述

### 1.1 服务地址与环境

`laike-service` 区分测试与正式两套环境（通过 Spring Profile 隔离数据库与 Redis）：

| 环境 | Spring Profile | 默认监听端口 | 常用公网 / 网关基址示例 | 数据库 | Redis 库 |
| --- | --- | --- | --- | --- | --- |
| 测试环境 | `test`（默认） | `10096` | 按实际测试域名 / `http://127.0.0.1:10096` | `huoke` | `13` |
| 正式环境 | `prod` | `10092` | `https://api.joyfile.net/huoke` | `qiaomen_license` | `15` |

> 下文所有接口路径均相对于服务基址（`BASE_URL`），例如 `POST {BASE_URL}/api/v1/external/tasks`。

### 1.2 认证与限流

- **认证方式**：所有 `/api/v1/external/**` 接口均通过 HTTP 请求头认证：
  ```http
  X-API-Key: <服务端配置的外部 API Key>
  Content-Type: application/json; charset=utf-8
  ```
- **限流策略**：默认按来源 IP 限流 **1000 次 / 分钟**（可通过服务端流量策略覆盖）；超限时返回 HTTP `429 Too Many Requests`：
  ```json
  {
    "success": false,
    "message": "请求过于频繁，请稍后再试",
    "limit": 1000,
    "windowSeconds": 60
  }
  ```
- **认证失败响应**：
  - `503 Service Unavailable`：`{"success": false, "message": "外部任务API尚未配置"}`
  - `401 Unauthorized`：`{"success": false, "message": "API Key无效"}`

### 1.3 任务下发前置条件

对指定 `licenseKey` 下发视频号任务前，必须满足以下全部条件，否则服务端直接返回 `400 Bad Request`：

1. 授权码存在、状态为 `ACTIVE`、未过期，且所属软件（如 `qiaomen`）处于启用状态。
2. 授权码平台类型 `platformCode` 必须为 **`WECHAT_CHANNELS`**。
3. 授权码套餐必须支持云端任务（`planCode = SMART`，即智联版；启航版 `STARTER` 不支持远程任务下发）。
4. 授权码已在「微信视频号助手」手机端完成登录绑定，且设备已完成至少一次在线认证（`deviceId` 非空且与遥测记录一致）。
5. 若设备当前在线，客户端上报的能力集必须包含 `WECHAT_CHANNELS_TASK`（即正常接入的微信视频号助手客户端）。

---

## 2. 授权与设备状态前置接口

### 2.1 创建视频号授权码

- **接口**：`POST /api/v1/external/licenses`
- **用途**：开通平台为 `WECHAT_CHANNELS`、支持云端任务的授权码。

**请求体示例**：
```json
{
  "softwareCode": "qiaomen",
  "planCode": "SMART",
  "platformCode": "WECHAT_CHANNELS",
  "validityDays": 30,
  "count": 1,
  "ownerPhone": "13800138000",
  "requestId": "provision-wechat-001"
}
```

**字段说明**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `softwareCode` | string | 是 | 软件代码，如 `qiaomen`，正则 `[A-Za-z0-9_-]{1,50}` |
| `planCode` | string | 是 | `STARTER`（启航版）或 `SMART`（智联版，支持云端任务） |
| `platformCode` | string | 否 | 默认 `DOUYIN`；视频号必须传 **`WECHAT_CHANNELS`** |
| `validityDays` | int | 是 | 有效天数，`1` ~ `365` |
| `count` | int | 是 | 生成数量，`1` ~ `100` |
| `ownerPhone` | string | 否 | 归属手机号（11 位中国大陆手机号） |
| `requestId` | string | 否 | 幂等请求编号（最长 100 字符）；重复成功使用返回 `409 Conflict` |

**响应**：HTTP `201 Created`，返回创建的 `licenses` 列表（含 `licenseKey`、`platformCode`、`planCode` 等）。

---

### 2.2 查询设备绑定与在线状态

- **接口**：`POST /api/v1/external/licenses/online`
- **用途**：下发任务前检查授权码是否有效、是否绑定设备、手机客户端是否在线及权限是否就绪。

**请求体**：
```json
{
  "licenseKey": "XXXX-XXXX-XXXX-XXXX"
}
```

**响应示例（HTTP `200 OK`）**：
```json
{
  "success": true,
  "licenseKey": "XXXX-XXXX-XXXX-XXXX",
  "valid": true,
  "status": "ACTIVE",
  "softwareCode": "qiaomen",
  "platformCode": "WECHAT_CHANNELS",
  "platformName": "微信视频号",
  "planCode": "SMART",
  "planName": "智联版",
  "bound": true,
  "deviceId": "a1b2c3d4e5f67890",
  "online": true,
  "clients": [
    {
      "clientRole": "OPERATOR",
      "clientApp": "WECHAT_CHANNELS",
      "appName": "视频号助手",
      "clientVersion": "1.0.39"
    }
  ],
  "appVersions": {
    "WECHAT_CHANNELS": "1.0.39"
  },
  "screenCaptureActive": true,
  "lastReportedAt": "2026-11-20T02:15:00Z",
  "checkedAt": "2026-11-20T02:15:05Z"
}
```

> `screenCaptureActive: true` 表示手机端 App、项目资源、MaaFramework 引擎以及 Shizuku 特权控制连接全部处于就绪状态。

---

### 2.3 查询授权码详情与已保存采集配置

- **接口**：`POST /api/v1/external/license-info`
- **用途**：查询单个或批量（最多 100 个）授权码的绑定信息、最近 20 条命令以及后台保存的默认采集配置（`taskConfig`、`taskConfigVersion`）。

**请求体（单条 / 批量二选一）**：
```json
{
  "licenseKey": "XXXX-XXXX-XXXX-XXXX"
}
```
或：
```json
{
  "licenseKeys": ["KEY-1", "KEY-2"]
}
```

---

## 3. 任务下发接口（核心）

### 3.1 提交任务

- **接口**：`POST /api/v1/external/tasks`
- **成功状态码**：HTTP `202 Accepted`

#### 请求体顶层字段

| 字段 | 类型 | 必填 | 校验规则 / 说明 |
| --- | --- | --- | --- |
| `licenseKey` | string | 是 | 目标设备的授权码（自动去空格转大写，最长 64 字符）。服务端据此确定设备与平台，无需额外传平台字段 |
| `requestId` | string | 是 | 调用方全局唯一请求编号（最长 64 字符），用于幂等去重 |
| `action` | string | 是 | 视频号设备仅支持 4 种动作：`START`、`STOP`、`SYNC_CONFIG`、`WECHAT_CHANNELS_TASK` |
| `taskConfig` | object | 视 `action` 而定 | 任务执行参数对象（序列化后不得超过 **32,000 字节**） |
| `callbackUrl` | string | 否 | 回调接收地址，须为完整 `http://` 或 `https://` URL（最长 512 字符） |

#### `action` 行为说明

| `action` | 映射内部命令类型 | `taskConfig` 要求 | 行为说明 |
| --- | --- | --- | --- |
| `START` | `WECHAT_CHANNELS_TASK` | **可选** | 省略或为空对象 `{}` 时，自动读取后台“视频号任务”页面为该授权码保存的 `collect_channels` 配置快照并下发；若显式传入，则仅接受 `task = "collect_channels"` |
| `SYNC_CONFIG` | `SYNC_CONFIG` | **可选** | 省略时读取后台已保存的 `collect_channels` 配置；显式传入时仅接受 `task = "collect_channels"`。手机端将配置写入本地 `config.json`，**不中断当前执行状态** |
| `STOP` | `STOP_TASK` | **禁止携带**（须省略或为空） | 立即取消服务端该授权码所有 `PENDING` 的视频号任务（置为 `EXPIRED` 并触发回调），并向手机发送停止命令终止当前执行及手机端排队任务 |
| `WECHAT_CHANNELS_TASK` | `WECHAT_CHANNELS_TASK` | **必填** | 下发具体的视频号 9 类业务任务之一（采集、7 类指定链接互动、消息巡检） |

#### `requestId` 幂等与冲突规则

- 相同 `requestId`、相同 `licenseKey`、相同 `deviceId`、相同命令类型、相同校验后 `taskConfig` 及相同 `callbackUrl` 重发：**直接返回原任务对象**（不重复创建、不重复下发）。
- 相同 `requestId` 但修改了设备、授权码、动作、参数或 `callbackUrl`：返回 `400 Bad Request`（`"请求编号已用于其他设备或参数"`）。

#### 成功响应示例（HTTP `202 Accepted`）

```json
{
  "success": true,
  "taskId": "998df775-d9bf-45c7-9434-6664377ac495",
  "requestId": "channels-collect-20261120-001",
  "action": "WECHAT_CHANNELS_TASK",
  "status": "PENDING",
  "deviceId": "a1b2c3d4e5f67890",
  "targetClientRole": "OPERATOR",
  "online": true,
  "resultMessage": null,
  "publishPlatform": null,
  "publishedWorkUrl": null,
  "createdAt": "2026-11-20T02:20:00.123Z",
  "completedAt": null,
  "hasTaskConfig": true,
  "taskConfig": {
    "task": "collect_channels",
    "options": {
      "entry": "search",
      "keyword": "家常菜",
      "count": 10,
      "videoFilter": "",
      "commentFilter": "",
      "commentOptions": {
        "unlimited": false,
        "visitProfiles": true,
        "maxComments": 20
      }
    }
  }
}
```

> **注意**：`POST /api/v1/external/tasks` 使用 `action: "START"` 创建的任务，在服务端存储的 `commandType` 也是 `WECHAT_CHANNELS_TASK`，因此响应中的 `action` 字段会返回 `"WECHAT_CHANNELS_TASK"`。

---

### 3.2 `taskConfig` 9 类视频号业务任务完整规范

`taskConfig` 顶层只允许包含 `task` 和 `options` 两个键（传入多余字段会被严格拒绝）。

#### 任务清单总览

| `taskConfig.task` | 业务含义 | 必填 `options` | 可选 `options` |
| --- | --- | --- | --- |
| `collect_channels` | 视频号作品、评论与评论者资料采集 | 当 `entry=search` 时必填 `keyword` | `entry`、`keyword`、`count`、`videoFilter`、`commentFilter`、`commentOptions` |
| `link_like_video` | 指定作品点赞及推荐（红心） | `link` | — |
| `link_follow_author` | 关注指定作品作者 | `link` | — |
| `link_message_author` | 给指定作品作者发私信 | `link`、`text` | — |
| `link_comment_video` | 评论指定作品 | `link`、`text` | — |
| `link_like_comment` | 给指定作品中某用户的评论点赞 | `link`、`nickname` | — |
| `link_reply_comment` | 回复指定作品中某用户的评论 | `link`、`nickname`、`text` | — |
| `link_message_commenter` | 私信指定作品中的某位评论者 | `link`、`nickname`、`text` | — |
| `inspect_messages` | 视频号消息中心巡检（点赞 / 回复） | — | `like`、`maxMessages`、`replyText` |

> **所有 `options` 均启用白名单校验**：传入该任务未声明的字段会直接返回 `400 Bad Request`（`"不支持的参数：<字段名>"`）。

---

#### ① `collect_channels`（视频号采集）

**`options` 字段规范**：

| 字段 | 类型 | 必填 | 默认值 | 校验范围与说明 |
| --- | --- | --- | --- | --- |
| `entry` | string | 否 | `"recommend"` | 采集入口：仅支持 `"recommend"`（推荐流）或 `"search"`（关键词搜索） |
| `keyword` | string | 条件必填 | `""` | `entry = "search"` 时必填，非空，最长 200 字；`entry = "recommend"` 时可为空 |
| `count` | int | 否 | `10` | 目标成功保存的视频数量，范围 `1` ~ `500`（手机端连续跳过/过滤 50 条会提前结束） |
| `videoFilter` | string | 否 | `""` | 视频文案过滤词（命中任意词即采集），最长 2000 字，为空表示不过滤 |
| `commentFilter` | string | 否 | `""` | 评论内容过滤词（仅保存命中评论），最长 2000 字，为空表示不过滤 |
| `commentOptions` | object | 否 | 见下表 | 评论采集范围与资料访问设置 |

**`commentOptions` 子对象规范**：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `unlimited` | boolean | 否 | `false` | `true` 表示采集全部评论（含展开的二级回复）；`false` 时按 `maxComments` 限制单视频采集总评论数，达标即停 |
| `visitProfiles` | boolean | 否 | `true` | `true` 表示访问所有已采集评论者的个人资料并上报 `USER` 事件；`false` 表示仅采评论不进主页 |
| `maxComments` | int | 否 | `20` | 每个视频最多采集的评论总数（含展开的二级回复），范围 `1` ~ `1000` |

> **重要变更提醒**：历史字段 `maxPages`、`maxReplyExpansions`、`maxProfiles` **已彻底移除**。通过外部接口传入这些字段会直接返回 `400 Bad Request`（服务端仅在读取数据库历史保存的 `license_config` 时自动剥离旧字段）。

**请求示例（直接指定采集参数）**：
```json
{
  "licenseKey": "XXXX-XXXX-XXXX-XXXX",
  "requestId": "channels-collect-001",
  "action": "WECHAT_CHANNELS_TASK",
  "callbackUrl": "https://your-service.example.com/callbacks/wechat-task",
  "taskConfig": {
    "task": "collect_channels",
    "options": {
      "entry": "search",
      "keyword": "家常菜",
      "count": 5,
      "videoFilter": "教程,做法",
      "commentFilter": "怎么买,链接",
      "commentOptions": {
        "unlimited": false,
        "visitProfiles": true,
        "maxComments": 20
      }
    }
  }
}
```

**请求示例（使用后台已保存配置启动采集）**：
```json
{
  "licenseKey": "XXXX-XXXX-XXXX-XXXX",
  "requestId": "channels-start-001",
  "action": "START",
  "callbackUrl": "https://your-service.example.com/callbacks/wechat-task"
}
```

---

#### ② 7 类指定链接互动任务（`link_*`）

**通用字段校验规则**：

| 字段 | 类型 | 适用任务 | 校验规则 |
| --- | --- | --- | --- |
| `link` | string | 全部 7 类 `link_*`（必填） | 最长 2000 字符；必须为 `https`、无 userInfo、无显式端口，且 Host 为 `weixin.qq.com`（路径以 `/sph/` 开头）或 `channels.weixin.qq.com` |
| `nickname` | string | `link_like_comment`、`link_reply_comment`、`link_message_commenter`（必填） | 目标评论者昵称，非空，最长 200 字符；手机端会遍历评论区并处理匹配该昵称的第一条评论 |
| `text` | string | `link_comment_video`、`link_message_author`、`link_reply_comment`、`link_message_commenter`（必填） | 评论 / 回复 / 私信文本内容，非空，最长 500 字符 |

> **手机端去重与限制说明**：
> - 回复、私信、评论在手机端通过 `link_actions.json` 按目标对象永久去重（不按天清零），重复执行同一对象会直接返回已完成状态而不重复发送。
> - 私信发送后自动监测发送进度与感叹号「重发」按钮（最多重发 3 次，单次等待最长 60 秒）；若对方资料卡无私信入口、触发微信平台限制或重发 3 次仍失败，任务以 `FAILED` 终态返回并在 `resultMessage` 中说明原因。

**7 类任务请求示例**：

1. **作品点赞及推荐（`link_like_video`）**：
   ```json
   {
     "licenseKey": "XXXX-XXXX-XXXX-XXXX",
     "requestId": "wx-like-video-001",
     "action": "WECHAT_CHANNELS_TASK",
     "taskConfig": {
       "task": "link_like_video",
       "options": {
         "link": "https://weixin.qq.com/sph/AbCdEfGhI"
       }
     }
   }
   ```

2. **关注作者（`link_follow_author`）**：
   ```json
   {
     "licenseKey": "XXXX-XXXX-XXXX-XXXX",
     "requestId": "wx-follow-001",
     "action": "WECHAT_CHANNELS_TASK",
     "taskConfig": {
       "task": "link_follow_author",
       "options": {
         "link": "https://weixin.qq.com/sph/AbCdEfGhI"
       }
     }
   }
   ```

3. **私信作者（`link_message_author`）**：
   ```json
   {
     "licenseKey": "XXXX-XXXX-XXXX-XXXX",
     "requestId": "wx-dm-author-001",
     "action": "WECHAT_CHANNELS_TASK",
     "taskConfig": {
       "task": "link_message_author",
       "options": {
         "link": "https://weixin.qq.com/sph/AbCdEfGhI",
         "text": "您好，想咨询一下合作事宜"
       }
     }
   }
   ```

4. **评论作品（`link_comment_video`）**：
   ```json
   {
     "licenseKey": "XXXX-XXXX-XXXX-XXXX",
     "requestId": "wx-comment-001",
     "action": "WECHAT_CHANNELS_TASK",
     "taskConfig": {
       "task": "link_comment_video",
       "options": {
         "link": "https://weixin.qq.com/sph/AbCdEfGhI",
         "text": "讲得很详细，收藏了！"
       }
     }
   }
   ```

5. **点赞指定评论（`link_like_comment`）**：
   ```json
   {
     "licenseKey": "XXXX-XXXX-XXXX-XXXX",
     "requestId": "wx-like-comment-001",
     "action": "WECHAT_CHANNELS_TASK",
     "taskConfig": {
       "task": "link_like_comment",
       "options": {
         "link": "https://weixin.qq.com/sph/AbCdEfGhI",
         "nickname": "张三"
       }
     }
   }
   ```

6. **回复指定评论（`link_reply_comment`）**：
   ```json
   {
     "licenseKey": "XXXX-XXXX-XXXX-XXXX",
     "requestId": "wx-reply-comment-001",
     "action": "WECHAT_CHANNELS_TASK",
     "taskConfig": {
       "task": "link_reply_comment",
       "options": {
         "link": "https://weixin.qq.com/sph/AbCdEfGhI",
         "nickname": "张三",
         "text": "已私信您，请查收"
       }
     }
   }
   ```

7. **私信指定评论者（`link_message_commenter`）**：
   ```json
   {
     "licenseKey": "XXXX-XXXX-XXXX-XXXX",
     "requestId": "wx-dm-commenter-001",
     "action": "WECHAT_CHANNELS_TASK",
     "taskConfig": {
       "task": "link_message_commenter",
       "options": {
         "link": "https://weixin.qq.com/sph/AbCdEfGhI",
         "nickname": "张三",
         "text": "您好，看到您在视频下的留言"
       }
     }
   }
   ```

---

#### ③ `inspect_messages`（视频号消息巡检）

**`options` 字段规范**：

| 字段 | 类型 | 必填 | 默认值 | 校验范围与说明 |
| --- | --- | --- | --- | --- |
| `like` | boolean | 否 | `true` | 是否对巡检到的互动消息点赞 |
| `maxMessages` | int | 否 | `1` | 单次巡检最多处理的通知条数，范围 `1` ~ `10` |
| `replyText` | string | 否 | `""` | 回复文案，最长 500 字；为空字符串表示不回复 |

**请求示例**：
```json
{
  "licenseKey": "XXXX-XXXX-XXXX-XXXX",
  "requestId": "wx-inspect-msg-001",
  "action": "WECHAT_CHANNELS_TASK",
  "taskConfig": {
    "task": "inspect_messages",
    "options": {
      "like": true,
      "maxMessages": 5,
      "replyText": "感谢支持！"
    }
  }
}
```

---

#### ④ `STOP` 与 `SYNC_CONFIG` 示例

**停止任务**：
```json
{
  "licenseKey": "XXXX-XXXX-XXXX-XXXX",
  "requestId": "wx-stop-001",
  "action": "STOP"
}
```

**同步采集配置（不立即执行，不打断当前任务）**：
```json
{
  "licenseKey": "XXXX-XXXX-XXXX-XXXX",
  "requestId": "wx-sync-001",
  "action": "SYNC_CONFIG",
  "taskConfig": {
    "task": "collect_channels",
    "options": {
      "entry": "recommend",
      "count": 20,
      "videoFilter": "",
      "commentFilter": "",
      "commentOptions": {
        "unlimited": false,
        "visitProfiles": true,
        "maxComments": 30
      }
    }
  }
}
```

---

### 3.3 任务超时与过期规则

`laike-service` 后台每 60 秒扫描一次超时任务：

- `WECHAT_CHANNELS_TASK`（包含外部 `START` 与 `WECHAT_CHANNELS_TASK`）：创建时间超过 **24 小时**且仍在非终态（`PENDING`/`SENT`/`QUEUED`/`RUNNING`）并满足队列超时条件时，自动置为 `EXPIRED`（`"视频号任务超过有效等待时间"`）并触发回调。
- `STOP_TASK` / `SYNC_CONFIG`：创建时间超过 **10 分钟**且仍在 `PENDING` 或 `SENT` 状态时，自动置为 `EXPIRED`。

---

## 4. 任务状态查询接口

### 4.1 查询单个任务状态

- **接口**：`GET /api/v1/external/tasks/{taskId}`
- **响应**：HTTP `200 OK`（字段结构同 `POST /api/v1/external/tasks` 响应）；任务不存在返回 `404 Not Found`。

### 4.2 查询授权码最近任务列表

- **接口**：`GET /api/v1/external/tasks?licenseKey={licenseKey}`
- **说明**：返回该授权码最近 **50** 条顶层任务记录（按创建时间倒序）。
- **响应示例**：
  ```json
  {
    "success": true,
    "tasks": [
      {
        "success": true,
        "taskId": "998df775-d9bf-45c7-9434-6664377ac495",
        "requestId": "channels-collect-001",
        "action": "WECHAT_CHANNELS_TASK",
        "status": "SUCCESS",
        "deviceId": "a1b2c3d4e5f67890",
        "targetClientRole": "OPERATOR",
        "online": true,
        "resultMessage": "采集结束：视频 5/5，确认上传 42，待上传 0",
        "createdAt": "2026-11-20T02:20:00.123Z",
        "completedAt": "2026-11-20T02:26:18.456Z",
        "hasTaskConfig": true,
        "taskConfig": { "..." : "..." }
      }
    ]
  }
  ```

### 4.3 任务状态枚举（`status`）

| 状态 | 是否终态 | 说明 |
| --- | --- | --- |
| `PENDING` | 否 | 服务端已创建并排队，等待投递到手机 |
| `SENT` | 否 | 服务端已通过 WebSocket 下发给手机，等待手机接收确认 |
| `QUEUED` | 否 | 手机已收到命令并放入本地串行队列（`command_status` 上报） |
| `RUNNING` | 否 | 手机正在执行任务，`resultMessage` 会随步骤/上传进度动态更新 |
| `SUCCESS` | **是** | 手机上报执行成功（`command_ack: success=true`） |
| `FAILED` | **是** | 执行失败、被 `STOP` 中断、平台限制或重启中断（`command_ack: success=false`） |
| `EXPIRED` | **是** | 超时过期，或处于 `PENDING` 时被 `STOP` 取消 |
| `CANCELLED` | **是** | 任务取消（主要用于其他平台取消接口） |
| `REJECTED` | **是** | 任务被拒绝 |

---

## 5. 异步状态回调（Callback）协议

当创建任务时传入了 `callbackUrl`，服务端每次在任务状态发生变化时（如 `SENT`、`QUEUED`、`RUNNING`、`SUCCESS`、`FAILED`、`EXPIRED`），均会通过异步线程池向 `callbackUrl` 发送 HTTP `POST` 请求，并将往返记录写入数据库表 `remote_command_callback`。

- **HTTP 方法**：`POST`
- **请求头**：`Content-Type: application/json`
- **超时设置**：连接超时 10 秒，请求超时 15 秒
- **成功判定**：接收方返回 `2xx` HTTP 状态码（响应体截取前 1000 字符落库）

**回调请求体示例**：
```json
{
  "commandId": "998df775-d9bf-45c7-9434-6664377ac495",
  "externalRequestId": "channels-collect-001",
  "commandType": "WECHAT_CHANNELS_TASK",
  "status": "SUCCESS",
  "resultMessage": "采集结束：视频 5/5，确认上传 42，待上传 0",
  "publishedWorkUrl": null,
  "completedAt": "2026-11-20T02:26:18.456Z"
}
```

> **字段注意**：回调体中任务 ID 字段名为 `commandId`，请求编号字段名为 `externalRequestId`（与任务查询响应中的 `taskId` / `requestId` 对应）。

---

## 6. 采集数据查询接口（Content Events）

微信视频号采集产生的 `VIDEO`、`COMMENT`、`USER` 事件统一存储在 `laike-service` 的 `content_discovery_event` 表中，并在事件新增或字段补全时写入 `content_discovery_change` 表。数据默认保留 **30 天**（由 `CONTENT_EVENT_RETENTION_DAYS` 控制）。

### 6.1 按授权码游标分页查询（推荐业务方使用）

- **接口**：`GET /api/v1/external/content-events`

**Query 参数**：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `licenseKey` | string | 是 | — | 授权码 |
| `eventType` | string | 否 | 全部 | 视频号相关值：`VIDEO`、`COMMENT`、`USER`（不传则返回全部类型） |
| `platform` | string | 否 | `ALL` | **⚠️ 重要**：该参数校验白名单仅允许 `DOUYIN`、`XIAOHONGSHU`、`ALL`。**拉取微信视频号数据时必须省略该参数或传 `ALL`**，再根据返回项中 `payload.platform == "wechat"` 识别（传 `WECHAT` 会返回 400） |
| `cursor` | string | 否 | 空 | 上一页返回的 `nextCursor`（Base64URL 编码的 `<foundAtMs>:<id>`） |
| `limit` | int | 否 | `100` | 单页条数，范围 `1` ~ `500` |

**响应示例**：
```json
{
  "success": true,
  "items": [
    {
      "sourceId": 10042,
      "eventId": "3f91b4c2-1d65-4e0c-9a21-8b7c6d5e4f3a",
      "parentEventId": null,
      "eventType": "VIDEO",
      "sourceMode": "SEARCH",
      "deviceId": "a1b2c3d4e5f67890",
      "foundAt": "2026-11-20T02:21:10.500Z",
      "payload": {
        "platform": "wechat",
        "videoId": null,
        "videoUrl": "https://weixin.qq.com/sph/AbCdEfGhI",
        "authorNickname": "阿明厨房",
        "authorWechatChannelsId": "sphXXXXXXXXXXXX",
        "authorIdentityMethod": "wechat-channel-id",
        "authorCertification": "优质美食创作者",
        "authorBio": "每天分享一道家常菜",
        "authorIpLocation": "广东",
        "authorProfileLocation": "广东 深圳",
        "description": "今天教大家做一道下饭菜...",
        "publishTimeText": "2026年11月18日",
        "publishLocation": "深圳",
        "searchKeyword": "家常菜",
        "matchedKeyword": "家常菜",
        "missingFields": []
      }
    }
  ],
  "hasMore": true,
  "nextCursor": "MTc5NTE4NDQ3MDUwMDoxMDA0Mg"
}
```

### 6.2 全局增量追加流（供中台使用）

- **接口**：`GET /api/v1/external/content-events/stream`
- **Query 参数**：`eventType`（可选）、`platform`（默认 `ALL`，拉视频号不要传 `WECHAT`）、`cursor`（Base64URL 编码的主键 `id`）、`limit`（默认 `500`，最大 `1000`）。
- **返回差异**：每条 `item` 额外包含 `softwareCode` 和 `licenseKey`，便于中台全量同步。

### 6.3 字段更新变更流（最终一致）

- **接口**：`GET /api/v1/external/content-events/changes`
- **Query 参数**：`cursor`（Base64URL 编码的 `changeId`）、`limit`（默认 `500`，最大 `1000`）。
- **用途**：当手机端上报 `USER`（评论者资料）时，服务端在同事务中会自动把该用户的 `wechatChannelsId` 和 `eventId` 回填到父 `COMMENT` 的 `payload.authorWechatChannelsId` 与 `payload.userProfileEventId` 中，并在 `content_discovery_change` 写入一条 `COMMENT_AUTHOR` 变更记录。中台消费此接口即可实时获取补全后的最新 `COMMENT` 快照。

### 6.4 视频号三类事件 `payload` 字段与父子关联

**层级关联关系**：
- `VIDEO`：`parentEventId` 为 `null`（顶层作品）
- `COMMENT`：`parentEventId` 指向所属 `VIDEO` 的 `eventId`（入库时服务端自动从父 `VIDEO` 补齐 `videoId`、`videoUrl`、`searchKeyword`、`matchedKeyword`）
- `USER`：`parentEventId` 指向所属 `COMMENT` 的 `eventId`（入库时服务端自动回填父 `COMMENT` 的 `authorWechatChannelsId` 和 `userProfileEventId`）

**三类事件关键字段表**：

| 事件类型 | 关键 `payload` 字段 | 说明 |
| --- | --- | --- |
| `VIDEO` | `platform` (`"wechat"`)、`authorNickname`、`authorWechatChannelsId`、`authorCertification`、`authorBio`、`authorIpLocation`、`authorProfileLocation`、`description`、`publishTimeText`、`publishLocation`、`videoUrl`、`videoId`（恒为 `null`）、`searchKeyword`、`matchedKeyword`、`missingFields` | `videoUrl` 为分享链接（每次复制都会变化，不做去重依据，重复采集保留首次链接） |
| `COMMENT` | `platform` (`"wechat"`)、`commentId`（恒为 `null`）、`nickname`、`content`、`commentTime`、`ipLocation`、`commentLevel` (`1` 或 `2`)、`parentCommentKey`、`replyToNickname`、`replyToCommentKey`、`replyToWechatChannelsId`、`authorWechatChannelsId`、`userProfileEventId`、`replyCount`、`collectionComplete`、`contentMatched`、`commentMatchedKeywords`、`videoUrl`、`searchKeyword`、`matchedKeyword` | `commentLevel=1` 为一级评论，`commentLevel=2` 为二级回复，`parentCommentKey` 指向一级评论去重键（64位十六进制） |
| `USER` | `platform` (`"wechat"`)、`nickname`、`commentNickname`、`wechatChannelsId`、`identityMethod`、`accountType`、`authorBio`、`authorCertification`、`ipLocation`、`profileLocation`、`touchStatus` (`"UNTOUCHED"` 或 `"TOUCHED"`)、`profileActionsPerformed`、`returnVerified` | 仅采资料时 `touchStatus` 为 `"UNTOUCHED"`；`wechatChannelsId` 仅当评论者开通视频号并解析成功时有值 |

---

## 7. 附录：手机端与 `laike-service` 内部通信协议

供维护者联调手机端（`WechatRemoteService.kt`）与服务端（`TelemetryWebSocketHandler.java`）时参考。

### 7.1 手机端 HTTP 接口

| 接口 | 用途 | 关键请求字段 |
| --- | --- | --- |
| `POST /api/v1/licenses/verify` | 登录绑定与 WebSocket 重连前鉴权 | `softwareCode`、`licenseKey`、`machine`（Android ID）、`clientApp: "WECHAT_CHANNELS"`；校验返回 `code == 200` 且 `data.platformCode == "WECHAT_CHANNELS"` |
| `POST /api/v1/client/status` | 每 5 分钟轮询版本更新与通知 | `softwareCode`、`licenseKey`、`machine`、`clientApp: "WECHAT_CHANNELS"`、`clientVersion`、`clientVersionCode`；服务端按更新渠道 `WECHAT_CHANNELS` 返回 `release`（含 HTTPS `downloadUrl` 与 64 位 `sha256`）和 `messages` |
| `POST /api/v1/client/diagnostics` | 任务异常时自动上报诊断日志 | `softwareCode`、`licenseKey`、`machine`、`clientVersion`、`errorSummary`、`permissionStatus`、`runtimeLog` |

### 7.2 手机端常驻 WebSocket 协议（`/ws/telemetry`）

- **地址**：`wss://<host>/ws/telemetry`（Debug 包允许 `ws://`，Release 包强制 `wss://`）
- **心跳周期**：每 **15 秒**发送一次状态包（`envelope`），并重发尚未收到服务端确认的终态回执与运行中任务进度。

**① 手机 → 服务端：心跳 / 建连认证 / 采集事件上报**
```json
{
  "softwareCode": "qiaomen",
  "licenseKey": "XXXX-XXXX-XXXX-XXXX",
  "machine": "a1b2c3d4e5f67890",
  "clientApp": "WECHAT_CHANNELS",
  "clientRole": "OPERATOR",
  "appName": "视频号助手",
  "clientVersion": "1.0.39",
  "protocolVersion": 1,
  "supportsCommandLeaseHeartbeat": true,
  "capabilities": ["WECHAT_CHANNELS_TASK", "STOP_TASK", "SYNC_CONFIG"],
  "runtimeSessionId": "<uuid>",
  "currentCommandId": "",
  "taskState": "IDLE",
  "queuedCommandIds": [],
  "recoverableCommandIds": [],
  "activeCommandIds": [],
  "screenCaptureActive": true,
  "discoveryBatchId": "<uuid，仅上报数据时携带>",
  "discoveryEvents": [
    {
      "eventId": "<uuid>",
      "parentEventId": "",
      "eventType": "VIDEO",
      "sourceMode": "SEARCH",
      "dedupeKey": "<64位小写hex>",
      "contentHash": "<sha256(eventType|sourceMode|compactPayloadJson)>",
      "foundAt": 1795184470500,
      "payload": { "platform": "wechat" }
    }
  ]
}
```
> 单批 `discoveryEvents` 最多 200 条，单条 `payload` 序列化后不超过 20,000 字节；发送前按 `VIDEO` → `COMMENT` → `USER` 排序。服务端事务提交后返回：
> ```json
> {"ok": true, "clientRole": "OPERATOR", "discoveryBatchId": "<uuid>", "discoveryAckIds": ["<eventId>"], "profileVisitorAckIds": []}
> ```

**② 服务端 → 手机：任务下发**
```json
{
  "type": "command",
  "commandId": "998df775-d9bf-45c7-9434-6664377ac495",
  "command": "WECHAT_CHANNELS_TASK",
  "taskConfig": {
    "task": "collect_channels",
    "options": { "entry": "recommend", "count": 10 }
  }
}
```

**③ 手机 → 服务端：中间进度上报**
```json
{
  "type": "command_status",
  "commandId": "998df775-d9bf-45c7-9434-6664377ac495",
  "status": "RUNNING",
  "message": "已上传视频 2，评论 14，用户资料 10"
}
```

**④ 手机 → 服务端：终态回执，及服务端二次确认**
```json
// 手机上报
{"type": "command_ack", "commandId": "998df775-d9bf-45c7-9434-6664377ac495", "success": true, "message": "采集结束：视频 5/5，确认上传 42，待上传 0"}

// 服务端确认（手机收到 ok=true 后才删除本地 <commandId>.receipt.json）
{"type": "command_ack_receipt", "commandId": "998df775-d9bf-45c7-9434-6664377ac495", "ok": true}
```

---

## 8. 快速验证命令示例

### 8.1 使用 `curl` 下发与轮询

```bash
export BASE_URL="https://api.joyfile.net/huoke"
export API_KEY="<你的 X-API-Key>"
export LICENSE_KEY="<视频号智联版授权码>"

# 1. 检查设备在线状态
curl -sS -X POST "$BASE_URL/api/v1/external/licenses/online" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"licenseKey\":\"$LICENSE_KEY\"}"

# 2. 下发一个视频号搜索采集任务
curl -sS -X POST "$BASE_URL/api/v1/external/tasks" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"licenseKey\": \"$LICENSE_KEY\",
    \"requestId\": \"demo-$(date +%s)\",
    \"action\": \"WECHAT_CHANNELS_TASK\",
    \"taskConfig\": {
      \"task\": \"collect_channels\",
      \"options\": {
        \"entry\": \"search\",
        \"keyword\": \"家常菜\",
        \"count\": 2,
        \"videoFilter\": \"\",
        \"commentFilter\": \"\",
        \"commentOptions\": {
          \"unlimited\": false,
          \"visitProfiles\": true,
          \"maxComments\": 10
        }
      }
    }
  }"

# 3. 查询任务状态（将 <taskId> 替换为上一步返回值）
curl -sS -H "X-API-Key: $API_KEY" "$BASE_URL/api/v1/external/tasks/<taskId>"

# 4. 拉取该授权码采集到的内容事件（不要传 platform=WECHAT）
curl -sS -H "X-API-Key: $API_KEY" \
  "$BASE_URL/api/v1/external/content-events?licenseKey=$LICENSE_KEY&limit=50"
```

### 8.2 使用仓库内置脚本下发联调任务

仓库自带联调工具 [dispatch-integration-task.py](../plugins/wechat/tools/dispatch-integration-task.py)：

```bash
# 目录中需准备 credentials.json ({"server": "...", "apiKey": "..."}) 和 cards.json ([{"licenseKey": "..."}])
python3 plugins/wechat/tools/dispatch-integration-task.py WECHAT_CHANNELS_TASK \
  --card 0 \
  --config path/to/task.json \
  --wait 600 \
  --directory data/laike-integration
```
