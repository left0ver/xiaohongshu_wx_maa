# 微信视频号 WebSocket 上报函数

入口为 `plugins/wechat/wechat_channels/collector/upload.py` 中的 `upload_wechat_events`。

```python
result = upload_wechat_events(
    drafts,
    machine=android_id,
    software_code="qiaomen",
    timeout=15,
)
```

## 配置

授权码通过进程环境变量 `WECHAT_LICENSE_KEY` 提供。根目录 `.env.example` 给出变量名称，实际值保存在 Git 忽略的 `.env` 中。本地运行前，在同一个 shell 中导入：

```bash
set -a
source .env
set +a
```

手机宿主启动 Python 时，需要将该变量传入进程环境。`machine` 使用该授权码绑定的 Android ID；本地草稿的 `device_id` 是采集数据库生成的标识，两者分别保存各自用途。

默认地址为 `wss://api.joyfile.net/huoke/ws/telemetry`，可以通过 `server_url` 参数指定其他服务地址。依赖在插件的 `requirements.txt` 中声明，包括 `websockets` 和用于 TLS 证书验证的 `certifi`。

## 输入和结果

`drafts` 接收现有采集流程保存的事件列表或元组，每次最多 200 条。每条包含：

| 字段 | 要求 |
| --- | --- |
| `event_id` | 稳定的事件 ID，重传时保持原值 |
| `event_type` | `VIDEO`、`COMMENT` 或 `USER` |
| `parent_event_id` | 评论关联作品，用户资料关联评论；作品为空 |
| `source_mode` | `RECOMMEND`、`SEARCH`、`CITY`、`AUTO_RECOMMEND`、`AUTO_SEARCH`、`AUTO_CITY` |
| `dedupe_key` | 64 位小写十六进制摘要 |
| `collected_at` | 正整数毫秒时间戳 |
| `payload` | `platform` 为 `wechat` 的 JSON 对象，编码后最多 20000 字节 |

函数将事件公共字段转换为服务端名称，保持 `payload` 内容、事件 ID、去重标识和采集时间。发送顺序为作品、评论、用户资料；同批父子事件的类型和来源模式必须匹配。父事件可以是此前已上报的事件。

返回值包含：

| 字段 | 含义 |
| --- | --- |
| `discoveryAckIds` | 服务端已确认的本批事件 ID |
| `pendingEventIds` | 本批仍未确认的事件 ID |
| `uploaded` | 本批是否全部获得确认；空列表返回 `true` |

缺少授权码、无效输入、认证失败、网络失败、超时或回执格式错误会直接抛出异常。函数只返回确认结果，调用方依据 `discoveryAckIds` 更新自己的持久化记录；部分确认时保留 `pendingEventIds` 对应的数据。函数保持传入对象不变。

函数每次创建一个独立 WebSocket 连接，完成上报后关闭。服务端对同一设备保留一个连接，调用方需要协调该设备的连接使用。函数收到远程任务消息时继续等待数据回执，任务执行由宿主任务入口负责。

## 已完成验证

2026-11-12，在两台手机的 Android Python 3.13.15 中直接调用该函数，重放各自的真实视频号采集数据：

| 设备 | 数据 | 首次调用 | 重传调用 |
| --- | --- | --- | --- |
| 华为 Hebe-BD00 | 1 VIDEO、1 COMMENT | 2/2 ACK，889 毫秒 | 2/2 ACK，806 毫秒 |
| vivo V2131A | 1 VIDEO、1 COMMENT | 2/2 ACK，186 毫秒 | 2/2 ACK，263 毫秒 |

两台手机均通过空批次、缺少授权码、重复事件 ID、超过批次大小、平台不匹配、来源模式不受支持的检查，并确认输入对象保持不变。

本地证据为 `data/websocket-report-test/function-huawei-phone.json` 和 `data/websocket-report-test/function-vivo-phone.json`。该目录由 Git 忽略。此次验证覆盖函数、手机运行环境和线上 ACK；采集任务自动调用、持久化队列清理、USER 上报及服务端 SQL 核验仍需各自验证。
