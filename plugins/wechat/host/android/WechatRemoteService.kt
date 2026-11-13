package top.natsuu.mta

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.provider.Settings
import androidx.core.app.NotificationCompat
import fi.iki.elonen.NanoHTTPD
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

class WechatRemoteService : Service() {
    private val scheduler = Executors.newSingleThreadScheduledExecutor()
    private val worker = Executors.newSingleThreadExecutor()
    private val receipts = ConcurrentHashMap<String, CompletableFuture<JSONObject>>()
    private val taskResults = ConcurrentHashMap<String, JSONObject>()
    private val terminal = ConcurrentHashMap<String, JSONObject>()
    private val queued = ConcurrentHashMap<String, JSONObject>()
    private val executionLock = Any()
    @Volatile private var stopGeneration = 0L
    private var bridge: NanoHTTPD? = null
    @Volatile private var socket: WebSocket? = null
    @Volatile private var authorized = false
    @Volatile private var current = ""
    @Volatile private var executing = false
    @Volatile private var bridgeCommand = ""
    @Volatile private var stopRequested = false
    private lateinit var account: JSONObject
    private lateinit var directory: File
    private var scope = ""
    private var bridgeToken = UUID.randomUUID().toString()
    private val runtimeId = UUID.randomUUID().toString()
    @Volatile private var clientStatus = JSONObject()
    private var statusCheckedAt = 0L

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        startForeground(9401, taskNotification())
        AppPreparationManager.start(this)
    }

    private fun taskNotification(): android.app.Notification {
        val preferences = getSharedPreferences(PREFS, MODE_PRIVATE)
        val appLabel = applicationInfo.loadLabel(packageManager).toString()
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(CHANNEL,
            preferences.getString("notification_channel", appLabel), NotificationManager.IMPORTANCE_LOW))
        val launch = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CHANNEL).setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle(preferences.getString("notification_title", appLabel))
            .setContentText(preferences.getString("notification_connecting", ""))
            .setContentIntent(launch).setOngoing(true).build()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (::account.isInitialized) return START_STICKY
        val saved = getSharedPreferences(PREFS, MODE_PRIVATE).getString("account", null)
        if (saved == null) { stopSelf(); return START_NOT_STICKY }
        account = JSONObject(SecretBridge.decrypt(saved))
        scope = digest(account.getString("server") + "|" + account.getString("softwareCode") + "|" + account.getString("licenseKey") + "|" + account.getString("machine"))
        directory = File(filesDir, "wechat-remote/$scope").apply { mkdirs() }
        loadPendingResults()
        bridge = LocalBridge().also { it.start(30_000, true) }
        scheduler.scheduleWithFixedDelay({
            try {
                if (socket == null) connect()
                else if (authorized) {
                    send(envelope())
                    terminal.values.forEach { send(it) }
                    if (System.currentTimeMillis() - statusCheckedAt > 300_000) {
                        val info = packageManager.getPackageInfo(packageName, 0)
                        clientStatus = post("/api/v1/client/status", identity().put("clientApp", "WECHAT_CHANNELS")
                            .put("clientVersion", info.versionName).put("clientVersionCode", info.longVersionCode))
                        statusCheckedAt = System.currentTimeMillis()
                    }
                    if (current.isNotEmpty()) send(JSONObject().put("type", "command_status").put("commandId", current)
                        .put("status", if (executing) "RUNNING" else "QUEUED").put("message", lastProgress))
                }
            } catch (error: Exception) {
                connectionState = "连接失败：${error.message?.take(160) ?: error.javaClass.simpleName}"
                socket?.cancel()
                socket = null
                authorized = false
            }
        }, 0, 15, TimeUnit.SECONDS)
        return START_STICKY
    }

    private fun connect() {
        val verified = verifyAccount(account)
        expiresAt = verified.getLong("endTime")
        val address = account.getString("server").replaceFirst("https://", "wss://").replaceFirst("http://", "ws://") + "/ws/telemetry"
        connectionState = "连接中"
        socket = http.newWebSocket(Request.Builder().url(address).build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) { webSocket.send(envelope().toString()) }
            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val message = JSONObject(text)
                    when (message.optString("type")) {
                        "command" -> accept(message)
                        "command_ack_receipt" -> if (message.optBoolean("ok")) {
                            val id = message.getString("commandId")
                            terminal.remove(id)
                            File(directory, "$id.receipt.json").delete()
                        }
                        else -> {
                            if (message.optString("error") == "unauthorized") { authorized = false; connectionState = "授权失效"; nativeStop(); webSocket.close(1008, "authorization"); return }
                            if (message.optBoolean("ok")) {
                                val connectedNow = !authorized
                                authorized = true; connectionState = "在线"
                                if (connectedNow) worker.execute { if (current.isEmpty()) synchronizeUploads() }
                            }
                            receipts.remove(message.optString("discoveryBatchId"))?.complete(message)
                        }
                    }
                } catch (error: Exception) { connectionState = "任务通信异常：${error.javaClass.simpleName}" }
            }
            override fun onFailure(webSocket: WebSocket, error: Throwable, response: Response?) { disconnected(webSocket) }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) { disconnected(webSocket) }
        })
    }

    private fun disconnected(connection: WebSocket) {
        if (socket === connection) { socket = null; authorized = false; connectionState = "等待重新连接" }
    }

    private fun identity() = identity(account)

    private fun envelope(): JSONObject {
        val version = packageManager.getPackageInfo(packageName, 0).versionName ?: "1.0.0"
        return identity().put("clientApp", "WECHAT_CHANNELS").put("clientRole", "OPERATOR").put("appName", "视频号助手")
            .put("clientVersion", version).put("protocolVersion", 1).put("supportsCommandLeaseHeartbeat", true)
            .put("capabilities", JSONArray(listOf("WECHAT_CHANNELS_TASK", "STOP_TASK", "SYNC_CONFIG")))
            .put("runtimeSessionId", runtimeId).put("currentCommandId", current)
            .put("taskState", if (current.isEmpty()) "IDLE" else if (executing) "RUNNING" else "QUEUED")
            .put("queuedCommandIds", JSONArray(queued.keys.toList())).put("recoverableCommandIds", JSONArray(terminal.keys.toList()))
            .put("activeCommandIds", JSONArray(queued.keys.toList() + listOf(current).filter { it.isNotEmpty() }))
            .put("screenCaptureActive", RuntimeBridge.isAppReady() && RuntimeBridge.isProjectReady()
                && RuntimeBridge.isEngineReady() && RuntimeBridge.privilegedControlState() == 3)
    }

    private fun accept(command: JSONObject) {
        val id = command.getString("commandId")
        require(id.matches(Regex("[0-9a-fA-F-]{36}"))) { "任务编号无效" }
        val complete = File(directory, "$id.done.json")
        if (complete.isFile) { send(JSONObject(complete.readText())); return }
        if (current == id || queued.containsKey(id)) return
        when (command.getString("command")) {
            "STOP_TASK" -> {
                synchronized(executionLock) {
                    stopGeneration++
                    stopRequested = true
                    nativeStop()
                }
                queued.values.toList().forEach { finish(it.getString("commandId"), false, "任务已停止") }
                queued.clear()
                worker.execute { finish(id, true, "任务已停止") }
            }
            "SYNC_CONFIG" -> {
                atomic(File(directory, "config.json"), command.getJSONObject("taskConfig").toString())
                finish(id, true, "配置已同步")
            }
            "CLIENT_SELF_TEST" -> finish(id, RuntimeBridge.isEngineReady(), "手机运行环境检查完成")
            "WECHAT_CHANNELS_TASK" -> {
                val generation = stopGeneration
                queued[id] = command
                send(JSONObject().put("type", "command_status").put("commandId", id).put("status", "QUEUED").put("message", "手机已接收任务"))
                worker.execute { if (queued.remove(id) != null) execute(command, generation) }
            }
            else -> finish(id, false, "不支持的任务类型")
        }
    }

    private fun execute(command: JSONObject, generation: Long) {
        val id = command.getString("commandId")
        current = id; bridgeCommand = id; stopRequested = false; lastProgress = "准备执行"
        atomic(File(directory, "$id.active.json"), command.toString())
        var startedExecution: String? = null
        try {
            lastProgress = "正在准备手机运行环境"
            awaitPreparation(generation)
            val config = JSONObject(command.getJSONObject("taskConfig").toString())
            config.put("remote", JSONObject().put("url", "http://127.0.0.1:${bridge!!.listeningPort}")
                .put("token", bridgeToken).put("commandId", id).put("scope", scope).put("machine", account.getString("machine")))
            val started = synchronized(executionLock) {
                check(generation == stopGeneration) { "任务已停止" }
                JSONObject(nativeStart(config.toString()))
            }
            require(started.optBoolean("success")) { started.optString("message", "任务启动失败") }
            executing = true
            send(JSONObject().put("type", "command_status").put("commandId", id).put("status", "RUNNING").put("message", "正在执行视频号任务"))
            val executionId = started.getString("executionId")
            startedExecution = executionId
            while (true) {
                Thread.sleep(500)
                val state = JSONObject(nativeStatus())
                if (state.optString("executionId") == executionId && state.optString("state") == "Idle") {
                    val result = taskResults.remove(id) ?: JSONObject().put("success", false).put("message", "执行结束但未取得业务结果")
                    val success = !stopRequested && state.optString("severity") != "error" && result.optBoolean("success")
                    finish(id, success, if (stopRequested) "任务已停止" else result.optString("message", state.optString("message")))
                    break
                }
            }
        } catch (error: Exception) {
            if (startedExecution != null) {
                nativeStop()
                while (true) {
                    val state = JSONObject(nativeStatus())
                    if (state.optString("executionId") != startedExecution || state.optString("state") == "Idle") break
                    Thread.sleep(100)
                }
            }
            finish(id, false, error.message ?: error.javaClass.simpleName)
            submitDiagnostic(id, error)
        } finally { executing = false; current = ""; bridgeCommand = ""; lastProgress = "等待任务" }
    }

    private fun awaitPreparation(generation: Long) {
        val deadline = android.os.SystemClock.elapsedRealtime() + 180_000
        check(generation == stopGeneration) { "任务已停止" }
        if (!RuntimeBridge.isAppReady()) {
            lastProgress = "正在初始化视频号助手"
            ScheduleExecutionService.initializeApp(this)
        }
        while (!RuntimeBridge.isAppReady() || !RuntimeBridge.isProjectReady() || !RuntimeBridge.isEngineReady()) {
            check(generation == stopGeneration) { "任务已停止" }
            check(!RuntimeBridge.isPreparationFailed()) { "手机运行环境准备失败，请查看首页提示" }
            check(android.os.SystemClock.elapsedRealtime() < deadline) {
                if (RuntimeBridge.isAppReady()) "手机运行环境准备超时"
                else "后台初始化未完成，请打开视频号助手并检查后台启动权限"
            }
            Thread.sleep(100)
        }
        check(generation == stopGeneration) { "任务已停止" }
        require(RuntimeBridge.connectPrivilegedService(15_000)) { "自动化控制连接尚未就绪，请检查 Shizuku 状态" }
    }

    private fun synchronizeUploads() {
        val id = "sync-" + UUID.randomUUID().toString()
        bridgeCommand = id
        try {
            awaitPreparation(stopGeneration)
            val config = JSONObject().put("task", "sync_discovery").put("options", JSONObject())
                .put("remote", JSONObject().put("url", "http://127.0.0.1:${bridge!!.listeningPort}")
                    .put("token", bridgeToken).put("commandId", id).put("scope", scope).put("machine", account.getString("machine")))
            val started = JSONObject(nativeStart(config.toString()))
            check(started.optBoolean("success")) { started.optString("message", "待上传数据同步未启动") }
            val executionId = started.getString("executionId")
            while (true) {
                Thread.sleep(500)
                val state = JSONObject(nativeStatus())
                if (state.optString("executionId") == executionId && state.optString("state") == "Idle") {
                    val result = taskResults.remove(id)
                    check(result?.optBoolean("success") == true) { "待上传数据仍保留在手机" }
                    lastProgress = result.optString("message", "待上传数据已确认")
                    break
                }
            }
        } catch (error: Exception) { lastProgress = "数据同步失败：${error.message ?: error.javaClass.simpleName}" }
        finally { bridgeCommand = "" }
    }

    @Synchronized private fun finish(id: String, success: Boolean, message: String) {
        val receipt = JSONObject().put("type", "command_ack").put("commandId", id).put("success", success)
            .put("message", message.take(300))
        atomic(File(directory, "$id.done.json"), receipt.toString())
        atomic(File(directory, "$id.receipt.json"), receipt.toString())
        File(directory, "$id.active.json").delete()
        terminal[id] = receipt
        if (authorized && socket?.send(receipt.toString()) != true) {
            authorized = false; socket?.cancel(); socket = null
        }
    }

    private fun loadPendingResults() {
        directory.listFiles().orEmpty().filter { it.name.endsWith(".receipt.json") }.forEach {
            val item = JSONObject(it.readText()); terminal[item.getString("commandId")] = item
        }
        directory.listFiles().orEmpty().filter { it.name.endsWith(".active.json") }.forEach {
            val item = JSONObject(it.readText())
            finish(item.getString("commandId"), false, "App 重新启动，原任务已中断；请查看已保存的数据和操作记录")
        }
    }

    private fun submitDiagnostic(commandId: String, error: Exception) {
        scheduler.execute {
            try { post("/api/v1/client/diagnostics", identity().put("clientVersion", packageManager.getPackageInfo(packageName, 0).versionName)
                .put("errorSummary", "视频号任务失败：${error.javaClass.simpleName}").put("permissionStatus", "engineReady=${RuntimeBridge.isEngineReady()}")
                .put("runtimeLog", "commandId=$commandId")) }
            catch (failure: Exception) { connectionState = "诊断上传失败：${failure.javaClass.simpleName}" }
        }
    }

    private fun post(path: String, body: JSONObject): JSONObject = post(account, path, body)

    private fun send(message: JSONObject) { check(socket?.send(message.toString()) == true) { "服务连接不可用" } }

    private fun installUpdate() {
        check(current.isEmpty()) { "请等待当前任务结束" }
        val release = clientStatus.optJSONObject("release") ?: throw IllegalStateException("没有可用更新")
        check(release.optBoolean("updateAvailable")) { "已经是当前版本" }
        val url = release.getString("downloadUrl")
        require(java.net.URI(url).scheme == "https") { "更新地址必须为 HTTPS" }
        val expected = release.getString("sha256").lowercase(java.util.Locale.ROOT)
        require(expected.matches(Regex("[0-9a-f]{64}"))) { "更新校验值无效" }
        worker.execute {
            try {
                val destination = File(cacheDir, "wechat-update.apk")
                val checksum = MessageDigest.getInstance("SHA-256")
                http.newCall(Request.Builder().url(url).build()).execute().use { response ->
                    check(response.isSuccessful) { "更新下载失败" }
                    val body = requireNotNull(response.body)
                    require(body.contentLength() <= 250_000_000) { "更新包超过大小限制" }
                    body.byteStream().use { input -> destination.outputStream().use { output ->
                        val buffer = ByteArray(65536); var total = 0L
                        while (true) {
                            val count = input.read(buffer); if (count < 0) break
                            total += count; require(total <= 250_000_000) { "更新包超过大小限制" }
                            output.write(buffer, 0, count); checksum.update(buffer, 0, count)
                        }
                    } }
                }
                check(checksum.digest().joinToString("") { "%02x".format(it) } == expected) { "更新包校验失败" }
                RuntimeBridge.installUpdateApk(destination.absolutePath)
            } catch (error: Exception) { connectionState = "更新失败：${error.message ?: error.javaClass.simpleName}" }
        }
    }

    private inner class LocalBridge : NanoHTTPD("127.0.0.1", 0) {
        override fun serve(session: IHTTPSession): Response {
            if (session.headers["authorization"] != "Bearer $bridgeToken" || session.method != Method.POST)
                return newFixedLengthResponse(Response.Status.UNAUTHORIZED, "application/json", "{}")
            return try {
                require((session.headers["content-length"]?.toLong() ?: 0) in 1..4_500_000) { "请求长度无效" }
                val files = HashMap<String,String>(); session.parseBody(files)
                val body = JSONObject(requireNotNull(files["postData"]))
                require(body.getString("commandId") == bridgeCommand && bridgeCommand.isNotEmpty()) { "任务身份不匹配" }
                val result = when (session.uri) {
                    "/upload" -> {
                        val batchId = UUID.randomUUID().toString()
                        val pending = CompletableFuture<JSONObject>(); receipts[batchId] = pending
                        try {
                            send(envelope().put("discoveryBatchId", batchId).put("discoveryEvents", body.getJSONArray("discoveryEvents")))
                            pending.get(30, TimeUnit.SECONDS)
                        } finally { receipts.remove(batchId) }
                    }
                    "/progress" -> { lastProgress = body.optString("message", "正在执行").take(300); JSONObject().put("ok", true) }
                    "/result" -> { taskResults[bridgeCommand] = body.getJSONObject("result"); JSONObject().put("ok", true) }
                    else -> throw IllegalArgumentException("未知操作")
                }
                newFixedLengthResponse(Response.Status.OK, "application/json", result.toString())
            } catch (error: Exception) {
                newFixedLengthResponse(Response.Status.INTERNAL_ERROR, "application/json", JSONObject().put("error", error.javaClass.simpleName).toString())
            }
        }
    }

    override fun onDestroy() {
        authorized = false
        synchronized(executionLock) {
            stopGeneration++
            stopRequested = true
            nativeStop()
        }
        socket?.close(1000, "stop")
        scheduler.shutdownNow(); worker.shutdownNow(); bridge?.stop(); instance = null; connectionState = "已断开"
        super.onDestroy()
    }

    companion object {
        private const val PREFS = "wechat_remote_account"
        private const val CHANNEL = "wechat_remote"
        private val http = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS).pingInterval(20, TimeUnit.SECONDS).build()
        @Volatile private var instance: WechatRemoteService? = null
        @Volatile private var connectionState = "未连接"
        @Volatile private var lastProgress = "等待任务"
        @Volatile private var expiresAt = 0L
        @JvmStatic external fun nativeStart(config: String): String
        @JvmStatic external fun nativeStatus(): String
        @JvmStatic external fun nativeStop()

        @JvmStatic fun configure(context: Context, source: String): String {
            val value = JSONObject(source)
            if (value.has("notificationText")) {
                val text = value.getJSONObject("notificationText")
                val preferences = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                for (field in listOf("channel", "title", "connecting")) {
                    val translated = text.getString(field)
                    require(translated.isNotBlank() && translated.length <= 160) { "通知设置无效" }
                    preferences.putString("notification_$field", translated)
                }
                check(preferences.commit()) { "通知设置保存失败" }
                instance?.let { service ->
                    service.getSystemService(NotificationManager::class.java).notify(9401, service.taskNotification())
                }
                return state(context)
            }
            if (value.optBoolean("installUpdate")) { requireNotNull(instance).installUpdate(); return state(context) }
            if (value.optBoolean("disconnect")) {
                context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean("enabled", false).apply()
                context.stopService(Intent(context, WechatRemoteService::class.java)); return state(context)
            }
            check(instance?.current.isNullOrEmpty()) { "请先停止正在执行的任务" }
            check(instance == null) { "请先停止接单后重新登录" }
            val preferences = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val encryptedAccount = preferences.getString("account", null)
            val previous = encryptedAccount?.let { JSONObject(SecretBridge.decrypt(it)) }
            val server = value.optString("server", previous?.optString("server") ?: "").trim().trimEnd('/')
            val uri = java.net.URI(server)
            require(uri.scheme == "https" || BuildConfig.DEBUG && uri.scheme == "http") { "服务地址需要 HTTPS" }
            require(uri.host != null && uri.userInfo == null && uri.fragment == null && uri.query == null) { "服务地址无效" }
            val enteredLicense = value.optString("licenseKey", "").trim()
            val license = enteredLicense.ifEmpty { previous?.optString("licenseKey") ?: "" }
                .uppercase(java.util.Locale.ROOT)
            require(license.isNotEmpty() && license.length <= 64) { "请填写授权码" }
            val software = value.optString("softwareCode", previous?.optString("softwareCode") ?: "qiaomen").trim()
            val candidate = JSONObject().put("server", server).put("licenseKey", license)
                .put("softwareCode", software)
                .put("machine", Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID))
            val verifiedExpiry = verifyAccount(candidate).getLong("endTime")
            check(preferences.edit().putString("account", SecretBridge.encrypt(candidate.toString())).putBoolean("enabled", true).commit())
            expiresAt = verifiedExpiry
            connectionState = "连接中"
            context.startForegroundService(Intent(context, WechatRemoteService::class.java))
            return state(context)
        }

        private fun identity(account: JSONObject) = JSONObject()
            .put("softwareCode", account.getString("softwareCode"))
            .put("licenseKey", account.getString("licenseKey"))
            .put("machine", account.getString("machine"))
            .put("clientApp", "WECHAT_CHANNELS")

        private fun verifyAccount(account: JSONObject): JSONObject {
            val verified = post(account, "/api/v1/licenses/verify", identity(account))
            require(verified.optInt("code") == 200) { verified.optString("msg", "授权验证失败") }
            val data = verified.getJSONObject("data")
            require(data.getString("platformCode") == "WECHAT_CHANNELS") { "授权码平台不匹配" }
            return data
        }

        private fun post(account: JSONObject, path: String, body: JSONObject): JSONObject {
            val request = Request.Builder().url(account.getString("server") + path)
                .post(body.toString().toRequestBody("application/json; charset=utf-8".toMediaType())).build()
            http.newCall(request).execute().use { response ->
                val data = JSONObject(requireNotNull(response.body).string())
                require(response.isSuccessful) { data.optString("msg", data.optString("message", "请求失败")) }
                return data
            }
        }

        @JvmStatic fun restore(context: Context) {
            val preferences = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            if (context.packageName.contains(".wechat_channels") && preferences.contains("account")) {
                preferences.edit().putBoolean("enabled", true).apply()
                context.startForegroundService(Intent(context, WechatRemoteService::class.java))
            }
        }

        @JvmStatic fun restoreIfEnabled(context: Context) {
            if (context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean("enabled", false)) restore(context)
        }

        @JvmStatic fun state(context: Context): String {
            val saved = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("account", null)
            val account = saved?.let { JSONObject(SecretBridge.decrypt(it)) }
            return JSONObject().put("connected", instance?.authorized == true).put("status", connectionState)
                .put("receiving", instance != null)
                .put("batteryWhitelisted", RuntimeBridge.isBatteryOptimizationIgnored())
                .put("configured", account != null)
                .put("release", instance?.clientStatus?.optJSONObject("release"))
                .put("messages", instance?.clientStatus?.optJSONArray("messages") ?: JSONArray())
                .put("message", lastProgress).put("expiresAt", expiresAt).put("commandId", instance?.current ?: "")
                .put("server", account?.optString("server") ?: "https://api.joyfile.net/huoke")
                .put("softwareCode", account?.optString("softwareCode") ?: "qiaomen")
                .put("machine", Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID)).toString()
        }

        private fun atomic(file: File, value: String) {
            val destination = android.util.AtomicFile(file)
            val stream = destination.startWrite()
            try { stream.write(value.toByteArray(Charsets.UTF_8)); destination.finishWrite(stream) }
            catch (error: Exception) { destination.failWrite(stream); throw error }
        }

        private fun digest(value: String) = MessageDigest.getInstance("SHA-256").digest(value.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
    }
}
