# 微信视频号插件

PI v2 入口：`wechat_channels/interface.json`。共享识别与动作节点：`wechat_channels/resource/base/pipeline/channels.json`。

可组合操作：打开微信、进入发现、打开视频号、打开当前视频作者主页、打开主页中的视频、点赞当前视频、返回作者主页、返回推荐页、进入视频号个人页。点赞先识别状态，已点赞则跳过；点击后必须确认状态，不盲目重试。

稳定导航文字优先使用多尺度模板，未命中再通过宿主 `MtaOcrFallback` 执行 OCR。新增操作需要同时修改 pipeline、interface、两份 locale 和相应逻辑测试。

当前组合版本为 0.3.0，目标 60 秒尚未完成真机验收。历史 0.2.6 完整流程记录约为 vivo 102.4 秒、TINA 119.3 秒，不能当作当前版本的性能保证。

构建和宿主补丁说明见仓库根 README。原始截图、运行日志、APK 和开发机环境脚本不随仓库发布。截图回归工具保留在 `scripts/`，需自行提供 `captures/` 中的样本。
