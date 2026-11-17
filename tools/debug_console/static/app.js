(() => {
  const state = {
    devices: [],
    selectedSerial: "",
    selectedScope: "/data/local/tmp/wechat-channels-data",
    sourceFilter: "all",
    outcomeFilter: "all",
    searchQuery: "",
    runs: [],
    activeRunRef: null,
    activeRun: null,
    activeSnapshotIndex: -1,
    activeSnapshot: null,
    selectedLineIndex: -1,
    hoveredLineIndex: -1,
    frameImageCache: {},
    activeFrameImg: null,
    taskPollingTimer: null,
  };

  const TASK_TEMPLATES = {
    collect_channels: {
      entry: "recommend",
      count: 1,
      videoFilter: "",
      commentFilter: "",
      commentOptions: {
        unlimited: false,
        visitProfiles: true,
        maxPages: 2,
        maxReplyExpansions: 1,
        maxProfiles: 2,
      },
    },
    collect_comments: {
      maxPages: 3,
      maxReplyExpansions: 1,
      maxProfiles: 2,
    },
    collect_video: {},
    interact: {
      ensureFeed: true,
      likeWork: true,
      collectWork: false,
      commentText: "",
      likeComment: false,
    },
    link: {
      action: "likeVideo",
      link: "https://weixin.qq.com/sph/example",
    },
    mode: {
      mode: "recommend",
      maxItems: 1,
    },
    test: {},
  };

  // --- Helpers ---
  function $(id) {
    return document.getElementById(id);
  }

  function showToast(msg, duration = 3000) {
    const el = $("toastBox");
    el.textContent = msg;
    el.style.display = "block";
    clearTimeout(el._timer);
    el._timer = setTimeout(() => {
      el.style.display = "none";
    }, duration);
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatMs(ms) {
    if (ms === null || ms === undefined) return "-";
    const n = Number(ms);
    if (n < 1000) return `${n}ms`;
    return `${(n / 1000).toFixed(2)}s`;
  }

  function formatTime(epochSecOrMs) {
    if (!epochSecOrMs) return "-";
    const ms = epochSecOrMs < 1e11 ? epochSecOrMs * 1000 : epochSecOrMs;
    const d = new Date(ms);
    return d.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  async function apiGet(url) {
    const res = await fetch(url);
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  async function apiPost(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  // --- Device & Scope Loading ---
  async function loadDevices() {
    try {
      const data = await apiGet("/api/devices");
      state.devices = (data.devices || []).filter((d) => d.serial);
      const sel = $("deviceSelect");
      sel.innerHTML = "";
      if (state.devices.length === 0) {
        sel.innerHTML = `<option value="">未检测到 ADB 设备 (仅本地模式)</option>`;
        state.selectedSerial = "";
      } else {
        for (const d of state.devices) {
          const opt = document.createElement("option");
          opt.value = d.serial;
          const fg = d.foregroundPackage ? ` · ${d.foregroundPackage.split(".").pop()}` : "";
          opt.textContent = `${d.model} (${d.serial}) [${d.physicalWidth || 720}x${d.physicalHeight || 1600}]${fg}`;
          sel.appendChild(opt);
        }
        if (!state.selectedSerial || !state.devices.some((d) => d.serial === state.selectedSerial)) {
          state.selectedSerial = state.devices[0].serial;
        }
        sel.value = state.selectedSerial;
      }
      updateScopeDropdown();
    } catch (err) {
      showToast(`设备检测失败: ${err.message}`);
    }
  }

  function updateScopeDropdown() {
    const scopeSel = $("scopeSelect");
    scopeSel.innerHTML = "";
    const dev = state.devices.find((d) => d.serial === state.selectedSerial);
    const appsBadges = $("installedAppsBadges");
    if (appsBadges) {
      appsBadges.innerHTML = (dev?.installedApps || [])
        .map((app) => {
          const isDbg = app.buildType === "debug";
          const cls = isDbg ? "badge-ocr" : "badge-succeeded";
          const icon = isDbg ? "🐞 Debug" : "🟢 正式版";
          const shortPkg = app.package.replace("top.natsuu.mta.", "");
          return `<span class="badge ${cls}" title="${escapeHtml(app.label)} | 包名: ${escapeHtml(app.package)} | UID: ${escapeHtml(
            app.uid
          )}">${icon} ${escapeHtml(shortPkg)} v${escapeHtml(app.version)}</span>`;
        })
        .join("");
    }
    const scopes = dev?.scopes?.length
      ? dev.scopes
      : [{ path: "/data/local/tmp/wechat-channels-data", label: "默认数据目录 (wechat-channels-data)", runCount: 0 }];
    for (const s of scopes) {
      const opt = document.createElement("option");
      opt.value = s.path;
      opt.textContent = `${s.label} (${s.runCount} runs)`;
      scopeSel.appendChild(opt);
    }
    if (scopes.some((s) => s.path === state.selectedScope)) {
      scopeSel.value = state.selectedScope;
    } else {
      state.selectedScope = scopes[0].path;
      scopeSel.value = state.selectedScope;
    }
  }

  // --- Run List Loading & Filtering ---
  async function loadRuns() {
    const container = $("runListContainer");
    try {
      const params = new URLSearchParams({
        source: state.sourceFilter,
        serial: state.selectedSerial || "",
        scope: state.selectedScope || "/data/local/tmp/wechat-channels-data",
        limit: "70",
      });
      const data = await apiGet(`/api/runs?${params.toString()}`);
      state.runs = data.runs || [];
      renderRunList();
      if (!state.activeRunRef && state.runs.length > 0) {
        // Automatically select the first local or synced run for immediate feedback
        const firstReady = state.runs.find((r) => r.synced) || state.runs[0];
        if (firstReady) {
          selectRun(firstReady);
        }
      }
    } catch (err) {
      container.innerHTML = `<div style="padding:14px;color:var(--red);">加载任务列表失败: ${escapeHtml(err.message)}</div>`;
    }
  }

  function renderRunList() {
    const container = $("runListContainer");
    const q = state.searchQuery.trim().toLowerCase();
    const filtered = state.runs.filter((r) => {
      if (state.outcomeFilter !== "all" && r.outcome !== state.outcomeFilter) return false;
      if (!q) return true;
      const hay = `${r.runId} ${r.taskType} ${r.label || ""} ${r.summaryMessage || ""} ${r.serial || ""}`.toLowerCase();
      return hay.includes(q);
    });

    if (filtered.length === 0) {
      container.innerHTML = `<div style="padding:18px;color:var(--text-muted);text-align:center;">无匹配的任务记录</div>`;
      return;
    }

    container.innerHTML = "";
    for (const r of filtered) {
      const card = document.createElement("div");
      card.className = `run-card ${state.activeRunRef === r.id ? "active" : ""}`;
      const statusBadge =
        r.outcome === "succeeded"
          ? `<span class="badge badge-succeeded">SUCCESS</span>`
          : r.outcome === "failed"
          ? `<span class="badge badge-failed">FAILED</span>`
          : `<span class="badge badge-unknown">${escapeHtml((r.outcome || "RUN").toUpperCase())}</span>`;

      const srcBadge =
        r.sourceType === "adb"
          ? `<span class="badge badge-blue">📱 ${escapeHtml(r.serial?.slice(-4) || "ADB")}</span>`
          : `<span class="badge" style="background:#334155;color:#cbd5e1;">💻 LOCAL</span>`;

      const pullBtn =
        r.sourceType === "adb" && !r.synced
          ? `<button class="btn btn-sm btn-primary btn-pull-run" data-runid="${escapeHtml(r.runId)}" style="padding:1px 6px;font-size:10px;">⬇️ 拉取</button>`
          : "";

      card.innerHTML = `
        <div class="run-card-top">
          <span class="run-id-mono" title="${escapeHtml(r.label || r.runId)}">${escapeHtml(r.runId.slice(0, 18))}</span>
          <div style="display:flex;gap:4px;align-items:center;">
            ${srcBadge}
            ${statusBadge}
          </div>
        </div>
        <div class="run-meta-row">
          <span style="color:#38bdf8;font-weight:600;">${escapeHtml(r.taskType)}</span>
          <span>📷 ${r.snapshotCount || 0} 步 · 🖼️ ${r.frameCount || 0}</span>
        </div>
        <div class="run-meta-row">
          <span>${formatTime(r.mtime)}</span>
          ${pullBtn}
        </div>
        ${
          r.summaryMessage
            ? `<div style="font-size:11px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(
                r.summaryMessage
              )}</div>`
            : ""
        }
      `;

      card.addEventListener("click", (ev) => {
        if (ev.target.closest(".btn-pull-run")) {
          ev.stopPropagation();
          pullAndOpenAdbRun(r);
          return;
        }
        selectRun(r);
      });
      container.appendChild(card);
    }
  }

  async function pullAndOpenAdbRun(runItem) {
    try {
      showToast(`正在从手机 ${runItem.serial} 拉取 ${runItem.runId.slice(0, 8)}...`);
      const res = await apiPost("/api/runs/sync", {
        serial: runItem.serial,
        scopePath: runItem.scopePath,
        runId: runItem.runId,
        includeSqlite: true,
      });
      runItem.synced = true;
      state.activeRunRef = res.runRef;
      state.activeRun = res.run;
      renderRunList();
      onRunLoaded(res.run);
      showToast("拉取成功！");
    } catch (err) {
      showToast(`拉取失败: ${err.message}`, 4500);
    }
  }

  async function selectRun(runItem) {
    try {
      state.activeRunRef = runItem.id;
      renderRunList();
      $("activeRunHeaderBadge").textContent = `正在加载 ${runItem.runId}...`;
      const data = await apiGet(`/api/runs/detail?runRef=${encodeURIComponent(runItem.id)}`);
      state.activeRun = data.run;
      onRunLoaded(data.run);
    } catch (err) {
      showToast(`加载任务详情失败: ${err.message}`, 4500);
    }
  }

  function onRunLoaded(run) {
    $("activeRunHeaderBadge").textContent = `Run: ${run.runId} (${run.relPath})`;
    renderRunOverview(run);
    renderStepTimeline(run);
    renderSqliteAndUploadsTab(run);

    if (run.snapshots && run.snapshots.length > 0) {
      selectSnapshotByIndex(0);
    } else {
      state.activeSnapshotIndex = -1;
      state.activeSnapshot = null;
      clearSnapshotInspector();
    }
  }

  // --- Tab 1: Overview Bar, Waterfall & Timeline ---
  function renderRunOverview(run) {
    const kpiRow = $("runKpiRow");
    const dbCounts = run.sqliteAndUploads?.counts || {};
    const reco = run.recognitionStats || {};
    const outcomeBadge =
      run.outcome === "succeeded"
        ? `<span class="badge badge-succeeded">✅ SUCCEEDED</span>`
        : run.outcome === "failed"
        ? `<span class="badge badge-failed">❌ FAILED</span>`
        : `<span class="badge badge-unknown">${escapeHtml(run.outcome)}</span>`;

    const srcSummary = Object.entries(reco.bySource || {})
      .map(([k, v]) => `${k}:${v}`)
      .join(" / ") || "-";

    kpiRow.innerHTML = `
      <div class="kpi-group">
        ${outcomeBadge}
        <div class="kpi-item"><span class="kpi-label">Run ID:</span><span class="kpi-val">${escapeHtml(run.runId.slice(0, 12))}</span></div>
        <div class="kpi-item"><span class="kpi-label">阶段总耗时:</span><span class="kpi-val" style="color:var(--accent);">${formatMs(run.totalTaskDurationMs)}</span></div>
        <div class="kpi-item"><span class="kpi-label">页面快照数:</span><span class="kpi-val">${reco.totalSnapshots || 0} 步</span></div>
        <div class="kpi-item"><span class="kpi-label">识别累计耗时:</span><span class="kpi-val">${formatMs(reco.totalRecognitionMs || 0)}</span></div>
        <div class="kpi-item"><span class="kpi-label">识别来源分布:</span><span class="kpi-val">${escapeHtml(srcSummary)}</span></div>
        <div class="kpi-item"><span class="kpi-label">控件树空退化次数:</span><span class="kpi-val" style="color:${reco.hierarchyFallbacks > 0 ? "var(--amber)" : "var(--green)"};">${reco.hierarchyFallbacks || 0}</span></div>
      </div>
      <div class="kpi-group">
        <div class="kpi-item"><span class="kpi-label">SQLite 草稿:</span><span class="kpi-val">视频 ${dbCounts.videos || 0} · 评论 ${dbCounts.comments || 0} · 用户 ${dbCounts.users || 0}</span></div>
        <div class="kpi-item"><span class="kpi-label">上报 ACK:</span><span class="kpi-val" style="color:var(--green);">${dbCounts.acknowledgedUploads || 0}</span></div>
      </div>
    `;

    const errBanner = $("runErrorBanner");
    if (run.errorMessage) {
      errBanner.style.display = "flex";
      errBanner.className = "error-banner";
      errBanner.innerHTML = `
        <span>🚨 <strong>任务异常中止原因:</strong> ${escapeHtml(run.errorMessage)}</span>
        <span style="font-size:11px;color:#fecdd3;">定位提示：请查看时间线最后一步快照与页面状态</span>
      `;
    } else {
      errBanner.style.display = "none";
    }

    // Waterfall stages
    const strip = $("stageWaterfallStrip");
    strip.innerHTML = "";
    const stages = run.stages || [];
    const maxDur = Math.max(1000, ...stages.map((s) => s.durationMs || 0));
    for (const st of stages) {
      const pct = Math.min(100, Math.round(((st.durationMs || 0) / maxDur) * 100));
      const isSlow = (st.durationMs || 0) >= 15000;
      const pill = document.createElement("div");
      pill.className = `stage-pill ${st.outcome || ""}`;
      pill.innerHTML = `
        <div class="stage-pill-top">
          <span title="${escapeHtml(st.name)}">${escapeHtml(st.label)}</span>
          <span style="font-family:var(--font-mono);color:${isSlow ? "var(--amber)" : "var(--accent)"};">
            ${st.durationMs ? formatMs(st.durationMs) : st.outcome}
          </span>
        </div>
        <div class="stage-bar-track">
          <div class="stage-bar-fill ${isSlow ? "slow" : ""}" style="width:${st.durationMs ? Math.max(6, pct) : 100}%;"></div>
        </div>
      `;
      strip.appendChild(pill);
    }
  }

  function renderStepTimeline(run) {
    const listEl = $("stepTimelineList");
    const snaps = run.snapshots || [];
    $("stepCountLabel").textContent = String(snaps.length);

    if (snaps.length === 0) {
      // If no snapshots exist, show timingEvents instead
      const events = run.timingEvents || [];
      if (events.length === 0) {
        listEl.innerHTML = `<div style="padding:16px;color:var(--text-muted);text-align:center;">该运行目录无快照文件 (001-*.json)</div>`;
        return;
      }
      listEl.innerHTML = `<div style="padding:8px;color:var(--text-muted);font-size:11.5px;">该运行仅包含 timings.jsonl 事件流（共 ${events.length} 条）：</div>`;
      for (const ev of events) {
        const div = document.createElement("div");
        div.className = "step-item";
        div.innerHTML = `
          <div class="step-item-head">
            <span class="step-seq">#${ev._lineIndex + 1}</span>
            <span class="step-label">${escapeHtml(ev.kind || "event")}</span>
            ${ev.durationMs !== undefined ? `<span class="badge badge-blue">${formatMs(ev.durationMs)}</span>` : ""}
          </div>
          <div style="font-family:var(--font-mono);font-size:10.5px;color:var(--text-muted);word-break:break-all;">
            ${escapeHtml(JSON.stringify(ev))}
          </div>
        `;
        listEl.appendChild(div);
      }
      return;
    }

    listEl.innerHTML = "";
    snaps.forEach((s, idx) => {
      const item = document.createElement("div");
      item.className = `step-item ${state.activeSnapshotIndex === idx ? "active" : ""}`;
      const srcClass = `badge-${s.source || "unknown"}`;
      const hasFallback = (s.attempts || []).some((a) => a.reason);
      const abnBadge =
        s.abnormal && s.abnormal.kind && !["none", "unknown"].includes(s.abnormal.kind)
          ? `<span class="badge badge-failed">⚠️ ${escapeHtml(s.abnormal.reason)}</span>`
          : "";
      const frameBadge = s.hasFrame ? `<span title="含原始画面截图">🖼️</span>` : "";

      item.innerHTML = `
        <div class="step-item-head">
          <span class="step-seq">#${String(s.sequence).padStart(3, "0")}</span>
          <span class="step-label" title="${escapeHtml(s.filename)}">${escapeHtml(s.label)} ${frameBadge}</span>
          <span class="badge ${srcClass}">${escapeHtml(s.source)}</span>
        </div>
        <div class="step-sub">
          <span style="color:#38bdf8;">${escapeHtml(s.pageState)}</span>
          <span>识别 ${formatMs(s.recognitionDurationMs)} ${s.deltaMs ? `(Δ+${formatMs(s.deltaMs)})` : ""}</span>
        </div>
        <div class="step-sub">
          <span>${s.lineCount} 行文本</span>
          ${hasFallback ? `<span style="color:var(--amber);">⚡ 控件树回退</span>` : ""}
          ${abnBadge}
        </div>
      `;
      item.addEventListener("click", () => selectSnapshotByIndex(idx));
      listEl.appendChild(item);
    });
  }

  async function selectSnapshotByIndex(idx, hotReload = false) {
    const snaps = state.activeRun?.snapshots || [];
    if (idx < 0 || idx >= snaps.length) return;
    state.activeSnapshotIndex = idx;
    state.selectedLineIndex = -1;
    state.hoveredLineIndex = -1;

    // Highlight active timeline step
    const items = $("stepTimelineList").querySelectorAll(".step-item");
    items.forEach((el, i) => el.classList.toggle("active", i === idx));
    items[idx]?.scrollIntoView({ block: "nearest" });

    const snapSummary = snaps[idx];
    try {
      const params = new URLSearchParams({
        runRef: state.activeRun.runRef || state.activeRunRef,
        filename: snapSummary.filename,
        hotReload: hotReload ? "1" : "0",
      });
      const authorHint = $("replayAuthorInput")?.value?.trim();
      const kwHint = $("replayKeywordInput")?.value?.trim();
      if (authorHint) params.set("author", authorHint);
      if (kwHint) params.set("keyword", kwHint);

      const data = await apiGet(`/api/runs/snapshot?${params.toString()}`);
      state.activeSnapshot = data.snapshot;

      if (data.snapshot.authorHintUsed && !$("replayAuthorInput").value) {
        $("replayAuthorInput").value = data.snapshot.authorHintUsed;
      }

      // Load frame image if available
      state.activeFrameImg = null;
      if (data.snapshot.hasFrame) {
        const frameUrl = `/api/runs/frame?runRef=${encodeURIComponent(
          state.activeRun.runRef || state.activeRunRef
        )}&filename=${encodeURIComponent(snapSummary.filename)}`;
        loadFrameImage(frameUrl);
      } else {
        drawCanvas();
      }

      renderSnapshotInspector(data.snapshot);
      renderReplayLab(data.snapshot);
    } catch (err) {
      showToast(`加载快照详情失败: ${err.message}`);
    }
  }

  function loadFrameImage(url) {
    if (state.frameImageCache[url]) {
      state.activeFrameImg = state.frameImageCache[url];
      drawCanvas();
      return;
    }
    const img = new Image();
    img.onload = () => {
      state.frameImageCache[url] = img;
      state.activeFrameImg = img;
      drawCanvas();
    };
    img.onerror = () => {
      state.activeFrameImg = null;
      drawCanvas();
    };
    img.src = url;
  }

  function clearSnapshotInspector() {
    $("stepSummaryContent").textContent = "该运行无快照数据";
    $("linesTableBody").innerHTML = "";
    $("linesCountBadge").textContent = "0";
    drawCanvas();
  }

  // --- Center Column: 720xH Visual Screen & Box Canvas ---
  function drawCanvas() {
    const canvas = $("screenCanvas");
    const ctx = canvas.getContext("2d");
    const snap = state.activeSnapshot;
    const zoom = parseFloat($("canvasZoomSelect").value || "0.55");

    const logicalW = snap?.width || 720;
    const logicalH = snap?.height || 1600;
    canvas.width = Math.round(logicalW * zoom);
    canvas.height = Math.round(logicalH * zoom);

    ctx.save();
    ctx.scale(zoom, zoom);

    // 1. Background (Screenshot or Dark Phone Wireframe)
    if (state.activeFrameImg) {
      ctx.drawImage(state.activeFrameImg, 0, 0, logicalW, logicalH);
      // Slight dim overlay so bounding boxes stand out clearly
      ctx.fillStyle = "rgba(9, 13, 20, 0.18)";
      ctx.fillRect(0, 0, logicalW, logicalH);
    } else {
      ctx.fillStyle = "#0b1120";
      ctx.fillRect(0, 0, logicalW, logicalH);
      // Draw subtle coordinate grid every 100px
      ctx.strokeStyle = "rgba(148, 163, 184, 0.08)";
      ctx.lineWidth = 1;
      for (let x = 0; x <= logicalW; x += 120) {
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, logicalH);
        ctx.stroke();
      }
      for (let y = 0; y <= logicalH; y += 200) {
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(logicalW, y);
        ctx.stroke();
        ctx.fillStyle = "rgba(148, 163, 184, 0.28)";
        ctx.font = "14px monospace";
        ctx.fillText(`Y=${y}`, 6, y + 16);
      }
    }

    if (!snap) {
      ctx.fillStyle = "#64748b";
      ctx.font = "22px sans-serif";
      ctx.fillText("请选择左侧步骤查看 720×H 画面与识别框", 130, logicalH / 2);
      ctx.restore();
      return;
    }

    const showLines = $("chkLayerLines").checked;
    const showSemantic = $("chkLayerSemantic").checked;
    const showClicks = $("chkLayerClicks").checked;
    const showRoi = $("chkLayerRoi").checked;
    const showText = $("chkLayerText").checked;

    // 2. ROI Box
    if (showRoi && Array.isArray(snap.roi) && snap.roi.length === 4) {
      const [rx, ry, rw, rh] = snap.roi;
      if (rw > 0 && rh > 0) {
        ctx.save();
        ctx.strokeStyle = "#38bdf8";
        ctx.lineWidth = 3;
        ctx.setLineDash([10, 6]);
        ctx.strokeRect(rx, ry, rw, rh);
        ctx.fillStyle = "rgba(56, 189, 248, 0.06)";
        ctx.fillRect(rx, ry, rw, rh);
        ctx.fillStyle = "#38bdf8";
        ctx.font = "bold 16px monospace";
        ctx.fillText(`ROI [${rx},${ry},${rw},${rh}]`, rx + 8, Math.max(20, ry + 20));
        ctx.restore();
      }
    }

    // 3. Raw Recognition Lines
    const lines = snap.lines || [];
    if (showLines) {
      lines.forEach((line, idx) => {
        if (!Array.isArray(line.box) || line.box.length < 4) return;
        const [bx, by, bw, bh] = line.box;
        const isSelected = idx === state.selectedLineIndex;
        const isHovered = idx === state.hoveredLineIndex;

        let color = "#22c55e"; // hierarchy
        if (line.source === "ocr" || line.source === "ocr-region") color = "#f59e0b";
        else if (line.source === "template") color = "#a855f7";
        if (line.ocrText) color = "#06b6d4"; // hierarchy corrected by OCR

        ctx.save();
        ctx.strokeStyle = isSelected ? "#ffffff" : isHovered ? "#2dd4bf" : color;
        ctx.lineWidth = isSelected ? 4 : isHovered ? 3 : 1.8;
        ctx.fillStyle = isSelected
          ? "rgba(20, 184, 166, 0.28)"
          : isHovered
          ? "rgba(45, 212, 191, 0.22)"
          : !state.activeFrameImg
          ? "rgba(30, 41, 59, 0.55)"
          : "rgba(15, 23, 42, 0.12)";
        ctx.fillRect(bx, by, bw, bh);
        ctx.strokeRect(bx, by, bw, bh);

        if (showText && line.text) {
          ctx.fillStyle = isSelected ? "#ffffff" : color;
          const fontSize = Math.min(20, Math.max(12, Math.round(bh * 0.65)));
          ctx.font = `${fontSize}px -apple-system, sans-serif`;
          const labelTxt = line.text.replace(/\s+/g, " ").slice(0, 28);
          ctx.fillText(labelTxt, bx + 3, by + Math.min(bh - 3, fontSize));
        }
        ctx.restore();
      });
    }

    // 4. Semantic Parser Regions (work_region, comment name/body/like, search cards, field evidence)
    if (showSemantic && snap.replay?.semanticBoxes) {
      for (const sbox of snap.replay.semanticBoxes) {
        if (!Array.isArray(sbox.box) || sbox.box.length < 4) continue;
        const [sx, sy, sw, sh] = sbox.box;
        ctx.save();
        ctx.strokeStyle = sbox.color || "#ec4899";
        ctx.lineWidth = 2.5;
        ctx.setLineDash([5, 3]);
        ctx.strokeRect(sx - 2, sy - 2, sw + 4, sh + 4);
        if (showText && sbox.label) {
          ctx.fillStyle = sbox.color || "#ec4899";
          ctx.font = "bold 13px sans-serif";
          ctx.fillText(sbox.label.slice(0, 26), sx, Math.max(14, sy - 4));
        }
        ctx.restore();
      }
    }

    // 5. Click Targets (Crosshairs, e.g. x+16 safe nickname click)
    if (showClicks && snap.replay?.clickTargets) {
      for (const pt of snap.replay.clickTargets) {
        ctx.save();
        const cx = pt.x;
        const cy = pt.y;
        ctx.strokeStyle = pt.color || "#f43f5e";
        ctx.fillStyle = pt.color || "#f43f5e";
        ctx.lineWidth = 2.5;

        ctx.beginPath();
        ctx.arc(cx, cy, 10, 0, Math.PI * 2);
        ctx.stroke();

        ctx.beginPath();
        ctx.arc(cx, cy, 3.5, 0, Math.PI * 2);
        ctx.fill();

        ctx.beginPath();
        ctx.moveTo(cx - 15, cy);
        ctx.lineTo(cx + 15, cy);
        ctx.moveTo(cx, cy - 15);
        ctx.lineTo(cx, cy + 15);
        ctx.stroke();

        if (showText && pt.label) {
          ctx.font = "bold 13px sans-serif";
          ctx.fillText(`🎯 ${pt.label} (${cx},${cy})`, cx + 12, cy + 4);
        }
        ctx.restore();
      }
    }

    ctx.restore();
  }

  // --- Right Column: Snapshot Summary & Lines Table ---
  function renderSnapshotInspector(snap) {
    const rep = snap.replay?.parsers || {};
    const pState = rep.pageState?.label || rep.pageState?.state || "-";
    const mPage = rep.modePage?.modePage || "-";
    const abn = rep.abnormalState || {};
    const attemptsHtml = (snap.attempts || [])
      .map(
        (a) =>
          `<span class="badge badge-${a.source}">${escapeHtml(a.source)}: ${formatMs(a.durationMs)}${
            a.reason ? ` (${escapeHtml(a.reason)})` : ""
          }</span>`
      )
      .join(" ");

    $("stepSummaryContent").innerHTML = `
      <div class="kv-grid">
        <span class="kv-key">快照文件:</span><span class="kv-val">${escapeHtml(snap.filename)}</span>
        <span class="kv-key">画面尺寸:</span><span class="kv-val">${snap.width} × ${snap.height}</span>
        <span class="kv-key">最终来源:</span><span class="kv-val"><span class="badge badge-${snap.source}">${escapeHtml(snap.source)}</span></span>
        <span class="kv-key">识别尝试:</span><span class="kv-val">${attemptsHtml || "-"}</span>
        <span class="kv-key">页面状态:</span><span class="kv-val" style="color:var(--accent);font-weight:600;">${escapeHtml(pState)}</span>
        <span class="kv-key">模式页判定:</span><span class="kv-val">${escapeHtml(mPage)} (Tab: ${escapeHtml(rep.modePage?.activeTopTab || "-")})</span>
        <span class="kv-key">异常状态:</span><span class="kv-val" style="color:${
          abn.kind && !["none", "unknown"].includes(abn.kind) ? "var(--red)" : "var(--green)"
        };">${escapeHtml(abn.kind || "none")} (${escapeHtml(abn.reason || "normal")})</span>
      </div>
    `;

    renderLinesTable();
  }

  function renderLinesTable() {
    const snap = state.activeSnapshot;
    const tbody = $("linesTableBody");
    const lines = snap?.lines || [];
    $("linesCountBadge").textContent = String(lines.length);

    const filterQ = ($("lineFilterInput").value || "").trim().toLowerCase();
    tbody.innerHTML = "";

    lines.forEach((line, idx) => {
      const txt = line.text || "";
      const resId = line.selector?.resource || "";
      if (filterQ && !`${txt} ${resId} ${line.source}`.toLowerCase().includes(filterQ)) {
        return;
      }
      const tr = document.createElement("tr");
      if (idx === state.selectedLineIndex) tr.classList.add("selected");
      const boxStr = Array.isArray(line.box) ? `[${line.box.join(",")}]` : "-";
      tr.innerHTML = `
        <td style="font-family:var(--font-mono);color:var(--text-muted);">${idx}</td>
        <td>
          <div style="font-weight:500;">${escapeHtml(txt || "(空容器/图标)")}</div>
          ${line.ocrText ? `<div style="font-size:10px;color:#06b6d4;">OCR修正: ${escapeHtml(line.ocrText)}</div>` : ""}
          ${resId ? `<div style="font-size:10px;color:var(--text-muted);font-family:var(--font-mono);">${escapeHtml(resId)}</div>` : ""}
        </td>
        <td style="font-family:var(--font-mono);font-size:10.5px;">${escapeHtml(boxStr)}</td>
        <td><span class="badge badge-${line.source}">${escapeHtml(line.source)}</span></td>
      `;
      tr.addEventListener("mouseenter", () => {
        state.hoveredLineIndex = idx;
        drawCanvas();
      });
      tr.addEventListener("mouseleave", () => {
        state.hoveredLineIndex = -1;
        drawCanvas();
      });
      tr.addEventListener("click", () => {
        selectLineBox(idx);
      });
      tbody.appendChild(tr);
    });
  }

  function selectLineBox(idx) {
    const snap = state.activeSnapshot;
    if (!snap || !snap.lines || idx < 0 || idx >= snap.lines.length) return;
    state.selectedLineIndex = idx;
    const line = snap.lines[idx];
    const [x, y, w, h] = line.box || [0, 0, 0, 0];
    const centerPt = `[${x + Math.floor(w / 2)}, ${y + Math.floor(h / 2)}]`;
    const leftNickPt = `[${x + Math.floor(Math.min(w, 32) / 2)}, ${y + Math.floor(h / 2)}]`;

    $("btnCopyBoxAssert").style.display = "inline-flex";
    $("btnCopyBoxAssert").onclick = () => {
      const snippet = JSON.stringify({ text: line.text, box: line.box, source: line.source });
      navigator.clipboard.writeText(snippet);
      showToast("已复制该识别框 JSON 到剪贴板！");
    };

    $("selectedBoxContent").innerHTML = `
      <div class="kv-grid">
        <span class="kv-key">序号 / 来源:</span><span class="kv-val">#${idx} (${escapeHtml(line.source)}, score=${line.score ?? 1})</span>
        <span class="kv-key">文本 (text):</span><span class="kv-val" style="color:#fff;font-weight:600;">${escapeHtml(line.text)}</span>
        <span class="kv-key">框 [x,y,w,h]:</span><span class="kv-val">[${x}, ${y}, ${w}, ${h}] (右下: ${x + w}, ${y + h})</span>
        <span class="kv-key">中心点 / 左端:</span><span class="kv-val">中心 ${centerPt} · 昵称安全左端 ${leftNickPt}</span>
        ${
          line.selector
            ? `<span class="kv-key">控件 Selector:</span><span class="kv-val">${escapeHtml(
                `${line.selector.class || ""} (${line.selector.resource || line.selector.attribute || ""})`
              )}</span>`
            : ""
        }
      </div>
    `;
    renderLinesTable();
    drawCanvas();
  }

  // --- Tab 2: Parser Replay & Hot-Reload Lab ---
  function renderReplayLab(snap) {
    const p = snap?.replay?.parsers || {};
    $("replayPageStatePre").textContent = JSON.stringify(
      {
        replayDurationMs: snap?.replay?.durationMs,
        hotReloaded: snap?.replay?.hotReloaded,
        pageState: p.pageState,
        modePage: p.modePage,
        abnormalState: p.abnormalState,
        feedAuthor: p.feedAuthor,
        commentControl: p.commentControl,
        workRegion: p.workRegion,
      },
      null,
      2
    );
    $("replayVideoPre").textContent = JSON.stringify(p.parseVideo || {}, null, 2);
    $("replayAuthorPre").textContent = JSON.stringify(
      {
        authorProfile: p.parseAuthorProfile,
        basicInfo: p.parseBasicInfo,
      },
      null,
      2
    );
    $("replayCommentsPre").textContent = JSON.stringify(p.parseCommentDrawer || {}, null, 2);
    $("replaySearchCardsPre").textContent = JSON.stringify(p.parseSearchCards || {}, null, 2);
    $("customLinesTextarea").value = JSON.stringify(snap?.lines || [], null, 2);
  }

  async function runHotReloadReplay() {
    if (!state.activeSnapshot || state.activeSnapshotIndex < 0) {
      showToast("请先选择一个快照步骤");
      return;
    }
    await selectSnapshotByIndex(state.activeSnapshotIndex, true);
    showToast("🔥 已热重载本地 Python Collector 代码并完成重跑解析！");
  }

  async function runCustomLinesReplay() {
    try {
      const raw = $("customLinesTextarea").value;
      const customLines = JSON.parse(raw);
      if (!Array.isArray(customLines)) throw new Error("JSON 必须是 lines 数组");
      const res = await apiPost("/api/runs/replay", {
        lines: customLines,
        height: state.activeSnapshot?.height || 1605,
        width: state.activeSnapshot?.width || 720,
        label: state.activeSnapshot?.label || "custom-sandbox",
        authorHint: $("replayAuthorInput").value.trim() || null,
        keywordHint: $("replayKeywordInput").value.trim() || null,
        hotReload: true,
      });
      if (state.activeSnapshot) {
        state.activeSnapshot.lines = customLines;
        state.activeSnapshot.replay = res.replay;
        renderSnapshotInspector(state.activeSnapshot);
        renderReplayLab(state.activeSnapshot);
        drawCanvas();
      }
      showToast(`沙盒重跑完成 (${res.replay.durationMs}ms)！画布与右侧面板已同步更新。`);
    } catch (err) {
      showToast(`自定义 Lines 解析失败: ${err.message}`, 4500);
    }
  }

  function exportSnapshotUnitTest() {
    const snap = state.activeSnapshot;
    if (!snap) {
      showToast("请先选择一个快照");
      return;
    }
    const compactLines = (snap.lines || []).map((l) => ({
      text: l.text,
      box: l.box,
      source: l.source || "ocr",
      ...(l.selector ? { selector: l.selector } : {}),
    }));
    const pState = snap.replay?.parsers?.pageState?.state || "unknown";
    const code = `def test_snapshot_${snap.label.replace(/[^a-zA-Z0-9_]/g, "_")}(self):
    lines = ${JSON.stringify(compactLines, null, 4)}
    height = ${snap.height || 1605}
    self.assertEqual(classify_page_state(lines, height), "${pState}")
`;
    navigator.clipboard.writeText(code);
    showToast("📋 已复制 Python 单元测试函数到剪贴板！");
  }

  // --- Tab 3: SQLite & Upload Batches Inspector ---
  function renderSqliteAndUploadsTab(run) {
    const sq = run.sqliteAndUploads || {};
    const banner = $("sqliteAcceptanceBanner");
    const treeEl = $("sqliteHierarchyContainer");
    const batchesEl = $("uploadBatchesContainer");
    const payloadsEl = $("payloadFilesContainer");

    if (!sq.hasDatabase) {
      banner.innerHTML = `<span style="color:var(--text-muted);">未在该运行目录或上级目录检测到 <code>videos.sqlite3</code> 数据库文件。</span>`;
      treeEl.innerHTML = `<div style="color:var(--text-muted);">无本地 SQLite 记录</div>`;
    } else if (sq.error) {
      banner.innerHTML = `<span style="color:var(--red);">读取 SQLite 失败: ${escapeHtml(sq.error)}</span>`;
      treeEl.innerHTML = "";
    } else {
      const acc = sq.acceptance || {};
      const passBadge = acc.passed
        ? `<span class="badge badge-succeeded">✅ acceptance.sql &amp; 外键完整性校验通过</span>`
        : `<span class="badge badge-failed">❌ 数据完整性校验发现异常</span>`;
      banner.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
          <div>
            <strong>🗄️ SQLite 数据库:</strong> <code>${escapeHtml(sq.dbPath)}</code>
            (Device ID: <code>${escapeHtml(sq.deviceId || "-")}</code>)
          </div>
          ${passBadge}
        </div>
        <div style="margin-top:6px;font-size:11.5px;color:var(--text-muted);">
          VIDEO 草稿: <b>${sq.counts?.videos || 0}</b> ·
          COMMENT 草稿: <b>${sq.counts?.comments || 0}</b> ·
          USER 草稿: <b>${sq.counts?.users || 0}</b> ·
          待发送队列(Pending): <b>${sq.counts?.pendingUploads || 0}</b> ·
          已确认(ACK): <b style="color:var(--green);">${sq.counts?.acknowledgedUploads || 0}</b>
          ${
            !acc.passed
              ? `<div style="color:var(--red);margin-top:4px;">异常项: 重复VideoKey=${acc.duplicateVideoKeys?.length}, 非法Video=${acc.invalidVideoDraftIds?.length}, 孤儿Comment=${acc.orphanCommentIds?.length}, 孤儿User=${acc.orphanUserIds?.length}</div>`
              : ""
          }
        </div>
      `;

      const tree = sq.hierarchyTree || [];
      if (tree.length === 0) {
        treeEl.innerHTML = `<div style="color:var(--text-muted);">video_drafts 表为空</div>`;
      } else {
        treeEl.innerHTML = "";
        for (const v of tree) {
          const vp = v.event?.payload || {};
          const vCard = document.createElement("div");
          vCard.className = "entity-card";
          const upBadge =
            v.uploadState === "acknowledged"
              ? `<span class="badge badge-succeeded">ACK 已确认</span>`
              : v.uploadState === "pending"
              ? `<span class="badge badge-warning">PENDING 待上传</span>`
              : `<span class="badge">LOCAL_ONLY</span>`;

          const commentsHtml = (v.comments || [])
            .map((c) => {
              const cp = c.event?.payload || {};
              const usersHtml = (c.users || [])
                .map((u) => {
                  const up = u.event?.payload || {};
                  return `
                    <div class="user-branch">
                      <div><strong>👤 评论者主页 (USER):</strong> ${escapeHtml(up.nickname || "-")} · 视频号ID: <code>${escapeHtml(
                    up.wechat_channels_id || up.wechatChannelsId || "未公开"
                  )}</code></div>
                      <div style="font-size:11px;color:var(--text-muted);">
                        地区: ${escapeHtml(up.location || "-")} · IP: ${escapeHtml(up.ipLocation || "-")} · 简介: ${escapeHtml(
                    up.signature || "-"
                  )}
                      </div>
                    </div>
                  `;
                })
                .join("");

              return `
                <div class="comment-branch">
                  <div style="display:flex;justify-content:space-between;gap:6px;">
                    <span>💬 <strong>${escapeHtml(cp.nickname)}</strong> ${
                cp.replyToNickname ? `回复 <b>@${escapeHtml(cp.replyToNickname)}</b>` : ""
              }: ${escapeHtml(cp.content)}</span>
                    <span class="badge badge-${c.uploadState === "acknowledged" ? "succeeded" : "unknown"}">${escapeHtml(
                c.uploadState
              )}</span>
                  </div>
                  <div style="font-size:11px;color:var(--text-muted);">
                    时间: ${escapeHtml(cp.commentTime)} · IP: ${escapeHtml(cp.ipLocation || "-")} · 赞: ${
                cp.likeCount ?? 0
              } · 作者视频号ID: <code>${escapeHtml(cp.authorDouyinId || cp.authorWechatChannelsId || "-")}</code>
                  </div>
                  ${usersHtml}
                </div>
              `;
            })
            .join("");

          vCard.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
              <strong>🎬 @${escapeHtml(vp.authorNickname || "-")}</strong>
              <div style="display:flex;gap:6px;align-items:center;">
                <span class="badge badge-blue">${escapeHtml(v.event?.source_mode || "-")}</span>
                ${upBadge}
              </div>
            </div>
            <div style="margin:6px 0;font-size:12px;">${escapeHtml(vp.description || "")}</div>
            <div style="font-size:11px;color:var(--text-muted);display:flex;flex-wrap:wrap;gap:10px;">
              <span>视频号ID: <code>${escapeHtml(vp.author_wechat_channels_id || vp.authorWechatChannelsId || "-")}</code></span>
              <span>短链: <code>${escapeHtml(vp.videoUrl || "-")}</code></span>
              <span>发布: ${escapeHtml(vp.publishTimeText || "-")} (${escapeHtml(vp.publishLocation || "-")})</span>
              <span>归位核验: <b>${String(vp.returnVerified)}</b></span>
              <span>缺失字段: <code>${escapeHtml((vp.missingFields || []).join(",") || "无")}</code></span>
            </div>
            <div style="margin-top:8px;font-size:11.5px;color:var(--accent);">
              关联评论 (${v.commentCount}) · 关联评论者主页 (${v.userCount})
            </div>
            ${commentsHtml}
          `;
          treeEl.appendChild(vCard);
        }
      }
    }

    // Upload batches
    const batches = sq.uploadBatches || [];
    if (batches.length === 0) {
      batchesEl.innerHTML = `<div class="inspector-card" style="color:var(--text-muted);">无 uploads/*-request.json 批次文件</div>`;
    } else {
      batchesEl.innerHTML = batches
        .map(
          (b) => `
          <div class="inspector-card">
            <h4>
              <span>Batch <code>${escapeHtml(b.batchId.slice(0, 12))}</code> (${escapeHtml(b.eventTypes.join(", "))})</span>
              <span class="badge ${b.uploaded ? "badge-succeeded" : "badge-warning"}">
                Sent ${b.sentCount} / ACK ${b.acknowledgedCount}
              </span>
            </h4>
            <details>
              <summary style="cursor:pointer;font-size:11.5px;color:var(--text-muted);">展开查看上报 Payload 与 ACK 回执</summary>
              <pre class="code-block" style="margin-top:6px;">${escapeHtml(
                JSON.stringify({ events: b.events, ack: b.ack }, null, 2)
              )}</pre>
            </details>
          </div>
        `
        )
        .join("");
    }

    // Payload files & ledgers
    const allFiles = { ...(run.payloadFiles || {}), ...(sq.auxLedgers || {}) };
    const fnames = Object.keys(allFiles);
    if (fnames.length === 0) {
      payloadsEl.innerHTML = `<div class="inspector-card" style="color:var(--text-muted);">无输出 JSON 文件</div>`;
    } else {
      payloadsEl.innerHTML = fnames
        .map(
          (fn) => `
          <div class="inspector-card">
            <h4>📄 ${escapeHtml(fn)}</h4>
            <pre class="code-block">${escapeHtml(JSON.stringify(allFiles[fn], null, 2))}</pre>
          </div>
        `
        )
        .join("");
    }
  }

  // --- Live Probe, Code Push & Task Runner (Tab 4) ---
  async function handleLiveProbe() {
    if (!state.selectedSerial) {
      showToast("请先连接并选择一台 ADB 手机设备");
      return;
    }
    const btn = $("btnLiveProbe");
    btn.disabled = true;
    btn.textContent = "⏳ 正在抓屏与导出控件树...";
    try {
      const res = await apiPost("/api/device/probe", {
        serial: state.selectedSerial,
        label: "live-probe",
      });
      state.activeRunRef = res.probe.runRef;
      state.activeRun = res.run;
      onRunLoaded(res.run);
      // Select the newly added last snapshot
      const lastIdx = (res.run.snapshots || []).length - 1;
      if (lastIdx >= 0) {
        await selectSnapshotByIndex(lastIdx);
      }
      // Switch to Tab 1
      switchTab("tab-workflow");
      showToast(`📸 已抓取手机 ${state.selectedSerial} 实时画面 (${res.probe.lineCount} 行控件)！`);
      loadRuns();
    } catch (err) {
      showToast(`实时探测失败: ${err.message}`, 4500);
    } finally {
      btn.disabled = false;
      btn.textContent = "📸 抓取手机当前画面与控件";
    }
  }

  async function handlePushCode() {
    if (!state.selectedSerial) {
      showToast("请先选择一台 ADB 手机设备");
      return;
    }
    const btn = $("btnPushCode");
    btn.disabled = true;
    try {
      showToast(`正在推送 collector/*.py 到 ${state.selectedSerial}...`);
      const res = await apiPost("/api/device/push-code", { serial: state.selectedSerial });
      const appCount = (res.result.appRuntimesSynced || []).length;
      showToast(
        `✅ 已推送 ${res.result.pushedCount} 个文件到 CLI 目录及 ${appCount} 个 App 运行时 (含正式版与Debug版)`,
        4000
      );
    } catch (err) {
      showToast(`推送失败: ${err.message}`, 4500);
    } finally {
      btn.disabled = false;
    }
  }

  async function startDeviceTask() {
    if (!state.selectedSerial) {
      showToast("请先连接并选择一台 ADB 手机设备");
      return;
    }
    let cfg = {};
    try {
      const txt = $("runnerConfigTextarea").value.trim();
      cfg = txt ? JSON.parse(txt) : {};
    } catch (err) {
      showToast(`任务 JSON 配置格式有误: ${err.message}`);
      return;
    }
    try {
      const res = await apiPost("/api/device/tasks/start", {
        serial: state.selectedSerial,
        taskMode: $("runnerTaskMode").value,
        config: cfg,
        dumpFrames: $("runnerDumpFrames").checked,
        syncCodeFirst: $("runnerSyncCode").checked,
        noLaunch: $("runnerNoLaunch").checked,
      });
      showToast(`🚀 任务 #${res.task.taskId} 已在手机 ${state.selectedSerial} 启动！`);
      loadDeviceTasks();
      startTaskPolling();
    } catch (err) {
      showToast(`启动任务失败: ${err.message}`, 4500);
    }
  }

  async function loadDeviceTasks() {
    try {
      const data = await apiGet("/api/device/tasks");
      const tasks = data.tasks || [];
      const container = $("deviceTasksContainer");
      if (tasks.length === 0) {
        container.innerHTML = `<div style="color:var(--text-muted);">暂无通过调试台启动的任务进程。</div>`;
        return;
      }
      container.innerHTML = "";
      for (const t of tasks) {
        const card = document.createElement("div");
        card.className = "entity-card";
        const badgeClass =
          t.status === "succeeded"
            ? "badge-succeeded"
            : t.status === "failed"
            ? "badge-failed"
            : "badge-warning";

        card.innerHTML = `
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <strong>#${escapeHtml(t.taskId)} · ${escapeHtml(t.taskMode)} (${escapeHtml(t.serial)})</strong>
            <div style="display:flex;gap:6px;align-items:center;">
              <span class="badge ${badgeClass}">${escapeHtml(t.status.toUpperCase())}</span>
              ${
                t.status === "running"
                  ? `<button class="btn btn-sm btn-danger btn-stop-task" data-tid="${escapeHtml(t.taskId)}">⏹ 停止</button>`
                  : ""
              }
              ${
                t.detectedRunId
                  ? `<button class="btn btn-sm btn-primary btn-open-detected" data-serial="${escapeHtml(
                      t.serial
                    )}" data-runid="${escapeHtml(t.detectedRunId)}">🔍 拉取并调试该 Run</button>`
                  : ""
              }
            </div>
          </div>
          <div style="font-size:11px;color:var(--text-muted);margin:4px 0;font-family:var(--font-mono);">
            $ ${escapeHtml(t.command)}
          </div>
          <pre class="code-block" style="max-height:220px;margin-top:6px;">${escapeHtml(
            (t.logs || []).join("\n") || "等待输出..."
          )}</pre>
        `;
        card.querySelector(".btn-stop-task")?.addEventListener("click", async () => {
          await apiPost("/api/device/tasks/stop", { taskId: t.taskId });
          loadDeviceTasks();
        });
        card.querySelector(".btn-open-detected")?.addEventListener("click", async () => {
          await pullAndOpenAdbRun({
            serial: t.serial,
            scopePath: "/data/local/tmp/wechat-channels-dev/data",
            runId: t.detectedRunId,
          });
          switchTab("tab-workflow");
        });
        container.appendChild(card);
      }

      if (!tasks.some((t) => t.status === "running") && state.taskPollingTimer) {
        clearInterval(state.taskPollingTimer);
        state.taskPollingTimer = null;
      }
    } catch (err) {
      // ignore transient poll errors
    }
  }

  function startTaskPolling() {
    if (state.taskPollingTimer) clearInterval(state.taskPollingTimer);
    state.taskPollingTimer = setInterval(loadDeviceTasks, 2000);
  }

  // --- Navigation & Event Listeners ---
  function switchTab(tabId) {
    document.querySelectorAll("#mainNavTabs .nav-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tabId);
    });
    document.querySelectorAll(".tab-pane").forEach((pane) => {
      pane.classList.toggle("active", pane.id === tabId);
    });
    if (tabId === "tab-runner") {
      loadDeviceTasks();
    }
  }

  function bindEvents() {
    // Main nav tabs
    document.querySelectorAll("#mainNavTabs .nav-tab").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });

    // Source filter tabs
    document.querySelectorAll("#sourceFilterTabs .seg-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#sourceFilterTabs .seg-tab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.sourceFilter = btn.dataset.source;
        loadRuns();
      });
    });

    $("deviceSelect").addEventListener("change", (e) => {
      state.selectedSerial = e.target.value;
      updateScopeDropdown();
      loadRuns();
    });

    $("scopeSelect").addEventListener("change", (e) => {
      state.selectedScope = e.target.value;
      loadRuns();
    });

    $("runSearchInput").addEventListener("input", (e) => {
      state.searchQuery = e.target.value;
      renderRunList();
    });

    $("outcomeFilterSelect").addEventListener("change", (e) => {
      state.outcomeFilter = e.target.value;
      renderRunList();
    });

    $("btnRefreshAll").addEventListener("click", async () => {
      await loadDevices();
      await loadRuns();
      showToast("已刷新设备与任务列表");
    });

    $("btnLiveProbe").addEventListener("click", handleLiveProbe);
    $("btnPushCode").addEventListener("click", handlePushCode);

    // Step navigation
    $("btnPrevStep").addEventListener("click", () => {
      if (state.activeSnapshotIndex > 0) selectSnapshotByIndex(state.activeSnapshotIndex - 1);
    });
    $("btnNextStep").addEventListener("click", () => {
      const len = state.activeRun?.snapshots?.length || 0;
      if (state.activeSnapshotIndex + 1 < len) selectSnapshotByIndex(state.activeSnapshotIndex + 1);
    });

    window.addEventListener("keydown", (e) => {
      if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
      if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
        if (state.activeSnapshotIndex > 0) {
          e.preventDefault();
          selectSnapshotByIndex(state.activeSnapshotIndex - 1);
        }
      } else if (e.key === "ArrowDown" || e.key === "ArrowRight") {
        const len = state.activeRun?.snapshots?.length || 0;
        if (state.activeSnapshotIndex + 1 < len) {
          e.preventDefault();
          selectSnapshotByIndex(state.activeSnapshotIndex + 1);
        }
      }
    });

    // Canvas layer checkboxes & zoom
    for (const id of ["chkLayerLines", "chkLayerSemantic", "chkLayerClicks", "chkLayerRoi", "chkLayerText", "canvasZoomSelect"]) {
      $(id).addEventListener("change", drawCanvas);
    }

    // Canvas mouse hover & click to inspect bounding boxes
    const canvas = $("screenCanvas");
    canvas.addEventListener("mousemove", (e) => {
      const rect = canvas.getBoundingClientRect();
      const zoom = parseFloat($("canvasZoomSelect").value || "0.55");
      const lx = Math.round((e.clientX - rect.left) / zoom);
      const ly = Math.round((e.clientY - rect.top) / zoom);
      $("cursorCoordLabel").textContent = `X: ${lx} , Y: ${ly}`;

      const lines = state.activeSnapshot?.lines || [];
      let foundIdx = -1;
      // Prefer smallest matching box under cursor
      let bestArea = Infinity;
      lines.forEach((line, idx) => {
        if (!Array.isArray(line.box) || line.box.length < 4) return;
        const [bx, by, bw, bh] = line.box;
        if (lx >= bx && lx <= bx + bw && ly >= by && ly <= by + bh) {
          const area = bw * bh;
          if (area < bestArea) {
            bestArea = area;
            foundIdx = idx;
          }
        }
      });
      if (foundIdx !== state.hoveredLineIndex) {
        state.hoveredLineIndex = foundIdx;
        drawCanvas();
      }
    });

    canvas.addEventListener("click", () => {
      if (state.hoveredLineIndex >= 0) {
        selectLineBox(state.hoveredLineIndex);
      }
    });

    $("lineFilterInput").addEventListener("input", renderLinesTable);
    $("btnQuickHotReplay").addEventListener("click", runHotReloadReplay);
    $("btnRunHotReplay").addEventListener("click", runHotReloadReplay);
    $("btnReplayCustomLines").addEventListener("click", runCustomLinesReplay);
    $("btnExportUnitTest").addEventListener("click", exportSnapshotUnitTest);

    // Task runner mode template switching
    $("runnerTaskMode").addEventListener("change", (e) => {
      const tpl = TASK_TEMPLATES[e.target.value] ?? {};
      $("runnerConfigTextarea").value = JSON.stringify(tpl, null, 2);
    });
    $("btnStartDeviceTask").addEventListener("click", startDeviceTask);
    $("btnRefreshTasks").addEventListener("click", loadDeviceTasks);
  }

  async function init() {
    bindEvents();
    await loadDevices();
    await loadRuns();
  }

  init();
})();
