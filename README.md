# 微信视频号与小红书 Maa 插件

基于 MaaFramework / MaaTauriAndroid 的手机端自动化代码。两个插件独立维护，通过实时控件、图像模板与 OCR 识别页面。微信采集优先读取控件树，导航在控件信息不完整时先匹配模板、失败才调用 OCR；点赞只点击识别到的拇指图标并验证状态。

## 仓库结构

- `plugins/wechat/`：微信视频号 PI 资源、图标模板、OCR 模型、操作组合和测试。
- `plugins/xiaohongshu/`：小红书 Python Agent、页面解析、评论采集、Java 控件树助手、PI 资源和测试。
- `patches/maa-tauri-android.patch`：当前两个插件依赖的宿主修改。
- `docs/xiaohongshu-checklist.md`：主任务清单，记录字段、上传、关联、双链路和整体验收。
- `docs/xiaohongshu-behavior-checklist.md`：小红书 App 行为清单，记录采集、导航、互动、运行保护与真机验收；作品发布已移出需求范围。
- `docs/wechat-batch1.md`：视频号第一批采集范围、运行方式、三轮耗时及已知问题。

宿主补丁包含屏幕尺寸和截图管道修复、任务失败停止选项、微信 `MtaOcrFallback` 回调，以及小红书物理屏幕校验与前台剪贴板桥接。直接使用未经补丁修改的上游 APK，不能保证这些资源能运行。

## 当前能做什么

| 插件 | 已有行为 | 验证边界 |
| --- | --- | --- |
| 微信视频号 | 采集视频及作者资料，保留首次昵称和文案，生成作品伪链接并本地去重保存；第二批可见一级评论默认不开启 | 2026-11-06 同一策略三个视频完整手机端流程通过，平均 245.985 秒；修复单字评论边界、简介头部混入和基础信息模板误拒绝。首次昵称、标点及表情仍有 OCR 差异；评论翻页、二级回复和上报未完成 |
| 小红书图文 | 发现页选作品、`seen.json` 去重、无独立标题与长文多段落（4 屏滑动）展开补采、作者主页红薯号/蓝 V 认证主体提取、分享短链提取、返回列表连续采集 | 真机连续多篇批次（75.3 秒 / 3 篇）及长文企业号实测通过 |
| 小红书视频 | 免暂停采集、左下角折叠文案抽屉展开（含误触 `#话题` 自动返回改点边缘重展、底部快捷表情条过滤）、XML + MaaFramework `JOCR` 红薯号核验、右侧分享短链与多篇连续采集 | 真机连续多篇视频批次（42.3 秒 / 2 篇）实测通过 |
| 小红书评论与主页 | 图文内联评论区与视频半屏评论抽屉、翻页去重与到底/空态判定、二级回复展开与回复对象唯一身份（`replyToCommentKey`/`replyToDouyinId`）关联、评论者主页字段提取（缺失不猜测）与异常隔离（`skippedProfiles`） | 真机图文/视频多轮翻页、多位评论者主页往返与异常回退实测通过 |
| 小红书互动与消息 | 图文/视频点赞与收藏（防取消）、发表一级评论、评论点赞与回复（三层自评防循环回评）、指定作品插队回复、作者/评论者主页关注与首作点赞收藏、消息中心 `评论和@` 巡检回复、每日分项配额持久化 | 真机全流程验证通过（私信会话按指令跳过） |
| 小红书运行模式与恢复 | 推荐模式、关键词搜索（综合/最新/最多点赞/最多评论 + 笔记类型 + 发布时间筛选）、同城模式（`定位权限未开启` 自动点 `暂时不了`）、自动模式三阶段切换、滑块/验证码零点击暂停恢复、弹窗/意外页有界归位 | 真机三阶段自动模式（124.0 秒）与多分辨率单元测试（80 项全绿）通过 |

上述小红书实测记录来自 2026-11-04 至 2026-11-06 的 `vivo V2131A / Android 12`（小红书 `9.52.1`）。详细状态见 `plugins/xiaohongshu/status.json`。不包含服务端、MediaCrawler、真实采集数据或运行日志。

## 小红书清单现状

