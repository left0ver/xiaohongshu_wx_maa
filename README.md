# 微信视频号与小红书 Maa 插件

基于 MaaFramework / MaaTauriAndroid 的手机端自动化代码。两个插件独立维护，主要通过实时控件、图像模板与 OCR 识别页面。微信原组合的点赞步骤按已识别的视频页面连续点击视频区域三次，使用短边 720 归一化坐标，仍有布局适配限制。

## 仓库结构

- `plugins/wechat/`：微信视频号 PI 资源、图标模板、OCR 模型、操作组合和测试。
- `plugins/xiaohongshu/`：小红书 Python Agent、页面解析、评论采集、Java 控件树助手、PI 资源和测试。
- `patches/maa-tauri-android.patch`：当前两个插件依赖的宿主修改。
- `docs/xiaohongshu-checklist.md`：原始需求清单。勾选要求完整验收，不能将本地单次成功等同于服务端验收。
- `docs/wechat-batch1.md`：视频号第一批采集范围、运行方式、三轮耗时及已知问题。

宿主补丁包含屏幕尺寸和截图管道修复、任务失败停止选项、微信 `MtaOcrFallback` 回调，以及小红书物理屏幕校验与前台剪贴板桥接。直接使用未经补丁修改的上游 APK，不能保证这些资源能运行。

## 当前能做什么

| 插件 | 已有行为 | 验证边界 |
| --- | --- | --- |
| 微信视频号 | 保留原有操作组合；新增视频、作者资料采集、返回身份校验、本地去重保存及阶段计时 | 采集代码的应用内三轮实测：两轮失败、一轮成功（243.122 秒）；本次整合后的 APK 尚未重新构建验收 |
| 小红书图文 | 发现页选作品、读取标题正文、进入作者主页取小红书号、复制分享链接、返回列表并本地保存 | 一次完整实测：采集 24.6 秒，含准备共 37 秒；长正文和无独立标题等样式仍待适配 |
| 小红书评论 | 从当前图文打开评论区、读取可见评论、进入一位评论者主页、读取小红书号和粉丝数、返回原评论位置 | 一次实测读取 5 条评论并访问 1 人：操作 17 秒，总计 22.8 秒 |
| 小红书视频 | 筛选视频、读取文案、进入作者主页等代码已接入 | 完整流程未验收，部分作者主页加载或身份校验失败 |

上述小红书实测记录来自 2026-11-04 的 vivo V2131A / Android 12。详细状态见 `plugins/xiaohongshu/status.json`。不包含服务端、MediaCrawler、真实采集数据或运行日志。

目前资源随 APK 打包，**插件导入、热更新和切换管理尚未完成**。评论全量翻页、二级回复关联、视频评论抽屉、搜索/同城、互动发布、服务端上传和多机型验收也尚未完成。SQLite 队列已有基础逻辑和测试，但还没有与采集上传链路接通。

## 依赖与拉取方式

本仓库只维护插件与必要的宿主补丁，不包含 MaaTauriAndroid 源码，也不使用 Git 子模块。宿主从上游单独克隆到同级目录，固定版本为 `4da508b20e4d7be132621ca24ec32f2b5b5a5fa0`。

在准备存放两个项目的目录执行：

```bash
git clone https://github.com/muyuzhong/xiaohongshu_wx_maa.git
git clone https://github.com/Craun718/MaaTauriAndroid.git MaaTauriAndroid-host
cd xiaohongshu_wx_maa
plugin_root="$PWD"
host_root="$(cd ../MaaTauriAndroid-host && pwd)"
git -C "$host_root" checkout --detach 4da508b20e4d7be132621ca24ec32f2b5b5a5fa0
git -C "$host_root" apply --check "$plugin_root/patches/maa-tauri-android.patch"
git -C "$host_root" apply "$plugin_root/patches/maa-tauri-android.patch"
```

补丁只需应用一次，应用后宿主工作区显示修改状态是预期结果。升级宿主时需要重新检查补丁及两个插件，不要直接切到最新版本。已有开发目录可以继续使用，无需覆盖或重置其中的修改。

