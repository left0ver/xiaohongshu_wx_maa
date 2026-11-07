# 当前手机已修复任务复测

日期：2026-11-07。Hebe-BD00 / TINA-AN00，Android 11，微信 8.0.76，1236×2676。

范围：按用户要求跳过第一个日文昵称识别问题，复测消息巡检、深色作者菜单、原失败插队回复和简介处理。没有修改业务代码；将此前修改同步至独立手机测试目录，22 个 Python 文件 SHA256 与本地一致。电脑 124 项测试通过，Android 124 项通过（5.918 秒）。未重新打包 APK，未测试服务端上传。

证据根目录：[regression](../plugins/wechat/data/acceptance/current-phone-20261107/regression/)。每个 Run 目录包含 result.json、OCR JSON、逐步截图、耗时与 MAA 日志；根目录保存六项运行日志、测试日志和源码哈希。

| 任务 | Run | 结果 |
| --- | --- | --- |
| 浅色单视频及作者采集 | 4b15b051-db9a-4941-89ad-ecce9df97f9d | 流程完成，任务 33.246 秒；H相见. 视频与作者落库，保存简介等于第一步 parse_author 结果。但简介仍混入“广东河源”。 |
| 消息巡检入口 | 024c57d1-e20d-4124-b201-a5a494d3b118 | 失败，10.342 秒。已成功点击本机“消息”入口，进入消息中心；页面误判阻断后续巡检。 |
| 原失败作品插队回复 | 885963fb-6355-4023-bff9-1dce1e6092b2 | 定位与防重通过，任务 23.659 秒。评论列表已滚动、作品头部不可见，恢复顶部后匹配原作品和恒萌琪铭评论。返回 already_replied、clicked=false、submitted=false、verified=true。此次没有重新发送回复。 |
| 深色单视频及作者采集 | 1b11fe3f-4986-4907-8807-e0f187e8e9dc | 流程完成，任务 35.538 秒。AA丽泽主页三点菜单→更多信息→基础信息成功，读取 sphMbo0UFAN5GIW，视频落库。但无简介主页的“9条朋友圈内容”被第一步解析存为简介。 |
| 郁WT完整采集补测 | 69407091-67e5-47bb-9a5b-0e0887f1d34f | 6.273 秒失败，停在昵称识别；点赞列表播放页“郁WT”和“已关注”合并识别，报 The feed author is missing or ambiguous。未进入简介步骤，不能记为完整流程通过。 |
| 郁WT折叠简介局部补测 | 11c6616b-ab30-4e38-bb34-ac076242b4c6 | 15.279 秒完成。人工打开原作者主页后，生产展开逻辑点击一次，parse_author 提取六行简介，原样保存文本供核对；无 read_profile_bio。此项只证明主页展开及解析。 |

## 消息任务仍需适配

本机消息中心顶部为爱心图标和“评论”，没有代码要求的“点赞／评论／关注”三个文字页签。classify_page_state 要求三个页签同时存在才能判定 channels_message_center，因此本机页面被判为 comment_drawer。异常为：

```text
Could not navigate to Channels message center from state=comment_drawer
```

“消息／视频号消息”入口兼容已生效，失败点已推进到消息中心页面识别。没有执行消息点赞或回复；本轮保持问题原样供下一步处理。

- [当时截图](../plugins/wechat/data/acceptance/current-phone-20261107/regression/024c57d1-e20d-4124-b201-a5a494d3b118/003-messages-center.png)
- [具体运行日志](../plugins/wechat/data/acceptance/current-phone-20261107/regression/messages.log)
- [结果 JSON](../plugins/wechat/data/acceptance/current-phone-20261107/regression/024c57d1-e20d-4124-b201-a5a494d3b118/result.json)

## 简介结果与限制

按用户确认的规则，仅采用第一步 parse_author 输出，不再调用已移除的 read_profile_bio。两个完整采集 Run 均校验最终 authorBio 与该主页解析输出完全相同。此实现规则通过验证，不代表第一步字段内容全部正确。

郁WT展开后六行正文：

