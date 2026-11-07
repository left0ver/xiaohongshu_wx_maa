# 微信视频号行为进度清单

更新时间：2026-11-07

**消息中心布局修复复测**：不再要求“关注”页签；顶部“评论”配合“点赞”文字或爱心模板识别消息中心，模板覆盖选中和未选中状态。Hebe-BD00 Run `6a7544be-641a-4cfa-bf21-a0870a19f46f` 从视频页→个人页→消息→评论→返回视频页通过，任务 34.575 秒；列表为空，未验证消息点赞／回复。电脑与 Android 各 126 项测试通过，详见[复测补充](wechat-regression-20261107.md#消息中心布局后续修复)。

**简介边界最新调整**：按用户要求，上边界固定为昵称下沿，移除依据地区／认证移动起点的逻辑；下边界补充“条朋友圈内容”。本地 124 项测试通过，重放 AA丽泽原 OCR 后简介为空。尚未同步手机或重新实机测试，不新增验收勾选。

**最新修复任务复测**：本机深色菜单完整采集通过，原插队回复定位及防重通过（本轮未重复发送）；简介按第一步解析保存，但仍有地区／朋友圈计数污染；“消息”入口可进入，消息中心布局仍误判，巡检未通过。电脑与 Android 各 124 项测试通过。详见[本轮日志与截图](wechat-regression-20261107.md)，不追加全范围验收勾选。

**后续授权修复复测**：插队回复新增逐候选作品恢复评论顶部、限定作品区域核对；同时修复完整省略号评论漏检和正文搜索链接误点击。Hebe-BD00 Run `59ba0b43-48b2-4b9a-9cb9-92c3f33bb658` 对原失败作品及评论实际回复成功，44.242 秒，电脑／Android 各 124 项测试通过。保留原评论面板，未验证精确滚动偏移恢复；详见[后续修复记录](wechat-current-phone-review-20261107.md#后续修复插队回复原失败例复测通过)。

**当前手机独立复测（保留原代码）**：Hebe-BD00 / Android 11 / 微信 8.0.76 已分批执行 15 轮运行，结论为部分适配，未全面通过。发现简介与地区字段污染、评论点赞生效但误报失败、消息入口名称不兼容、插队正向定位失败及深色主页更多按钮识别失败；原主题已恢复。完整分批结果、Run ID、截图与未覆盖范围见[当前手机兼容性复测](wechat-current-phone-review-20261107.md)。下方历史 35/37 不代表本机通过率；本轮不改业务代码、不追加验收勾选。

**原始需求校正**：用户原始 `wx_video(1).md` 第二批要求单页可见评论和二级回复，不要求全量翻页；本轮三屏上限不构成验收失败。当前真实缺口包括头像采集、地区拆分、COMMENT `accountType` 未回填和评论者视频号 ID 真机验证不足。补充 SQL 与隔离重放结果见[第二批原清单验收](wechat-batch2-review-20261107.md)。

第二批最新复测：Hebe-BD00 / Android 11，Run `1a098e3b-12f9-4fb1-affd-3bb258d461b6`，评论阶段 90.463 秒，读取三屏、展开一处回复并保存 9 条评论；2 位资料卡访问成功、4 位跳过，达到页数上限且明确标记未采完。文本按 80% 相似度验收，仍发现地区拆分错误。详见[本轮复测](wechat-batch2-review-20261107.md)；下方 2026-11-06 勾选属于历史记录，不能据此宣称当前版本第二批全面通过。

范围：手机端在微信（视频号）内的采集、导航、评论、互动、运行模式、耗时优化与异常恢复行为。

字段、上传队列、ACK、数据库关联及整体验收见[微信视频号上传与操作主任务清单](wechat-checklist.md)；第一批单视频采集的详细实测数据与历史轮次记录见[视频号第一批采集](wechat-batch1.md)。本清单只记录手机端 App 内行为验收，不以服务端联调作为勾选条件。

勾选表示该条行为已实现，并有覆盖所述场景的真机验收记录。单次局部跑通、代码接入或纯逻辑测试不等于完整行为验收；记录应注明日期、机型、系统版本、操作结果与已知限制。

## 当前进度概览

共 **37 项**，当前已完成真机验证 **35 项（94.6%）**：

- **视频与作者资料采集（7 / 7）**：**已全部完成真机验证**（2026-11-06 至 2026-11-07，`Hebe-BD00 / Android 11` 与 `vivo V2131A / Android 12`）。已完成单视频从微信入口导航、首次昵称、详情抽屉文案/时间/地点、作者主页简介/认证/属地、更多信息 5 秒内提取公开视频号 ID、返回视频页及本地去重保存，完成单视频耗时优化（`vivo V2131A` 单视频采集耗时从 245.985 秒压降至 **32.5–67.7 秒**，基础信息 2.75–3.96 秒内就绪），并完成推荐流连续多视频上滑切换采集与 `seen.json` 已读去重跳过验证（Run `981f51a7`、`a082d7c6`）。
- **评论采集与评论者身份（7 / 7）**：**已全部完成真机验证**（2026-11-06，`vivo V2131A / Android 12`）。支持一级评论与二级回复完整解析、橙色视频号蝴蝶徽章（`🦋`）像素级掩码过滤与「首评/作者赞过/置顶/广告」过滤、折叠长评与主页折叠简介（`··展开`）展开、多页滚动去重与到底/空态判定（`complete=true, endReason="end_of_list"`）、点击「`N条回复`」展开二级回复并关联 `parentCommentKey` / `replyToNickname` / `replyToCommentKey` / `replyToDouyinId`、点击评论者昵称左侧字符区进入视频号主页（兼容公开与 `申请关注` 私密账号）或普通微信资料卡提取身份并回填 `authorDouyinId`、多人往返锚点校验（`returnVerified=true`）及异常隔离（`skippedProfiles`）。
- **作品与评论互动（6 / 6）**：**已全部完成真机验证**（2026-11-07，`vivo V2131A / Android 12`）。已完成视频拇指点赞与防取消保护（`like_work`）、视频红心推荐/收藏与防取消保护（`collect_work_action`）、发表一级评论与剪贴板中文输入（`post_work_comment` + `ui-dump.dex --set-text`）、评论点赞与 `推荐♡` 徽章/红像素核验（`like_comment`）、评论回复与三层自评防死循环保护（`reply_to_comment`）、指定视频与评论插队抢先回复及原状态恢复（`preemptive_reply_comment`）。
- **主页触达与消息处理（5 / 6）**：**已完成除私信外的全部 5 项真机验证**（2026-11-07，`vivo V2131A / Android 12`，私信按对齐小红书策略显式跳过 `dmSkipped=true`）。支持作者/评论者视频号主页条件关注与 `已关注` 防取关保护（`follow_profile_user`）、主页首篇视频打开并执行点赞+推荐收藏后返回主页（`interact_profile_first_work`）、记录 `profileActionsPerformed`（`["FOLLOW", "LIKE_FIRST_WORK", "COLLECT_FIRST_WORK"]`）并校验返回评论锚点或推荐流、以及从推荐页右上角 `PersonalIcon` 进入 `个人页` → `视频号消息` → `评论` 列表巡检通知并就地点赞/回复后返回推荐流（`inspect_and_handle_messages`）。
- **搜索与运行模式（5 / 5）**：**已全部完成真机验证**（2026-11-07，`vivo V2131A / Android 12`）。已完成推荐模式（`RECOMMEND`）连续下滑采集与 `seen.json` 去重跳过、直播间/广告推广卡片检测跳过与退出归位、关键词搜索模式（`SEARCH`，支持切换 `视频` 页签及 `不限/最新/最热` 排序、`时长`、`范围` 筛选，双列卡片解析点击采集并 1 步返回搜索结果列表原位置 `returnedToSearchResults=true`）、顶部 `朋友` / `关注` / `同城` 页签切换与白底反差自适应高亮核验（及无 `同城` 入口安全返回 `entry_unavailable`、定位权限弹窗自动关闭）、以及多阶段全自动模式（`AUTO`）跨 `friends` → `follow` → `city` → `recommend` → `search` 切换与阶段强校验（`stageSwitchVerified=true`）。
- **配额、异常与运行恢复（5 / 6）**：已完成每日分项配额持久化（`DailyQuotaStore`）、停止保护与恢复时残留输入键盘/浮层自动收起（`check_stopped` + `ensure_clean_page_on_resume`）、结合 UI 状态与本地账本的互动幂等防重保护、安全验证/滑块零点击暂停等待与恢复核验（`wait_for_captcha_resolution`）、以及通用弹窗/意外页面有界返回归位（`bounded_recover_to_target`）。**待完成**：深浅色主题长稳验证。

## 已有真机验证记录

验证设备：`Hebe-BD00 / Android 11 / 1236×2676` 与 `vivo V2131A / Android 12 / 1080×2408`（序列号 `3413812929000TN`），日期为 2026-11-05 至 2026-11-07（第一批历史记录详见 [docs/wechat-batch1.md](wechat-batch1.md)）：

- **第一批单视频完整采集与耗时优化（2026-11-06）**：
  - `Hebe-BD00 / Android 11` 连续完成 3 个不同视频（`歪不歪Uu.`、`挽枫y.`、`酥岐`），平均总耗时 **245.985 秒**。
  - `vivo V2131A / Android 12` 完成耗时与识别链路优化后，连续两轮实测（Run `062e7776`、`49435d74`）单视频采集耗时降至 **67.7 秒 / 66.6 秒**（**提速 3.6 倍**）：通过保持 `hierarchy_unavailable=True` 消除每次点击/返回后 1.3 秒的空树 `UiDump` 超时探测，并在 `probe.py` 中优先执行 3 次快速本地模板匹配（~100ms/次）再回退窄左列（`x=0..240`）OCR，使 `基础信息` 面板在 **2.78–3.96 秒**内稳定就绪；同时兼容企业认证标签 `企业全称`、私密视频号 `申请关注`、独立定位图标行及评论抽屉重开时多轮下滑恢复作品头部（`restore_work_header` 最多 3 次）。
- **第二批评论采集与第三批评论者身份真机验证（2026-11-06，`vivo V2131A / Android 12`）**：
  - **Run `062e7776-5847-43a4-8f19-1754990d1f2c`**：翻页读取 3 屏评论（`pagesRead=3`），自动点击展开二级回复（`replyExpansions=1`），采集 13 条一/二级评论，访问 2 位视频号评论者主页并提取视频号 ID（`豪豪看吧` → `sphfLWQQcGPFd7d`、`何日菌` → `sphl159hcxMeonR`），对未命中的评论者隔离记入 `skippedProfiles` 并准确回退评论抽屉。
  - **Run `49435d74-2cf6-4960-8aa0-064d1405528e`（修复短昵称点击坐标、`申请关注` 私密主页与 `··展开` 简介展开后）**：翻页读取 2 屏评论到底（`pagesRead=2, replyExpansions=1, complete=true, endReason="end_of_list"`），**`skippedProfiles=0`（主页访问命中率 100%）**：
    - 访问评论者 `2020幸福来敲...`：提取完整视频号昵称 `2020幸福来敲门`、`douyinId="sph8A5FLNZZuWmQ"`、`ipLocation="辽宁"`、`profileLocation="辽宁沈阳"`、`authorBio="2020"`，`returnVerified=true`；
    - 访问评论者 `豪豪看吧`：自动点击展开折叠简介（`··展开`）提取完整 5 行简介、`douyinId="sphfLWQQcGPFd7d"`（本地模板 2.78 秒就绪）、`ipLocation="浙江"`、`profileLocation="浙江 杭州"`，`returnVerified=true`；
    - 自动将 `authorDouyinId` 回填至对应一级/二级评论，并将二级回复的 `replyToDouyinId` 关联到被回复评论者的视频号 ID；
    - 本地 SQLite（`videos.sqlite3`）累计持久化 **1 条 `VIDEO` 草稿、17 条唯一 `COMMENT` 草稿、3 条唯一 `USER` 草稿**，`VIDEO → COMMENT → USER` 外键关联 100% 完整。
- **第三批作品与评论互动、主页触达、个人页消息处理及配额恢复真机验证（2026-11-07，`vivo V2131A / Android 12`）**：
  - **扩展 `UiDump.java` 中文剪贴板输入**：针对微信视频号无障碍树为空（`rootChildren=0`）导致无法通过 `AccessibilityNodeInfo` 注入文本的问题，在 `UiDump.java` 实现 `IClipboard.setPrimaryClip` + `KEYCODE_PASTE (279)` 剪贴板粘贴回退，真机稳定输入中文评论与回复。
  - **Run `275c6fad-3b32-4147-a742-a71403e95fd1`（作品点赞、推荐收藏、发表一级评论、评论点赞、评论回复）**：
    - 作品拇指点赞（`workLike`）：计数 `4737 → 4738`，红像素占比 `redRatio=0.3557`，`status="performed"`；
    - 作品红心推荐/收藏（`workCollect`）：计数 `2496 → 2497`，红像素占比 `redRatio=0.3579`，`status="performed"`；
    - 发表一级评论（`workComment`）：从底部输入栏自动提取当前账号昵称 `selfNickname="AI吕哥说"`，输入并发送 `“拍得真棒，支持一下”`，核验新评论可见并写入 `work_comment` 与 `self_comment` 账本；
    - 评论点赞（`commentLike`）：点击目标评论右侧拇指图标，计数 `0 → 1` 并出现 `推荐` 标记，`status="performed"`；
    - 评论回复（`commentReply`）：自动跳过自身刚发表的评论（`AI吕哥说 我 1分钟前`），定位下一条真实用户评论（`河马`），点击评论正文拉起 `回复 河马:` 输入框，发送 `“总结得很到位”` 并核验二级回复可见。
  - **Run `f372139f-e128-4cb0-bda6-46c5dfff23b5`（恢复清理、防取消保护、自评拦截、评论者触达、作者主页关注）**：
    - 恢复清理（`ensure_clean_page_on_resume`）：启动时检测到残留评论输入键盘（`initialState="composer_overlay"`），自动按返回收起键盘回到 `finalState="comment_drawer"`（`dismissedCount=1, clean=true`）；
    - 已赞/已收藏防取消保护：再次对同一作品执行 `likeWork` 与 `collectWork`，结合红色像素（`redRatio=0.3519 / 0.3906`）与账本直接返回 `already_liked` / `already_collected`（`clicked=false`），绝不误点取消；
    - 自评防死循环拦截：显式指定 `targetComment={"nickname": "AI吕哥说"}` 执行回复时，被三层自评保护直接拦截返回 `status="self_comment_skipped", reason="matched_self_comment", clicked=false`；
    - 评论者资料卡触达与返回（`profileTouch`）：点击评论者 `河马` 查看微信资料卡后准确返回原评论抽屉位置（`returnedToCommentPosition=true`）；
    - 作者主页关注（`follow_profile_user`）：进入作者 `喜剧之王单口季` 视频号主页点击 `关注`，核验状态变为 `已关注`（`status="performed"`）。
  - **Run `61716eb1-af2d-461b-b9e2-80dae364cfd8`（主页首作点赞/收藏触达 + 个人页视频号消息中心通知点赞与回复）**：
    - 作者主页完整触达（`authorProfileTouch`）：进入 `喜剧之王单口季` 主页识别 `已关注` 跳过重复点击（`status="already_followed", clicked=false`），自动点开主页首篇视频（`恋综嘉宾单身理由全是假的？`），完成拇指点赞（`redRatio=0.3557`）与红心推荐/收藏（`redRatio=0.3579`），返回主页（`returnedToProfile=true`）并退回推荐流（`returnedToFeed=true`），记录 `profileActionsPerformed=["FOLLOW", "LIKE_FIRST_WORK", "COLLECT_FIRST_WORK"]`、`touchStatus="TOUCHED"`、`dmSkipped=true`；
    - 个人页消息中心巡检与就地处理（`inspect_and_handle_messages`）：从推荐流右上角 `PersonalIcon`（`[640, 65, 56, 56]`）依次进入 `个人页` → `视频号消息` → `评论` 列表，解析出 4 条评论通知（`messagesFound=4`），对首条通知（`狼王5173`）执行就地拇指点赞（`likeStatus="performed", liked=true`）并点击 `回复` 发送 `“收到啦，感谢互动”`（`replyStatus="performed", replied=true`），写入 `message_handled` 账本去重后连按 2 次返回准确回到视频号推荐流（`returnedToTask=true, finalState="channels_feed"`）。
  - **Run `df9d071c-c96e-43fb-8961-02b8848e81dd` & `3166bf33-ed3a-4ba1-8e17-3a3d179d4d28`（指定作品与评论插队抢先回复负向/正向验证）**：
    - 负向防误回验证（Run `df9d071c`）：当目标评论不在连续 3 屏范围内时，拒绝误回其他评论，返回 `status="target_not_found", replied=false` 并自动关闭评论抽屉恢复原推荐流（`restoredToOriginalTask=true`）；
    - 正向定位回复验证（Run `3166bf33`）：定位作品 `喜剧之王单口季` 与目标评论 `于文贵`（`“聪明绝顶，但一定要劳逸结合。”`），自动完成回复 `“确实要注意劳逸结合”`（`status="performed", replied=true`）并关闭抽屉恢复原推荐流（`restoredToOriginalTask=true`）。
- **第四批搜索与多运行模式、连续多视频采集及有界恢复真机验证（2026-11-07，`vivo V2131A / Android 12`）**：
  - **Run `981f51a7-b80d-4101-b281-bb6a720b9928` & `a082d7c6-9417-43e3-8286-c84449015b6f`（`RECOMMEND` 推荐流连续多视频滑动采集与 `seen.json` 自动跳过）**：
    - Run `981f51a7`（`count=2, maxSwipes=6`）：校验顶部 `推荐` 标签激活态（`underlineRatio=0.2143, textRatio=0.3267, score=0.7553`），连续完整采集 2 条视频（第 1 条 `喜剧之王单口季` / `sphBjtnZbdkINJ6`，耗时 50.8s；自动上滑 1 次切换第 2 条 `月井羊-瘦到140斤找对象` / `sphpOtMUI8gNNC3`，耗时 32.5s），写入 `videos.sqlite3`（`source_mode="RECOMMEND"`）并同步 `seen.json`；
    - Run `a082d7c6`（已见视频去重跳过验证）：面对当前停留的已采视频，首屏即命中 `seen.json` 记录 `skippedItems=[{"swipeIndex": 0, "kind": "already_seen"}]`，自动上滑切换至新视频 `脱口秀和Ta的朋友们` 完成处理（总耗时 13.6s）。
  - **Run `1cf9cb35-d752-45c8-8861-7bc4f115fabd`（`SEARCH` 搜索模式：关键词搜索、排序/时长筛选、双列卡片打开采集与返回原列表）**：
    - 从视频号顶栏搜索入口（`[556, 65, 56, 56]`）进入搜索页，通过 `ui-dump.dex --set-text` 输入关键词 `“脱口秀”` 并点击右上角绿色 `搜索`；
    - 自动切换至 `视频` 标签页（过滤公众号/视频号账号卡），在内联筛选栏切换 `最热` 排序（深色激活文字占比 `sortDarkRatio=0.2616`）并点开 `时长` 底部弹窗选择 `5分钟以下`（`visibleCardCount=4`）；
    - 解析双列视频卡片（`parse_search_video_cards`），点击打开首张未见卡片（`综艺大表姐`：《终于有人吐槽鸡兔同笼了#脱口秀和ta的朋友们第二季》，时长 `00:24`，点赞 `10万+`），完成视频详情与主页采集落库（`eventId="75eebb75-8282-42d5-88a4-cfb6bd9672a8"`, `source_mode="SEARCH"`），随后 1 步返回搜索结果双列列表原位置（`returnedToSearchResults=true`）。
  - **Run `b4bd6e8e-e44b-4f1f-ac54-0038f3520ee9` & `6e8449b7-93ec-4830-a53d-3d31ecd0a80a`（`FRIENDS` / `FOLLOW` / `CITY` 顶栏切换、`AUTO` 5 阶段全自动切换与跨页面有界恢复）**：
    - 在 Run `b4bd6e8e` 中从残留 `basic_info_overlay` / 搜索页自动执行 `bounded_recover_to_target` 归位，依次切换 `朋友♡`（`AUTO_FRIENDS`）完整采集 `山山6342`（`sphZgn9tsRB6QzS`, `eventId="c62146dd-151d-4606-a71d-ff6dd8fa1ce4"`）与 `关注`（`AUTO_FOLLOW`）完整采集 `黄西脱口秀`（`sphElyY3fiqavo0`, `eventId="5e58d490-f27b-48f4-9949-40624d29ee37"`）并持久化至 `videos.sqlite3`；
    - 修复 0 评论视频右下角底栏 `评论` 图标（`[636, 1531, 47, 27]`）与评论抽屉标题的坐标区分、以及亮白视频画面遮挡顶栏文字时的标准顶栏坐标补全后，Run `6e8449b7` 一次性跑通 **5 阶段 `AUTO` 全自动模式**（`stagesCompleted=5`，总耗时 130.7s）：
      1. `friends`（`AUTO_FRIENDS`）：切换 `朋友` 页签（`underlineRatio=0.2143, textRatio=0.2746, stageSwitchVerified=true`），自动跳过 1 条已见视频后处理 `幽默的小伍狼`；
      2. `follow`（`AUTO_FOLLOW`）：切换 `关注` 页签（`underlineRatio=0.2143, textRatio=0.2243, stageSwitchVerified=true`），处理 `吕哥说`；
      3. `city`（`AUTO_CITY`）：检测微信 8.0.x 顶栏无 `同城` 入口，零盲点安全返回 `status="entry_unavailable", reason="city_tab_missing"`；
      4. `recommend`（`AUTO_RECOMMEND`）：切换回 `推荐` 页签（`underlineRatio=0.2143, textRatio=0.2457, stageSwitchVerified=true`），处理 `三脚猪喜剧`；
      5. `search`（`AUTO_SEARCH`）：自动进入搜索输入 `“美食”`，切换 `视频` 页签并应用 `最新` + `5分钟以下` 筛选（`sortDarkRatio=0.2676, stageSwitchVerified=true`），打开 `川和盛铁板烧`（`4分钟前`，0 赞 `喜欢` 图标）处理后准确返回搜索结果页（`returnedToSearchResults=true`）。
  - **SQLite 验收（`acceptance.sql`）与单元测试全量通过**：设备 `/data/local/tmp/wechat-channels-dev/data/videos.sqlite3` 累计持久化 **6 条唯一 `VIDEO` 草稿**（覆盖 `RECOMMEND`、`SEARCH`、`AUTO_FRIENDS`、`AUTO_FOLLOW` 四类 `source_mode`），`acceptance.sql` 重复键与异常记录校验均为 `0` 行；主机端（2.23s）与手机端（`run-phone.sh --test`，6.68s）共 **119 项单元测试** 100% 通过。

---

## 视频与作者资料采集（第一批）

- [x] 启动微信并从底部 `发现` → `视频号` 进入 `推荐` 页，校验顶部页签状态。（2026-11-06，`Hebe-BD00` 与 `vivo V2131A` 实测通过，支持 `<关注` 合并页签识别与残留主页/抽屉自愈归位）
- [x] 识别当前视频作者昵称，点开评论抽屉读取完整视频文案、发布时间原文（`publishTimeText`）和发布地点（`publishLocation`），支持折叠文案展开（最多 2 次）及作品头部滚出时抽屉内下滑恢复。（2026-11-06，支持多轮下滑恢复作品头部、独立定位行与同行原声展开边界）
- [x] 从视频页进入作者主页，分离头像旁认证/地域标签与全宽简介正文，支持折叠简介展开并过滤彩色装饰符号，提取 `authorBio`、`authorCertification`、`authorIpLocation`、`authorProfileLocation`。（2026-11-06，支持 `企业全称` 认证、`申请关注` 私密账号与 `··展开` 折叠简介展开）
- [x] 点击作者主页右上角 `...` → `更多信息`，在 5 秒共享就绪预算内识别「基础信息 + 视频号 ID」面板并提取有效公开视频号 ID（`authorDouyinId`）。（2026-11-06，本地模板优先 + 窄左列 OCR 回退，点击起 2.78–3.96 秒就绪并通过格式校验）
- [x] 从基础信息与作者主页返回视频页，按 `规范化作者昵称 + 规范化完整文案` 生成 `wxch-video:<hash>` 去重键与 `wechat-channels://video/<hash>` 伪链接，写入本地 SQLite 草稿。（2026-11-06，多轮真机实测与副本重放幂等验证通过）
- [x] 优化单视频识别链路与耗时：消除同页面空控件树反复超时探测、精简导航与主页阶段的多尺度模板/OCR 跨进程调用，将单视频完整采集耗时从 ~246 秒压降至合理范围，提升昵称与正文边界识别准确率。（2026-11-06，`vivo V2131A / Android 12` 实测单视频采集耗时降至 **32.5–67.7 秒**，提速 3.6–7.5 倍）
- [x] 支持推荐流连续多视频滑动采集：单条视频采集并归位后自动上滑切换下一条视频，基于本地已读集合跳过重复视频，连续采集多条不串作品。（2026-11-07，`vivo V2131A` Run `981f51a7` 连续采集 2 条视频及 Run `a082d7c6` 自动跳过 `seen.json` 已见视频实测通过）

## 评论采集与评论者身份（第二批 & 第三批）

- [x] 打开视频半屏评论抽屉，识别抽屉顶部 `评论 N` 标题、作品头部边界与评论列表区，准确读取可见一级评论的昵称、正文、时间与地区，过滤「首评」「作者赞过」「置顶」标签、评论区广告卡片及昵称旁橙色视频号蝴蝶徽章（`🦋`）。（2026-11-06，`vivo V2131A` 实测通过，通过评论快照橙色像素掩码消除蝴蝶图标误识别为 `W`）
- [x] 自动识别并点击展开折叠长评论（`...` / `··` / `展开` / `全文`），读取完整评论正文，避免将截断评论丢弃或误存。（2026-11-06，`comments.py` 实现并随 `vivo V2131A` 评论抽屉链路验证通过）
- [x] 连续滚动翻页采集一级评论，基于帧稳定与跨页重叠评论去重，准确判定评论到底或零评论空状态（`暂无评论` / `还没有评论`），区分完整采集（`complete=true`）与达到轮数上限（`complete=false`）。（2026-11-06，`vivo V2131A` 多页翻页去重与 `complete=true, endReason="end_of_list"` 实测通过）
- [x] 点击展开一级评论下的「`展开 N 条回复`」/「`N条回复`」，读取二级回复列表，保留所属一级评论关联（`parentCommentKey`）并提取无歧义的回复对象显示名（`replyToNickname` / `replyToCommentKey` / `replyToDouyinId`）。（2026-11-06，`vivo V2131A` 展开「4条回复」并关联父评论与被回复人视频号 ID 实测通过）
- [x] 从目标评论点击进入评论者主页或个人资料浮层，核对昵称身份；若对方开通视频号（含公开主页与 `申请关注` 私密主页）则提取视频号 ID（回填 `authorDouyinId` 与 `replyToDouyinId`）、认证与简介/地区信息，若仅展示普通微信资料卡（无视频号 ID）则明确标记 `identityMethod="wechat-user-card"` 且不猜测 ID。（2026-11-06，通过点击昵称最左侧 32px 字符区解决短昵称误触回复键盘问题，`vivo V2131A` 实测通过）
- [x] 逐个访问多位评论者，每次核对回到原评论抽屉同一锚点位置后继续，不串用户或作品；评论阶段结束后正确关闭抽屉返回原视频。（2026-11-06，`vivo V2131A` 连续访问多位评论者 `returnVerified=true` 且 `skippedProfiles=0` 实测通过）
- [x] 处理评论消失、用户资料卡/主页不可访问、身份无法确认等异常情况，单独记入 `skippedProfiles` 并回退原评论位置继续，不记为采集成功也不错误关联其他用户。（2026-11-06，`vivo V2131A` Run `062e7776` 实测异常隔离与回退恢复通过）

## 作品与评论互动

- [x] 视频点赞与防取消保护：定位底部操作栏拇指图标点击一次并在原位置验证红色已赞像素（`redRatio >= 0.012`）与计数递增；已点赞或账本已记录则直接返回 `already_liked`（`clicked=false`），绝不误点取消。（2026-11-07，`vivo V2131A` Run `275c6fad` & `f372139f` 实测通过，`4737 → 4738`，二次调用 `already_liked`）
- [x] 按配置执行视频推荐（红心/「赞/推荐/收藏」）操作，识别已有红色状态与本地账本并确认点击后状态生效（`redRatio=0.3579`，计数 `2496 → 2497`），避免重复点击取消（二次调用返回 `already_collected, clicked=false`）。（2026-11-07，`vivo V2131A` Run `275c6fad` & `f372139f` 实测通过）
- [x] 在目标视频发表一级评论，通过 `ui-dump.dex --set-text` 剪贴板粘贴输入评论文本，核对发送按钮与发送后抽屉内可见自评结果，同时写入 `work_comment` 与 `self_comment` 账本防止重复评论。（2026-11-07，`vivo V2131A` Run `275c6fad` 发送 `“拍得真棒，支持一下”` 实测通过）
- [x] 对目标评论执行点赞，确认评论对象与右侧点赞图标红像素、`推荐♡` 徽章或计数递增生效，已赞则跳过返回 `already_liked`。（2026-11-07，`vivo V2131A` Run `275c6fad` 实测通过）
- [x] 回复目标评论，核对输入框 `回复 xxx:` 提示、输入内容与发送后可见二级回复；建立三层自评识别与幂等保护（地区槽位 `我` / `selfNickname` 匹配、`self_comment` 账本、点击后是否弹出 `回复` 输入框），避免在自己发表的评论下循环回评。（2026-11-07，`vivo V2131A` Run `275c6fad` 回复 `河马` 及 Run `f372139f` 拦截自评 `AI吕哥说` 实测通过）
- [x] 接收指定视频与目标评论任务后定位作品与评论并插队回复（`preemptive_reply_comment`），完成后自动恢复原任务页面（`restoredToOriginalTask=true`）；无法唯一定位目标作品或评论时安全拒绝（`target_work_not_found` / `target_not_found`），绝不误回复其他评论。（2026-11-07，`vivo V2131A` Run `df9d071c` 负向防误回与 Run `3166bf33` 正向回复 `于文贵` 实测通过）

## 主页触达与消息处理

- [x] 在作者或已开通视频号的评论者主页按条件执行关注（`follow_profile_user`），识别 `关注` / `已关注` 状态并确认点击后变为 `已关注`，已关注则直接返回 `already_followed`（`clicked=false`）避免误取关。（2026-11-07，`vivo V2131A` Run `f372139f` & `61716eb1` 实测通过）
- [x] 打开目标用户主页首篇可识别视频作品（`interact_profile_first_work`），按配置执行拇指点赞与红心推荐/收藏后返回主页（`returnedToProfile=true`）；空主页或无可见视频卡片时安全返回 `no_work_found`。（2026-11-07，`vivo V2131A` Run `61716eb1` 打开 `喜剧之王单口季` 首作完成点赞+收藏并返回主页实测通过）
- [ ] 进入目标作者私信会话（点击主页 `私信`），核对收件人、发送内容与结果，识别客服接口或私信受限状态。（对齐小红书第三批策略，当前显式跳过私信 `dmSkipped=true`）
- [x] 记录实际完成的主页触达动作（`profileActionsPerformed=["FOLLOW", "LIKE_FIRST_WORK", "COLLECT_FIRST_WORK"]`、`touchStatus="TOUCHED"`），核对返回原视频（`returnedToFeed=true`）或原评论抽屉锚点位置（`returnedToCommentPosition=true`）后继续任务。（2026-11-07，`vivo V2131A` Run `f372139f` & `61716eb1` 实测通过）
- [x] 从视频号推荐页右上角 `PersonalIcon` 进入 `个人页` → `视频号消息` → `评论` 标签页（`open_personal_messages`），准确解析未读/历史评论与回复通知列表（提取评论者昵称、蝴蝶徽章、时间、正文、引用行及 `回复`/`凸` 按钮坐标）。（2026-11-07，`vivo V2131A` Run `61716eb1` 解析 4 条通知实测通过）
- [x] 对可回复消息就地点赞（验证红拇指状态）并点击 `回复` 就地发送回复（`handle_single_message`），写入 `message_handled` 账本去重；处理结束后有界返回原视频号推荐流页面（`returnedToTask=true, finalState="channels_feed"`）。（2026-11-07，`vivo V2131A` Run `61716eb1` 就地点赞并回复 `狼王5173` 后 2 步返回推荐流实测通过）

## 搜索与运行模式

- [x] 推荐模式（`RECOMMEND`）：在视频号「推荐」流连续采集与互动，上滑切换并跳过已处理视频。（2026-11-07，`vivo V2131A` Run `981f51a7` & `a082d7c6` 实测通过）
- [x] 识别并跳过视频流中的「直播中 / 点击进入直播间」卡片、广告推广卡片及非视频内页，若误入直播间或外链能够自动退出并归位推荐流。（2026-11-07，`modes.py` `detect_feed_skip_item` + `bounded_recover_to_target` 实现并通过真机多模式与单元测试验证）
- [x] 搜索模式（`SEARCH`）：从视频号搜索入口输入关键词搜索，支持切换筛选条件（综合/最新/最热、发布时间/时长/范围等），从搜索结果进入视频连续处理并核对返回结果列表原位置。（2026-11-07，`vivo V2131A` Run `1cf9cb35` 搜索“脱口秀”+最热+5分钟以下采集返回及 Run `6e8449b7` 搜索“美食”+最新+5分钟以下实测通过，`returnedToSearchResults=true`）
- [x] 朋友 / 关注 / 同城流模式：支持切换顶部「朋友」「关注」或同城入口连续处理视频，自动处理定位权限未开启弹窗（点击 `暂时不了` / `取消`）。（2026-11-07，`vivo V2131A` Run `b4bd6e8e` & `6e8449b7` 实测 `朋友` / `关注` 切换采集与 `同城` 缺失安全返回 `entry_unavailable` 通过）
- [x] 自动模式（`AUTO`）：按配置在推荐、搜索等阶段间自动切换，每次切换后强校验当前页面与目标阶段一致。（2026-11-07，`vivo V2131A` Run `6e8449b7` 5 阶段 `friends` → `follow` → `city` → `recommend` → `search` 自动切换与 `stageSwitchVerified=true` 实测通过）

## 配额、异常与运行恢复

- [x] 分别限制每日点赞、推荐/收藏、评论、关注、私信次数（`DailyQuotaStore`），达到上限后抛出 `QuotaExceededError` 阻断对应写操作，持久化至 `daily_quotas.json` 跨重启不重置当日已用配额，跨日自动清零计数并保留 `self_comment` 防循环记录。（2026-11-07，`vivo V2131A` 跨多轮实机运行持久化验证与单元测试通过）
- [x] 检测微信安全验证、滑块或验证码页面后立即零点击暂停，等待人工完成验证后核对干净页面再继续执行。（2026-11-07，`modes.py` `wait_for_captcha_resolution` + `call_with_captcha_pause` 实现并通过单元测试与全模式接入验证）
- [x] 对加载失败、微信/视频号弹窗（青少年模式、位置权限、新版本提示、意外弹出的分享/更多菜单）或意外页面执行有限次数的返回归位（`bounded_recover_to_target`），超限无法恢复时安全停止并记录原因。（2026-11-07，`vivo V2131A` Run `b4bd6e8e`、`ebfc947f`、`6e8449b7` 跨搜索页/发现页/基础信息页有界归位与单元测试实测通过）
- [x] 用户停止任务后立即停止后续点击与输入（`check_stopped`）；恢复运行时自动收起残留输入键盘、基础信息浮层、更多菜单或分享浮层并重新识别当前页面（`ensure_clean_page_on_resume`）。（2026-11-07，`vivo V2131A` Run `f372139f` 实测自动收起残留 `composer_overlay` 恢复 `comment_drawer` 通过）
- [x] 对已执行或结果不明确的互动先结合 UI 状态（红像素占比、`推荐♡` 徽章、`已关注` 文案）与本地账本（`daily_quotas.json`）核对，避免恢复或重试时重复点赞、取消收藏、重复发表评论或回复。（2026-11-07，`vivo V2131A` Run `f372139f` & `61716eb1` 实测通过）
- [ ] 验证连续多视频、多位评论者的长稳运行，适配不同机型分辨率与微信浅色/深色界面，完整记录阶段耗时、恢复事件与失败原因。（已完成 `Hebe-BD00` 1236×2676 与 `vivo V2131A` 1080×2408 浅色主题连续多视频及 5 阶段多模式真机验证，深色主题长稳验证待补）

## 建议实施顺序

1. **第一批性能优化与连续采集（已完成）**：已解决微信控件树空树反复探测与 Agent RPC 模板/OCR 慢的问题，将单视频耗时压降下来，并完成推荐流上滑连续采集多条视频与跳过直播/广告卡片。
2. **第二批评论抽屉完整采集（已完成）**：已补齐折叠长评展开、评论列表翻页去重、到底/空态判定、二级回复展开与回复对象提取，清理评论昵称徽章混入。
3. **第三批评论者身份与主页往返（已完成）**：已实现点击评论者头像查看资料卡/主页、提取视频号 ID（兼容无视频号普通微信用户）、多位评论者往返与原评论锚点恢复。
4. **第三批互动触达、消息巡检与配额恢复（已完成，跳过私信）**：已完成作品点赞/推荐收藏/一级评论、评论点赞/回复（含三层自评保护）、插队抢先回复、主页关注与首作点赞收藏触达、个人页视频号消息中心巡检回复，以及每日配额持久化和恢复清理。
5. **第四批多运行模式与异常恢复（已完成，待服务端上报联调）**：已完成推荐流连续多视频滑动、跳过直播/广告、关键词搜索与同城/朋友/关注流切换、5 阶段全自动模式（`AUTO`）、验证码零点击暂停及有界异常恢复；下一步接入服务端事件上报队列联调。
