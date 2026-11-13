# 微信视频号实时上报

采集任务通过 `collector/publisher.py` 中的 `WechatPublisher` 管理待发送事件和 ACK。服务端下发的任务通过 `WechatRemoteService` 的常驻 WebSocket 上传数据，手机命令行任务通过 `collector/upload.py` 的 `upload_wechat_events` 上传。

视频资料取得并保存后，立即发送一条 `VIDEO`。每位评论者资料取得并确认返回原评论位置后，立即发送该条 `COMMENT` 和对应的 `USER`。达到资料访问数量限制或资料无法取得时，已取得的评论单独发送。三种类型保持独立事件及父子关联。

同一授权范围内，视频收到 ACK 后保持已确认状态，后续评论和用户事件直接引用它的 `eventId`。待发送内容和 ACK 复用 `videos.sqlite3` 的 `metadata` 表保存；传输失败会停止当前任务，尚未确认的事件由下次任务启动时补报。收到部分 ACK 时只确认对应事件。

每次请求的业务数据及响应分别保存在运行目录的 `uploads/*-request.json`、`uploads/*-ack.json`，文件中不包含授权码。

```python
result = upload_wechat_events(
    drafts,
    machine=android_id,
    software_code="qiaomen",
    timeout=15,
)
```

## 配置

服务端任务在 App 中填写服务地址、软件代码和授权码。App 使用 Android Keystore 保存授权信息，绑定设备并验证 `WECHAT_CHANNELS` 平台。Python 任务通过本机通信端口上传，使用 App 提供的设备身份和授权范围。任务、草稿与 ACK 保存在 `WECHAT_DATA_DIR/remote/<scope>/`，无需配置环境变量文件。设备重新连接后自动同步待上传数据。任务参数和外部调用示例见 [服务接入说明](../spec/wechat-laike/integration.md)。

以下环境变量用于手机命令行采集。

授权码通过 `WECHAT_LICENSE_KEY` 提供，绑定设备的 Android ID 通过 `WECHAT_DEVICE_ID` 提供。根目录 `.env.example` 给出变量名称，实际值保存在 Git 忽略的 `.env` 中。本地运行前，在同一个 shell 中导入：

```bash
set -a
source .env
set +a
```

手机采集入口将数据目录中的 `.env` 加载到进程环境，已设置的环境变量保持原值。App 使用 `WECHAT_DATA_DIR/.env`，手机命令行入口使用运行根目录下的 `data/.env`。文件权限应为 `600`。本地草稿的 `device_id` 是采集数据库生成的标识，上报的 `machine` 使用授权码绑定的 Android ID。

默认地址为 `wss://api.joyfile.net/huoke/ws/telemetry`，可以通过 `server_url` 参数指定其他服务地址。依赖包括 `websockets`、用于 TLS 证书验证的 `certifi`，以及读取环境变量文件的 `python-dotenv`。

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

函数将事件公共字段转换为服务端名称，身份字段使用 `authorWechatChannelsId`、`replyToWechatChannelsId`、`wechatChannelsId`、`videoId`。字段值来自采集结果。发送顺序为作品、评论、用户资料；同批父子事件的类型和来源模式必须匹配。

`WechatPublisher` 根据服务地址、软件、授权码和设备区分上报范围，并生成稳定的服务端事件 ID、去重标识及父事件映射。待发送内容在首次入队时保存，重传沿用同一份内容。已确认事件保留收据，重启后再次取得同一事件时直接跳过发送。

返回值包含：

| 字段 | 含义 |
| --- | --- |
| `discoveryAckIds` | 服务端已确认的本批事件 ID |
| `pendingEventIds` | 本批仍未确认的事件 ID |
| `uploaded` | 本批是否全部获得确认；空列表返回 `true` |

缺少授权码、无效输入、认证失败、网络失败、超时或回执格式错误会直接抛出异常。函数只返回确认结果，调用方依据 `discoveryAckIds` 更新自己的持久化记录；部分确认时保留 `pendingEventIds` 对应的数据。函数保持传入对象不变。

函数每次创建一个独立 WebSocket 连接，完成上报后关闭。服务端对同一设备保留一个连接，调用方需要协调该设备的连接使用。函数收到远程任务消息时继续等待数据回执，任务执行由宿主任务入口负责。

## 已完成验证

2026-11-12，在两台手机的 Android Python 3.13.15 中执行完整采集任务，搜索“家常菜”，读取一个视频、首屏评论和两位评论者资料，同时进行真实上报：

| 设备 | 数据 | 发送批次 | 确认结果 |
| --- | --- | --- | --- |
| 华为 Hebe-BD00 | 1 VIDEO、3 COMMENT、2 USER | 4 次 | 6/6 ACK |
| vivo V2131A | 1 VIDEO、4 COMMENT、2 USER | 5 次 | 7/7 ACK |

时间记录确认视频单独上报一次，COMMENT 与 USER 在每次资料访问完成时发送，所有请求都在评论采集函数结束前完成。两台均返回搜索结果页面，无障碍设置已恢复。使用真实采集记录重新打开上报数据库后，已确认事件的再次发送数量为 0；实际认证失败时，待发送记录在重启后保留，其他设备身份不会读取该记录。

本地证据位于 `data/streaming-upload/huawei-live/` 和 `data/streaming-upload/vivo-live/`，包括 `collection.json`、`timings.jsonl`、请求、ACK、`upload-batches.json` 和 `upload-validation.json`。该目录由 Git 忽略。运行 `python3 plugins/wechat/scripts/verify_streaming_upload.py <运行目录>` 可核对事件数量、父子关联、ACK 和分次发送顺序。

本轮通过手机 Python 入口验证，正式 APK 尚待重新打包；服务端 SQL 尚未独立核验。