```text
天天开心
希望每天有人私信投稿主播懒不想找素材
关注即可入股做我的第一批股东吧
目标粉丝1000
当前738
回来了11月6号开始发视频
```

- [郁WT展开截图](../plugins/wechat/data/acceptance/current-phone-20261107/regression/11c6616b-ab30-4e38-bb34-ac076242b4c6/final.png) / [简介文字](../plugins/wechat/data/acceptance/current-phone-20261107/regression/11c6616b-ab30-4e38-bb34-ac076242b4c6/bio.txt)
- [深色主页及误入的朋友圈计数](../plugins/wechat/data/acceptance/current-phone-20261107/regression/1b11fe3f-4986-4907-8807-e0f187e8e9dc/007-author-profile.png) / [简介文字](../plugins/wechat/data/acceptance/current-phone-20261107/regression/1b11fe3f-4986-4907-8807-e0f187e8e9dc/bio.txt)
- [H相见.简介文字](../plugins/wechat/data/acceptance/current-phone-20261107/regression/4b15b051-db9a-4941-89ad-ecce9df97f9d/bio.txt)

## 测试收尾

系统夜间模式恢复为原值 Night mode: no，微信浅色界面已恢复。没有新增点赞、关注或发送回复；本轮插队结果中的配额使用数仍为 like=2、collect=2、comment=3、follow=1、dm=0。没有重置账本或绕过配额。

同步时的预检查 Run 6fa7253a-4480-41f6-a880-1fd7ec95713e 检测到旧模块，业务动作前即停止。修正部署路径并核对源码哈希后才执行上述正式复测；不能将旧模块预检查作为当前代码功能结果。

本轮结论：深色菜单已可用，原插队定位失败已消除且防重生效；简介遵循用户指定的数据来源，但第一步仍有字段污染；消息中心页面布局兼容仍未通过。不能据此宣布所有问题解决或全部清单通过。

## 消息中心布局后续修复

按用户后续授权，不再要求“关注”页签；保留顶部“点赞”与“评论”文字识别，并在只有“评论”时补充爱心模板匹配。新增 messages-like.png 与 messages-like-inactive.png，分别直接裁自原失败截图及本轮切换评论后的截图，覆盖爱心选中粗描边和未选中灰色细描边。按 720 宽归一化，模板 32×29，匹配 ROI=[120,60,400,100]，阈值 0.88。识别结果以 source=template 和 templateNode 记录，原 OCR 文字保留；仅出现“评论”不会被判作消息中心。

电脑／Android 各 126 项单元测试通过（1.930／5.932 秒）；保存截图重放覆盖两种页签状态，以及浅色主页、深色主页、评论面板三个负例，见 [匹配结果](../plugins/wechat/data/acceptance/current-phone-20261107/regression/messages-template-replay.json)。这些负例不等于全机型适配证明。

第一次真机 Run `7051778e-1626-491b-a74c-035125f3b377` 从原失败消息页成功切到评论并返回，暴露未选中爱心的细描边差异后补充模板。最终 Run `6a7544be-641a-4cfa-bf21-a0870a19f46f` 从视频页开始，完整执行个人页→消息入口→评论页→两次返回至视频页，任务 34.575 秒（含启动总计 34.871 秒）。messages-comments 和 messages-return-check 均成功匹配模板并记录为 mixed。当前评论页显示“暂无消息”，messagesFound=0，returnedToTask=true；配置 maxMessages=0、like=false，没有消息点赞或发送回复，配额不变。此结果证明页面识别及导航修复，不证明有消息时的互动已验收。

证据：[最终日志](../plugins/wechat/data/acceptance/current-phone-20261107/regression/messages-fix-final.log)、[结果与逐步截图](../plugins/wechat/data/acceptance/current-phone-20261107/regression/6a7544be-641a-4cfa-bf21-a0870a19f46f/)、[Android 测试日志](../plugins/wechat/data/acceptance/current-phone-20261107/regression/messages-fix-tests.log)。变更已同步到独立手机测试目录（包含此前简介边界修改），未重打正式 APK；未写入采集数据库或测试服务端上传。