## 构建 APK

Linux 构建环境需按宿主说明安装 Rust/Android 工具链、pnpm 11、JDK 17、Android SDK 和 NDK。小红书 Java 助手使用 Android API 36、Build Tools 36.0.0；历史构建使用 NDK 28.2.13676358。自行设置 `JAVA_HOME`、`ANDROID_HOME`，不要复制某台开发电脑的绝对路径。

以下命令在本仓库根目录执行，默认宿主位于同级的 `MaaTauriAndroid-host`；若使用已有目录，请将 `host_root` 改为实际路径。先安装宿主的前端依赖：

```bash
plugin_root="$PWD"
host_root="$(cd ../MaaTauriAndroid-host && pwd)"
(cd "$host_root" && corepack pnpm@11 install)
```

微信视频号采集需要 Python Agent runtime；Maa 和 NumPy 由宿主匹配版本的 Agent core 提供：

```bash
python3 "$host_root/scripts/build_apk.py" \
  --profile "$plugin_root/plugins/wechat/profile.toml" --debug --use-prebuilt-core
```

小红书需要 Java 助手和 Python Agent runtime：

```bash
bash plugins/xiaohongshu/tools/build-ui-helper.sh
python3 "$host_root/scripts/build_apk.py" \
  --profile "$plugin_root/plugins/xiaohongshu/profile.toml" --debug --use-prebuilt-core
```

`build_apk.py` 使用上游的依赖准备和运行时构建流程；会修改宿主 Android 工程的本地 profile 配置，并执行编译。当前固定宿主 commit 没有 tag，因此这里明确使用 debug 签名。APK 位于宿主 Gradle 输出目录；本仓库不提交生成的 APK、runtime ZIP、签名密钥或 SDK。

`ui-dump.dex` 与 Java 源码一起保留，修改 `UiDump.java` 后必须重新生成。模型、图标和助手资源位于打包目录，不依赖开发机的 `/tmp` 或本地截图目录。

## 手机测试

保持手机解锁、亮屏、竖屏，并登录对应应用。助手需要已启动、已授权的 Shizuku。涉及多台设备的手动安装或调试时，显式指定 `adb -s <serial>`，不默认选择设备。

小红书评论任务的使用方式：先打开一篇正常图文，返回助手，选择“读取评论 → 进入评论者主页 → 返回”，启用前台模式且关闭运行前强制停止目标应用，然后运行。任务恢复现有小红书页面，只读取可见文字评论并访问一位评论者，不自动发表内容或执行互动。

本地结果由 Agent 写入运行目录之外的 `xiaohongshu-data/`，不代表已经上传、收到 ACK 或完成数据库关联。识别不明确、身份或返回位置不一致时，任务停止且不提交成功结果。

## 代码测试

以下纯业务逻辑测试不操作手机，不依赖私人截图：

```bash
python3 -m unittest discover -s plugins/wechat/tests
python3 -m unittest discover -s plugins/wechat/wechat_channels/collector
python3 -m unittest discover -s plugins/xiaohongshu/tests
```

小红书命令启动的 Android 专用测试在桌面会跳过。微信 `scripts/verify_captures.py` 和 `verify_operations.py` 是额外的截图回归工具，需要开发者自行提供本地截图及安装 `requirements-dev.txt` 中的依赖；截图不会随仓库分发。

## 第三方项目与许可

宿主来自 [MaaTauriAndroid](https://github.com/Craun718/MaaTauriAndroid)，许可副本在 `licenses/MaaTauriAndroid-LGPL-2.1.txt`，宿主补丁遵循其原有许可。MaaFramework 的二进制与许可证由宿主构建流程获取；OCR 模型的来源、PaddleOCR 与 MaaCommonAssets 许可证位于各插件的 `resource/base/model/ocr/`。

保留各第三方文件原有许可。本仓库新增业务代码暂未另行指定统一开源许可证。
