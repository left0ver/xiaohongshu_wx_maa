# 微信视频号上传与操作主任务清单

更新时间：2026-11-07

**消息中心布局修复复测**：不再要求“关注”页签；顶部“评论”配合“点赞”文字或爱心模板识别消息中心，模板覆盖选中和未选中状态。Hebe-BD00 Run `6a7544be-641a-4cfa-bf21-a0870a19f46f` 从视频页→个人页→消息→评论→返回视频页通过，任务 34.575 秒；列表为空，未验证消息点赞／回复。电脑与 Android 各 126 项测试通过，详见[复测补充](wechat-regression-20261107.md#消息中心布局后续修复)。

**简介边界最新调整**：按用户要求，上边界固定为昵称下沿，移除依据地区／认证移动起点的逻辑；下边界补充“条朋友圈内容”。本地 124 项测试通过，重放 AA丽泽原 OCR 后简介为空。尚未同步手机或重新实机测试，不新增验收勾选。

**最新修复任务复测**：本机深色菜单完整采集通过，原插队回复定位及防重通过（本轮未重复发送）；简介按第一步解析保存，但仍有地区／朋友圈计数污染；“消息”入口可进入，消息中心布局仍误判，巡检未通过。电脑与 Android 各 124 项测试通过。详见[本轮日志与截图](wechat-regression-20261107.md)，不追加全范围验收勾选。

**后续授权修复复测**：插队回复原失败例已在 Hebe-BD00 实际发送成功，Run `59ba0b43-48b2-4b9a-9cb9-92c3f33bb658`；本次新增 1 条回复，评论配额 2→3，回复账本 1→2。本次不新增采集数据，不据此补勾数据关联或上传验收。详见[后续修复记录](wechat-current-phone-review-20261107.md#后续修复插队回复原失败例复测通过)。

**当前手机复测补充（不改业务代码）**：Hebe-BD00 / Android 11 / 微信 8.0.76 独立测试库保存 6 VIDEO／8 COMMENT／2 USER，作品—评论及评论—用户孤立关联均为 0，但存在 1 条二级回复缺父评论、8/8 COMMENT 缺 accountType，以及简介、地区字段污染；服务端与 ACK 未测试。详见[当前手机兼容性复测](wechat-current-phone-review-20261107.md)，本轮不据此追加端到端勾选。

**原始需求校正与补测**：第二批以 `../wx_video(1).md` 第 23–34 行为准（相对仓库根的上级文件），不要求全量评论翻页。复用本轮真机并补做数据库副本重放：正常路径去重和跨作品隔离通过；9/9 COMMENT 缺失 `accountType`，未实现头像采集，存在地区拆分错误，公开视频号 ID 回填仅逻辑验证，本地草稿尚无重传／ACK。完整逐项结论见[第二批原清单验收](wechat-batch2-review-20261107.md)。

第二批最新复测：Hebe-BD00 / Android 11，Run `1a098e3b-12f9-4fb1-affd-3bb258d461b6`，保存 1 VIDEO / 9 COMMENT / 2 USER，评论孤立关联为 0；三屏后 `complete=false, endReason=page_limit`。按 80% 文本相似度抽检，12 字段中 11 个通过，发现“中国香港”拆分错位。第二批整体验收仍未通过，详见[本轮复测](wechat-batch2-review-20261107.md)。下方历史成功记录不代表当前版本全量覆盖。

范围：向 `content_discovery_event` 表写入视频号作品（`VIDEO`）、评论（`COMMENT`）、用户（`USER`）三类事件，建立 `作品 → 评论 → 用户` 的父子关联，并覆盖批量采集、互动触达与多运行模式。

本清单负责数据结构、事件关联、本地队列、服务端上报/ACK 可靠性及整体验收；手机端在微信（视频号）App 内的具体 UI 操作与真机行为验收见[微信视频号行为进度清单](wechat-behavior-checklist.md)；第一批单视频采集的详细实测记录见[视频号第一批采集](wechat-batch1.md)。

勾选表示该项已实现并完成所注明的端到端验证（含服务端上报/ACK 或明确约定的阶段验收）。仅完成本地 `local_only` 草稿保存但尚未接通上报队列与服务端核验的条目保持未勾选，并在下方进度中注明本地完成情况。

## 当前进度与清单分工

2026-11-12：采集任务默认启用评论者资料访问，支持 `commentOptions` 限制评论页数和资料访问数量。vivo V2131A / Android 12（Run `5ead042c-b5f7-4ba4-bf1c-c5066e1ec4e4`）与华为 Hebe-BD00 / Android 11（Run `9ba62485-ada0-4de6-bf0b-430afb1f74df`）各保存 1 VIDEO、3 COMMENT、2 USER；SQLite 完整性检查通过，COMMENT→VIDEO、USER→COMMENT 未关联记录均为 0。使用本轮真实记录在电脑及两台手机验证评论筛选后的 USER 关联与重复保存，结果通过。拟上报数据保存在 `data/collection-preview/*-search-users-upload.json`；此次验证涵盖本地数据，服务端 USER 上报仍按相关条目验收。

本清单共 **42 项**，当前完成端到端勾选 **11 项**（批量互动、主页触达、消息中心巡检、每日配额限制、多运行模式切换与有界异常恢复已在真机完成端到端闭环验证；服务端上报、ACK 与远端 SQL 联调暂缓）。手机端本地实现进展如下：

- **第一批 `VIDEO` 本地草稿已跑通并完成耗时优化与连续上滑采集（2026-11-06 至 2026-11-07）**：
  - 已在 `plugins/wechat/wechat_channels/collector/collection.py` 与 `modes.py` 实现 `VIDEO` 事件本地草稿构建与 SQLite 持久化（`video_drafts` 表，`status='local_only'`，支持写入 `source_mode` 如 `RECOMMEND`、`SEARCH`、`AUTO_FRIENDS`、`AUTO_FOLLOW`）；
  - 字段包含 `authorNickname`、`authorDouyinId`（公开视频号 ID `wechat-channel-id`，及无公开 ID 个人号的 `wxch_user_<hash>` 稳定回退 `wechat-author-fallback`）、`description`、`publishTimeText`、`publishLocation`、`authorBio`、`authorCertification`、`authorIpLocation`、`authorProfileLocation`、`missingFields`；
  - 采用 `规范化作者昵称 + 规范化完整文案` 的 SHA-256 摘要生成 `wxch-video:<hash>` 去重键与 `wechat-channels://video/<hash>` 伪链接，重复采集保留首次 `event_id`、`device_id` 和 `collected_at`；
  - `Hebe-BD00 / Android 11` 连续 3 个视频应用内流程验收通过；`vivo V2131A / Android 12` 完成空控件树缓存与本地模板优先优化后，单视频采集耗时压降至 **32.5–67.7 秒**，并完成推荐流连续多视频上滑采集与 `seen.json` 已读跳过真机验证（Run `981f51a7`、`a082d7c6`）。
- **第二批 `COMMENT` 与第三批 `USER` 手机端完整采集、回填及本地 SQLite 关联已全部跑通（2026-11-06，`vivo V2131A / Android 12`）**：
  - 已在 `collector/comments.py` 完整实现一/二级评论抽屉解析、橙色视频号蝴蝶徽章（`🦋`）掩码清理、折叠长评展开、多页滚动翻页与重叠去重、到底/空态核验（`complete=true, endReason="end_of_list"`）及「`N条回复`」二级回复展开；
  - 已实现点击评论者昵称左侧字符区访问评论者身份（兼容已开通视频号的公开/`申请关注` 私密主页提取 `douyinId`，以及普通微信用户底部资料卡标记 `identityMethod="wechat-user-card"`），支持主页折叠简介（`··展开`）展开、多人往返锚点校验（`returnVerified=true`）与异常隔离（`skippedProfiles`）；
  - 已实现 `comment_drafts` 与 `user_drafts` 表持久化（去重键分别为 `wxch-comment:<hash>` 与 `wxch-user:<hash>`），自动将评论者 `douyinId` 回填至评论的 `authorDouyinId` 与二级回复的 `replyToDouyinId`，并在 `vivo V2131A` 真机通过 `VIDEO → COMMENT → USER` 三表关联验证（1 条 `VIDEO`、17 条唯一 `COMMENT`、3 条唯一 `USER`）。
- **第三批批量操作（作品与评论互动、主页触达、个人页消息中心巡检、每日配额与恢复保护）已全部完成真机验证（2026-11-07，`vivo V2131A / Android 12`，跳过私信）**：
  - 已在 `collector/interactions.py` 与 `UiDump.java`（新增 `--set-text` 剪贴板粘贴回退）完整实现作品点赞（`like_work`）、作品红心推荐/收藏（`collect_work_action`）、发表一级评论（`post_work_comment`）、评论点赞（`like_comment`）、评论回复与三层自评防死循环保护（`reply_to_comment`）、指定作品与评论插队抢先回复（`preemptive_reply_comment`）；
  - 已实现作者/评论者主页关注（`follow_profile_user`）、主页首篇视频打开并执行点赞+推荐收藏后返回主页（`interact_profile_first_work`）、记录 `profileActionsPerformed`（`["FOLLOW", "LIKE_FIRST_WORK", "COLLECT_FIRST_WORK"]`）及返回评论锚点/推荐流校验（显式跳过私信 `dmSkipped=true`）；
  - 已实现从推荐页右上角进入 `个人页` → `视频号消息` → `评论` 列表解析通知并就地点赞/回复后返回推荐流（`inspect_and_handle_messages`），以及 `DailyQuotaStore`（`daily_quotas.json`）跨重启配额与幂等账本持久化和残留浮层恢复清理（`ensure_clean_page_on_resume`）。
- **第四批多运行模式（`RECOMMEND` / `SEARCH` / `FRIENDS` / `FOLLOW` / `CITY` / `AUTO`）与有界异常恢复已全部完成真机验证（2026-11-07，`vivo V2131A / Android 12`）**：
  - 已在 `collector/modes.py`、`phone.py`（`--mode`）与 `main.py`（`RunWechatMode`）完整实现推荐流连续多视频滑动采集与 `seen.json` 去重跳过、直播间/广告卡片识别跳过、关键词搜索与 `视频` 页签切换及排序/时长/范围筛选、双列搜索卡片解析打开采集并 1 步返回搜索结果列表（`returnedToSearchResults=true`）、顶部 `朋友` / `关注` / `同城` 页签切换与白底反差自适应高亮核验、5 阶段 `AUTO` 全自动模式切换与 `source_mode` 落库、验证码零点击暂停等待及跨页面有界返回归位（`bounded_recover_to_target`）。
- **服务端上报队列**：待接入。

---

## 第一批 作品 VIDEO

- [ ] 采集视频作者昵称 `payload.authorNickname`（保留首次识别结果，支持双行折行昵称拼接与浮层热评过滤）。*（本地草稿已实现并完成真机验证）*
- [ ] 采集完整视频文案 `payload.description`、发布时间原文 `payload.publishTimeText` 与发布地点 `payload.publishLocation`（从评论抽屉顶部作品区提取，支持展开折叠文案与多轮下滑恢复作品头部）。*（本地草稿已实现并完成真机验证）*
- [ ] 采集作者主页资料：简介 `payload.authorBio`、认证信息 `payload.authorCertification`（兼容 `认证信息` 与 `企业全称`）、IP 归属地 `payload.authorIpLocation`、资料所在地 `payload.authorProfileLocation`，未展示字段显式记入 `payload.missingFields`。*（本地草稿已实现并完成真机验证）*
- [ ] 采集作者公开视频号 ID `payload.authorDouyinId`（从作者主页 `...` → `更多信息` → `基础信息` 提取，`authorIdentityMethod="wechat-channel-id"`）；补充无公开视频号 ID 时的备用稳定标识方案。*（公开视频号 ID 本地提取与 `wechat-author-fallback` 备用标识均已实现并完成真机验证，2.75–3.96 秒内就绪）*
- [ ] 生成稳定作品去重键 `wxch-video:<hash>` 与作品伪链接 `payload.videoUrl`（`wechat-channels://video/<hash>`），本地重复采集保持幂等。*（本地 SQLite 已实现并验证）*
- [ ] 支持推荐流连续上滑采集多条视频，自动跳过直播间、推广广告与已采视频，优化单视频采集耗时。*（本地已实现并完成真机验证：单视频耗时压降至 32.5–67.7 秒，Run `981f51a7` & `a082d7c6` 连续多视频上滑与 `seen.json` 去重跳过通过）*
- [ ] 携带事件 ID（`event_id`）、授权码、设备标识（`device_id`）、来源模式（`source_mode`）和采集时间（`collected_at`）上报服务端并收到 ACK。
- [ ] 用作品查询 SQL（本地 `acceptance.sql` 及远端数据库 SQL）核对伪链接、去重键、作者昵称、视频号 ID、完整文案和时间字段。*（本地 `acceptance.sql` 已验证通过）*
- [ ] 第一批整体验收通过。

## 第二批 评论 COMMENT

- [ ] 采集评论者昵称 `payload.nickname`（过滤「作者」「首评」「作者赞过」「置顶」、评论区广告及橙色视频号蝴蝶徽章文字）。*（本地已实现并完成真机验证）*
- [ ] 采集完整评论正文 `payload.content`（支持自动点击展开折叠长评论 `...` / `··` / `展开` / `全文`）。*（本地已实现并完成真机验证）*
- [ ] 采集评论时间原文 `payload.commentTime` 及可见地区/IP 属地 `payload.ipLocation`。*（本地已实现并完成真机验证）*
- [ ] 连续滚动翻页采集一级评论，完成跨屏重叠去重、到底判定与零评论空态核验，输出准确的 `complete` 状态与 `endReason`。*（本地已实现并完成真机验证）*
- [ ] 展开评论下的「`展开 N 条回复`」/「`N条回复`」采集二级子评论，记录父评论关联（`parentCommentKey`）及回复对象（`replyToNickname` / `replyToCommentKey` / `replyToDouyinId`）。*（本地已实现并完成真机验证）*
- [ ] 填充评论者标识 `payload.authorDouyinId`，与第三批评论者资料采集回填衔接。*（本地已实现并完成真机验证）*
- [ ] 设置 `parent_event_id`，严格关联所属 `VIDEO` 事件的 `event_id`。*（本地 `comment_drafts` 已实现并完成真机验证）*
- [ ] 验证重复评论去重（`wxch-comment:<hash>`），以及不同视频下相同评论内容不会误合并。*（本地 `comment_drafts` 已实现并完成真机验证）*
- [ ] 接入评论事件队列持久化、重传与服务端 ACK 清理。
- [ ] 用评论查询 SQL 和作品—评论关联 SQL 验收。*（本地 SQLite 关联已验证通过，待远端数据库联调）*
- [ ] 第二批整体验收通过。

## 第三批 评论用户 USER

- [ ] 从评论区点击进入评论者主页或个人资料浮层，核对昵称身份对应正确。*（本地已实现并完成真机验证）*
- [ ] 提取已开通视频号评论者的公开视频号 ID `payload.douyinId`、认证、简介、地区及统计字段；对未开通视频号（仅展示普通微信资料卡）的评论者按规范记录身份类型（`identityMethod="wechat-user-card"`），不伪造视频号 ID。*（本地已实现并完成真机验证，兼容公开与 `申请关注` 私密视频号）*
- [ ] 设置 `parent_event_id`，关联所属 `COMMENT` 事件的 `event_id`。*（本地 `user_drafts` 已实现并完成真机验证）*
- [ ] 验证将评论者唯一标识回填到对应评论的 `payload.authorDouyinId`（及二级回复的 `payload.replyToDouyinId`），并记录实际触达状态 `payload.touchStatus`。*（本地已实现并完成真机验证）*
- [ ] 验证多位评论者依次访问并准确返回原评论抽屉位置继续任务（`returnVerified=true`），异常情况隔离记入 `skippedProfiles`，不串联其他用户或作品。*（本地已实现并完成真机验证）*
- [ ] 接入用户事件本地保存、重传和服务端 ACK 清理。*（本地 `user_drafts` 保存已实现，上报与 ACK 待接入）*
- [ ] 用 `COMMENT` 与 `USER` 关联查询 SQL 验收。*（本地 SQLite 关联已验证通过，待远端数据库联调）*
- [ ] 第三批整体验收通过。

## 批量操作（互动与触达）

- [x] 作品级互动：按配置执行视频点赞（拇指）、推荐（红心）或收藏、发表一级评论，并校验 UI 状态生效与防重复取消。（2026-11-07，`vivo V2131A` Run `275c6fad` & `f372139f` 真机验证通过）
- [x] 评论区触达：对目标评论执行点赞（`comment_like`）与回复（`comment_reply`，含三层自评防循环保护）。（2026-11-07，`vivo V2131A` Run `275c6fad` & `f372139f` 真机验证通过）
- [x] 插队评论回复：支持接收指定作品与目标评论任务（`preemptive_reply_comment`），定位并完成回复后恢复原采集流（`restoredToOriginalTask=true`），未命中目标时拒绝误回。（2026-11-07，`vivo V2131A` Run `df9d071c` & `3166bf33` 真机验证通过）
- [x] 主页触达：按条件执行作者/评论者主页关注、主页首篇视频点赞/收藏（跳过私信 `dmSkipped=true`），记录 `profileActionsPerformed`（`["FOLLOW", "LIKE_FIRST_WORK", "COLLECT_FIRST_WORK"]`）并校验返回原位置。（2026-11-07，`vivo V2131A` Run `f372139f` & `61716eb1` 真机验证通过）
- [x] 通知巡检与回复：进入视频号 `个人页` → `视频号消息` → `评论` 通知列表，巡检评论与回复通知，支持就地点赞与回复去重并返回推荐流（`returnedToTask=true`）。（2026-11-07，`vivo V2131A` Run `61716eb1` 真机验证通过）
- [x] 接入每日互动配额限制（点赞、收藏/推荐、评论、关注、私信独立计数，`daily_quotas.json` 跨重启持久化与幂等防重账本）。（2026-11-07，`vivo V2131A` 跨 4 轮实机运行与单元测试验证通过）

## 模式接入

- [x] 推荐模式（`RECOMMEND`）：视频号「推荐」流连续下滑选片、采集与互动。（2026-11-07，`vivo V2131A` Run `981f51a7` & `a082d7c6` 真机验证通过）
- [x] 搜索模式（`SEARCH`）：支持关键词搜索及筛选条件切换，对搜索结果视频连续采集与互动。（2026-11-07，`vivo V2131A` Run `1cf9cb35` & `6e8449b7` 真机验证通过）
- [x] 朋友 / 关注 / 同城模式（`FRIENDS` / `FOLLOW` / `CITY`）：支持顶部页签切换与定位权限弹窗处理。（2026-11-07，`vivo V2131A` Run `b4bd6e8e` & `6e8449b7` 真机验证通过）
- [x] 自动模式（`AUTO`）：支持多阶段自动切换并写入对应 `source_mode`（如 `AUTO_RECOMMEND` / `AUTO_SEARCH` / `AUTO_FRIENDS` / `AUTO_FOLLOW` / `AUTO_CITY`）。（2026-11-07，`vivo V2131A` Run `b4bd6e8e` & `6e8449b7` 5 阶段自动切换与 SQLite 落库真机验证通过）

## 上报与可靠性

实时采集上报验证（2026-11-12）：华为 Run `27eed506-a7f7-4444-838d-95e3e7f7ad2c` 分 4 次发送 1 VIDEO、3 COMMENT、2 USER，收到 6/6 ACK；vivo Run `594c9587-e266-47d5-8c5a-eea1308cd959` 分 5 次发送 1 VIDEO、4 COMMENT、2 USER，收到 7/7 ACK。视频各单独发送一次，评论和用户在资料访问完成时发送。重启后已确认事件发送数量为 0，实际认证失败的待发送记录保持完整。详细证据见[实时上报说明](wechat-upload.md)，正式 APK 和服务端 SQL 按各自范围验收。

2026-11-12：`upload_wechat_events` 已完成函数封装，授权码由 `WECHAT_LICENSE_KEY` 提供。华为 Hebe-BD00 与 vivo V2131A 的手机 Python 3.13.15 各发送 1 VIDEO、1 COMMENT，首次调用与重传均收到 2/2 ACK；校验失败和输入保持不变检查通过。当前验收范围见[上报函数说明](wechat-upload.md)，下列持久化队列和完整任务验收继续按各自范围检查。

- [ ] 作品、评论、用户事件统一持久化队列，按授权码隔离待发送记录，并与现有 `videos.sqlite3` 草稿平滑衔接。
- [ ] 实时包及心跳携带 `discoveryEvents`（每包最多 200 条），仅在收到服务端 `discoveryAckIds` 后清理已确认事件；相同去重键重传保持相同 `event_id`。
- [x] 微信安全验证/验证码检测零点击暂停保护，以及弹窗/意外页面有界返回归位防死循环。（2026-11-07，`modes.py` `wait_for_captcha_resolution` + `bounded_recover_to_target` 真机跨页归位与单元测试验证通过）
- [ ] 多机型分辨率真机验证、数据库 SQL 完整链路（`VIDEO → COMMENT → USER`）验收与正式 APK 打包验收。