1. **[小红书行为进度清单](docs/xiaohongshu-behavior-checklist.md)（手机端 App 内行为验收）**：共 **43 项**，已完成并真机端到端验证 **42 项（97.7%）**，仅 **1 项**（进入目标用户私信会话）按指令跳过：
   - **图文采集与列表导航（6 / 6）**：可见卡片选帖去重、无独立标题与长文展开滚动补采、作者主页身份与红薯号核验、分享短链剪贴板提取、返回瀑布流连续处理。
   - **视频采集（5 / 5）**：免暂停采集、左下角折叠文案抽屉展开、误触内嵌 `#话题` 自动返回并改点左右非标签边缘重展、过滤抽屉底部快捷表情横条、XML + `JOCR` 红薯号核验、右侧分享短链与多篇连续采集。
   - **评论采集与评论者主页（7 / 7）**：图文内联评论区、视频半屏评论抽屉、翻页去重与到底/空态判定、二级回复展开与 `replyToCommentKey`/`replyToDouyinId` 关联、评论者主页字段提取（缺失不猜测）、多人往返与异常隔离（`skippedProfiles`）。
   - **作品与评论互动（6 / 6）**：图文/视频点赞与收藏（已赞/已藏防取消）、发表一级评论、评论点赞、评论回复（含三层自评防循环回评保护）、指定作品与目标评论插队回复。
   - **主页触达（3 / 4）**：条件关注（防误触推荐栏小关注按钮）、主页首篇作品点赞/收藏、记录实际动作并返回原作品/评论锚点；私信会话按指令跳过。
   - **消息处理（3 / 3）**：`消息` → `评论和@` 未读巡检、就地回复/点赞与账本去重、有界返回原任务页。
   - **搜索与运行模式（6 / 6）**：推荐模式连续处理、跳过 `广告`/`赞助` 推广卡片与推荐前缀/搜索双行截断标题核对、关键词搜索 + 4 种排序 + 笔记类型 + 发布时间筛选、同城模式（含弹出 `定位权限未开启` 时自动点 `暂时不了` 关闭弹窗及定位权限状态识别）、自动模式（`recommend` → `search` → `city`）切换与阶段页面强校验。
   - **配额、异常与运行恢复（6 / 6）**：每日分项配额持久化（`DailyQuotaStore` 跨重启保留）、滑块/验证码零点击暂停等待与恢复核对、加载失败/各类弹窗（`暂时不了`/`不再提醒`/青少年模式/版本更新）/意外页面有界归位、停止保护与恢复清理重识别、幂等防重复互动、多篇多人连续运行与多分辨率适配。
2. **[小红书上传与操作主任务清单](docs/xiaohongshu-checklist.md)（数据链路、服务端上报与双链路协同）**：共 **54 项**（当前勾选 `0 / 54`）：
   - 其中 `[仅手机端]` 16 项对应的手机 UI 自动化采集、互动、模式与恢复行为（除跳过的私信外）已在行为清单中全部跑通；
   - 本地 SQLite 队列模型（`plugins/xiaohongshu/xiaohongshu/agent/discovery.py`）已完成授权码隔离、稳定事件 ID 与按发送批次 ACK 清理逻辑，待将采集/互动结果接入队列、接通服务端上报/ACK 并完成数据库 SQL 与 `MediaCrawler` 双链路联调验收后勾选。

目前资源随 APK 打包，**宿主插件导入、热更新和切换管理尚未完成**。

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

## CI 自动构建与发版

每次推送分支（包括 `main`），或在 Actions 手动运行 `Android Release`，都会测试插件并分别构建微信、小红书 **arm64-v8a Release APK**。两个包全部构建、签名校验成功后，自动发布到 [GitHub Releases](https://github.com/muyuzhong/xiaohongshu_wx_maa/releases)，附带 `SHA256SUMS`。失败的构建不会发布新版本。

版本名为 `1.0.<运行序号>`，versionCode 随运行序号递增；Release 标签为 `build-<运行序号>-<commit前7位>`，APK 文件名也带 commit。每次构建独立保留，不覆盖旧版本，也不将每次 CI 构建标记为稳定版 Latest。标签推送不会再次触发构建。

CI 单独拉取上面固定版本的宿主并应用补丁，不将宿主源码提交到本仓库。本自动发版流程按要求显式使用 `--release`；下面手动开发构建仍使用 debug。

签名依赖仓库 Actions Secrets：`ANDROID_RELEASE_KEYSTORE_BASE64`（PKCS12 密钥文件的 Base64）和 `ANDROID_RELEASE_KEYSTORE_PASSWORD`（密钥库及私钥共用密码），别名固定为 `release`。两种 APK 共用固定发行密钥，不能每次生成新密钥；私钥与密码不得提交。新建或迁移仓库时需要配置相同 Secrets 才能保持覆盖升级。

首次从原有 debug 包切换到此 Release 包可能因签名不同无法覆盖安装，需先备份数据再处理旧包；后续 CI 包保持相同签名。CI 验证不等于真机行为验收。

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
bash plugins/wechat/tools/build-ui-helper.sh
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

两个插件复用 `plugins/xiaohongshu/tools/UiDump.java` 的通用控件树导出实现，各自打包 `ui-dump.dex`；修改 Java 源码后需运行两个插件的 `build-ui-helper.sh` 重新生成。微信只使用其 XML 导出入口，不调用小红书专用的恢复任务入口。模型、图标和助手资源位于打包目录，不依赖开发机的 `/tmp` 或本地截图目录。

## 手机测试

保持手机解锁、亮屏、竖屏，并登录对应应用。助手需要已启动、已授权的 Shizuku。涉及多台设备的手动安装或调试时，显式指定 `adb -s <serial>`，不默认选择设备。

小红书评论任务的使用方式：先打开一篇正常图文，返回助手，选择“翻页采集评论 → 访问评论者 → 返回”，启用前台模式且关闭运行前强制停止目标应用，然后运行。新版本任务恢复现有小红书页面，最多采集 10 轮重叠视图、展开可见回复并进行 5 次评论者主页访问。每次访问后核对原评论位置；加载最多等待 90 秒。自动展开折叠评论，识别到底和明确空状态；只有到达末尾且采集数与前后显示总数一致时，才标记文本评论采集完整，否则保留部分结果。回复对象只提取显示名，歧义时留空，不推断目标用户 ID；图片附件不在完整性声明内。不自动发表内容或执行互动。当前已安装 APK 仍是旧版本，本批代码以手机 Python 直接调用验证，须重新打包后才能通过助手界面使用。

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
