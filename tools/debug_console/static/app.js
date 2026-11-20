(() => {
  const ORIGIN_OPTIONS = [
    {
      value: "all",
      label: "全部来源",
      explain:
        "<b>来源说明：</b>汇总这台手机全部 3 类任务，按最新执行时间倒序。<br>" +
        "• <b>📡 后台下发</b>：服务端 / 管理后台远程下发给 App 自动执行<br>" +
        "• <b>👆 App手动</b>：在手机 App 界面手动点「开始」执行<br>" +
        "• <b>🛠️ 开发调试</b>：电脑命令行或右上角「下发任务」经 USB 直接运行",
    },
    {
      value: "remote",
      label: "📡 后台下发 · 服务端远程",
      explain:
        "<b>📡 后台下发：</b>由服务端 / 管理后台远程下发给手机 App 自动执行的任务（已自动合并手机上所有授权和环境的远端目录，不再区分哈希目录）。",
    },
    {
      value: "manual",
      label: "👆 App手动 · 手机界面点击",
      explain:
        "<b>👆 App手动：</b>直接在手机 App 界面里手动点击「开始」运行的任务（手机目录：wechat-channels-data）。",
    },
    {
      value: "cli",
      label: "🛠️ 开发调试 · 命令行/调试台",
      explain:
        "<b>🛠️ 开发调试：</b>开发时通过电脑命令行或本页右上角「🛠️ 下发任务」，经 USB 直接在手机上跑的调试任务（手机目录：wechat-channels-dev/data）。",
    },
  ];

  const state = {
    devices: [],
    selectedSerial: "",
    selectedScope: "all",
    sourceFilter: "adb",
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
    showRealFrame: true,
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
        maxComments: 20,
      },
    },
    collect_comments: { unlimited: false, maxComments: 20 },
    collect_video: {},
    interact: { ensureFeed: true, likeWork: true },
    inspect_messages: { ensureFeed: true, inspectMessages: { like: true, maxMessages: 1, replyText: "" } },
    link_like_video: { action: "likeVideo", link: "https://weixin.qq.com/sph/example" },
    link_follow_author: { action: "followAuthor", link: "https://weixin.qq.com/sph/example" },
    link_message_author: { action: "messageAuthor", link: "https://weixin.qq.com/sph/example", text: "你好" },
    link_comment_video: { action: "commentVideo", link: "https://weixin.qq.com/sph/example", text: "拍得真棒！" },
    link_like_comment: { action: "likeComment", link: "https://weixin.qq.com/sph/example", nickname: "目标评论者昵称" },
    link_reply_comment: { action: "replyComment", link: "https://weixin.qq.com/sph/example", nickname: "目标评论者昵称", text: "赞同！" },
    link_message_commenter: { action: "messageCommenter", link: "https://weixin.qq.com/sph/example", nickname: "目标评论者昵称", text: "你好" },
    link: { action: "likeVideo", link: "https://weixin.qq.com/sph/example" },
    test: {},
  };

  function $(id) {
    return document.getElementById(id);
  }

  function showToast(msg, duration = 3200) {
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
    if (n < 1000) return `${n}毫秒`;
    return `${(n / 1000).toFixed(1)}秒`;
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

  // --- 1. Device & Scope Loading ---
  async function loadDevices() {
    try {
      const data = await apiGet("/api/devices");
      state.devices = (data.devices || []).filter((d) => d.serial);
      const sel = $("deviceSelect");
      sel.innerHTML = "";
      if (state.devices.length === 0) {
        sel.innerHTML = `<option value="">未连接手机 (仅查看本地缓存)</option>`;
        state.selectedSerial = "";
      } else {
        for (const d of state.devices) {
          const opt = document.createElement("option");
          opt.value = d.serial;
          opt.textContent = `${d.model} (${d.serial})`;
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

  function syncSourceTabsUI() {
    document.querySelectorAll("#sourceFilterTabs .seg-tab").forEach((b) => {
      b.classList.toggle("active", b.dataset.source === state.sourceFilter);
    });
    const scopeSel = $("scopeSelect");
    if (scopeSel) {
      scopeSel.style.display = state.sourceFilter === "adb" ? "" : "none";
    }
    updateOriginExplain();
  }

  function updateOriginExplain() {
    const box = $("originExplainBox");
    if (!box) return;
    if (state.sourceFilter === "local") {
      box.innerHTML =
        "<b>💻 本地离线：</b>展示电脑项目 <code>data/</code> 下已拉取缓存或留档的任务，已按任务 ID 去重并按<b>真实执行时间</b>倒序。";
      return;
    }
    const found = ORIGIN_OPTIONS.find((o) => o.value === state.selectedScope) || ORIGIN_OPTIONS[0];
    box.innerHTML = found.explain;
  }

  function updateScopeDropdown() {
    const scopeSel = $("scopeSelect");
    scopeSel.innerHTML = "";
    const dev = state.devices.find((d) => d.serial === state.selectedSerial);
    const appsBadges = $("installedAppsBadges");
    if (appsBadges) {
      appsBadges.innerHTML = (dev?.installedApps || [])
        .filter((app) => app.package.includes("wechat"))
        .map((app) => {
          const isDbg = app.buildType === "debug";
          const cls = isDbg ? "badge-ocr" : "badge-succeeded";
          const icon = isDbg ? "🐞 Debug版" : "🟢 正式版";
          return `<span class="badge ${cls}" title="${escapeHtml(app.label)} (包名: ${escapeHtml(app.package)})">${icon} v${escapeHtml(
            app.version
          )}</span>`;
        })
        .join("");
    }
    const counts = dev?.originCounts || { all: 0, remote: 0, manual: 0, cli: 0 };
    for (const item of ORIGIN_OPTIONS) {
      const opt = document.createElement("option");
      opt.value = item.value;
      const cnt = counts[item.value] ?? 0;
      opt.textContent = `${item.label} (${cnt}条)`;
      scopeSel.appendChild(opt);
    }
    if (!ORIGIN_OPTIONS.some((o) => o.value === state.selectedScope)) {
      state.selectedScope = "all";
    }
    scopeSel.value = state.selectedScope;
    syncSourceTabsUI();
  }

  // --- 2. Left Panel: Task Run List ---
  async function loadRuns() {
    const container = $("runListContainer");
    if (state.sourceFilter === "adb" && !state.selectedSerial) {
      state.runs = [];
      $("runCountBadge").textContent = "0 条";
      container.innerHTML = `<div style="padding:24px;color:var(--text-muted);text-align:center;">当前未连接手机，请连接设备后点击刷新，或切换到「💻 本地离线」查看已保存记录。</div>`;
      return;
    }
    try {
      const params = new URLSearchParams({
        source: state.sourceFilter,
        serial: state.selectedSerial || "",
        scope: state.selectedScope || "all",
        limit: state.sourceFilter === "adb" ? "40" : "60",
      });
      const data = await apiGet(`/api/runs?${params.toString()}`);
      state.runs = data.runs || [];
      renderRunList();
      if ((!state.activeRunRef || !state.runs.some((r) => r.id === state.activeRunRef)) && state.runs.length > 0) {
        selectRun(state.runs[0]);
      }
    } catch (err) {
      container.innerHTML = `<div style="padding:16px;color:var(--red);">加载任务列表失败: ${escapeHtml(err.message)}</div>`;
    }
  }

  function updateActiveRunCard(activeId) {
    const container = $("runListContainer");
    if (!container) return;
    container.querySelectorAll(".run-card").forEach((el) => {
      el.classList.toggle("active", el.dataset.runId === activeId);
    });
  }

  function renderRunList() {
    const container = $("runListContainer");
    const q = state.searchQuery.trim().toLowerCase();
    const filtered = state.runs.filter((r) => {
      if (state.outcomeFilter !== "all" && r.outcome !== state.outcomeFilter) return false;
      if (!q) return true;
      const hay = `${r.runId} ${r.taskTypeCn || ""} ${r.taskType} ${r.summaryMessage || ""} ${r.originLabel || ""} ${r.serial || ""}`.toLowerCase();
      return hay.includes(q);
    });

    $("runCountBadge").textContent = `${filtered.length} 条`;

    if (filtered.length === 0) {
      container.innerHTML = `<div style="padding:24px;color:var(--text-muted);text-align:center;">没有符合筛选条件的任务记录</div>`;
      return;
    }

    container.innerHTML = "";
    for (const r of filtered) {
      const card = document.createElement("div");
      const isFailed = r.outcome === "failed";
      card.dataset.runId = r.id;
      card.className = `run-card ${state.activeRunRef === r.id ? "active" : ""} ${isFailed ? "failed-card" : ""}`;

      const statusBadge =
        r.outcome === "succeeded"
          ? `<span class="badge badge-succeeded">✅ 成功</span>`
          : r.outcome === "failed"
          ? `<span class="badge badge-failed">❌ 失败</span>`
          : `<span class="badge badge-warning">⏳ 执行中/未知</span>`;

      const originTitle = r.scopePath
        ? `${r.originDesc || ""}\n手机目录: ${r.scopePath}`
        : `${r.originDesc || ""}\n本地路径: ${r.relPath || ""}`;
      const originPill = `<span class="origin-pill" title="${escapeHtml(originTitle)}">${escapeHtml(
        r.originLabel || (r.sourceType === "adb" ? "📱 手机任务" : "💻 本地留档")
      )}</span>`;

      const syncTag =
        r.sourceType === "adb"
          ? `<span class="sync-tag ${r.synced ? "" : "unsynced"}">${r.synced ? "● 已缓存" : "○ 点选拉取"}</span>`
          : "";

      card.innerHTML = `
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px;">
          <div class="run-card-title">${escapeHtml(r.taskTypeCn || r.taskType)}</div>
          ${statusBadge}
        </div>
        ${
          r.summaryMessage
            ? `<div class="run-card-msg">${escapeHtml(r.summaryMessage)}</div>`
            : ""
        }
        <div class="run-card-foot">
          <span>${formatTime(r.mtime)} · <b>${r.snapshotCount || 0} 个画面</b></span>
          <div style="display:flex;align-items:center;gap:6px;">
            ${originPill}
            ${syncTag}
          </div>
        </div>
      `;

      card.addEventListener("click", () => {
        selectRun(r);
      });
      container.appendChild(card);
    }
  }

  async function selectRun(runItem) {
    try {
      state.activeRunRef = runItem.id;
      updateActiveRunCard(runItem.id);
      const needsPull = runItem.sourceType === "adb" && !runItem.synced;
      if (needsPull) {
        showToast(`正在从手机拉取任务记录 (${runItem.runId.slice(0, 8)})...`, 2400);
      }
      const data = await apiGet(`/api/runs/detail?runRef=${encodeURIComponent(runItem.id)}`);
      if (state.activeRunRef !== runItem.id) return;
      if (needsPull) {
        runItem.synced = true;
        const card = $("runListContainer")?.querySelector(`.run-card[data-run-id="${CSS.escape(runItem.id)}"]`);
        const tag = card?.querySelector(".sync-tag");
        if (tag) {
          tag.classList.remove("unsynced");
          tag.textContent = "● 已缓存";
        }
      }
      state.activeRun = data.run;
      onRunLoaded(data.run);
    } catch (err) {
      showToast(`加载任务详情失败: ${err.message}`, 4500);
    }
  }

  function onRunLoaded(run) {
    $("activeRunIdLabel").textContent = `任务ID: ${run.runId.slice(0, 12)}`;
    renderCenterTaskSummary(run);
    renderCenterTimeline(run);

    if (run.timelineSteps && run.timelineSteps.length > 0) {
      selectSnapshotByIndex(0);
    } else {
      state.activeSnapshotIndex = -1;
      state.activeSnapshot = null;
      drawCanvas();
    }
  }

  // --- 3. Center Panel: Task Summary & Chinese Step-by-Step Timeline ---
  function renderCenterTaskSummary(run) {
    const container = $("taskSummaryContainer");
    const ov = run.runOverviewCn || {};
    const reco = run.recognitionStats || {};
    const statusBadge =
      run.outcome === "succeeded"
        ? `<span class="badge badge-succeeded" style="font-size:13px;padding:4px 12px;">✅ 任务执行成功</span>`
        : run.outcome === "failed"
        ? `<span class="badge badge-failed" style="font-size:13px;padding:4px 12px;">❌ 任务执行失败</span>`
        : `<span class="badge badge-warning" style="font-size:13px;padding:4px 12px;">⏳ 状态未知</span>`;

    const opItemsHtml = (ov.operationItems || [])
      .map(
        (item) => `
        <div class="overview-kv">
          <div class="overview-kv-label">${escapeHtml(item.label)}</div>
          <div class="overview-kv-val">${escapeHtml(item.value)}</div>
        </div>
      `
      )
      .join("");

    // Render collected videos table if any
    const videos = ov.collectedVideos || [];
    let videosHtml = "";
    if (videos.length > 0) {
      videosHtml = `
        <div style="margin-top:14px;">
          <div style="font-weight:700;font-size:13.5px;color:#0f172a;margin-bottom:6px;">
            🎬 本次任务采集到的视频与作者数据（共 ${videos.length} 条）
          </div>
          ${videos
            .map(
              (v) => `
              <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:10px 14px;margin-bottom:8px;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
                  <strong style="font-size:14px;color:#166534;">作者：@${escapeHtml(v.authorNickname || "-")}</strong>
                  <span class="badge badge-succeeded">公开视频号ID：${escapeHtml(v.channelId || "未公开")}</span>
                </div>
                <div style="font-size:13px;color:#0f172a;margin-bottom:6px;">
                  <b>视频文案：</b>${escapeHtml(v.description || "-")}
                </div>
                <div style="display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:#475569;">
                  <span><b>发布时间：</b>${escapeHtml(v.publishTimeText || "-")}</span>
                  <span><b>发布地点：</b>${escapeHtml(v.publishLocation || "-")}</span>
                  <span><b>作者简介：</b>${escapeHtml(v.authorBio || "-")}</span>
                  <span><b>IP 属地：</b>${escapeHtml(v.authorIpLocation || "-")}</span>
                  <span><b>分享短链：</b><code>${escapeHtml(v.videoUrl || "-")}</code></span>
                </div>
              </div>
            `
            )
            .join("")}
        </div>
      `;
    }

    // Render collected comments summary if any
    const comments = ov.collectedComments || [];
    let commentsHtml = "";
    if (comments.length > 0) {
      commentsHtml = `
        <details style="margin-top:10px;background:var(--bg-subtle);border:1px solid var(--border);border-radius:8px;padding:8px 12px;">
          <summary style="cursor:pointer;font-weight:700;font-size:13px;color:#0f172a;">
            💬 展开查看本次从评论区提取到的全部评论明细（共 ${comments.length} 条）
          </summary>
          <div style="margin-top:8px;max-height:220px;overflow-y:auto;">
            <table class="data-table">
              <thead>
                <tr><th>#</th><th>评论者昵称</th><th>评论内容</th><th>地区 / 时间</th><th>点赞数</th></tr>
              </thead>
              <tbody>
                ${comments
                  .map(
                    (c, idx) => `
                    <tr>
                      <td>${idx + 1}</td>
                      <td><b>@${escapeHtml(c.nickname || "-")}</b></td>
                      <td>${escapeHtml(c.content || "-")}</td>
                      <td>${escapeHtml([c.ipLocation, c.commentTime].filter(Boolean).join(" · ") || "-")}</td>
                      <td>${c.likeCount ?? 0}</td>
                    </tr>
                  `
                  )
                  .join("")}
              </tbody>
            </table>
          </div>
        </details>
      `;
    }

    container.innerHTML = `
      <div class="summary-box">
        <div class="summary-header">
          <div>
            <h2 class="summary-title">${escapeHtml(ov.taskTypeCn || "视频号任务")}</h2>
            <div class="summary-meta">
              <span>📊 <b>${run.timelineSteps.length}</b> 个步骤 · ${reco.totalSnapshots || 0} 个画面</span>
              <span>⏱️ 识别总耗时 <b>${formatMs(reco.totalRecognitionMs || 0)}</b></span>
              ${
                run.totalTaskDurationMs
                  ? `<span>🏁 流程总耗时 <b>${formatMs(run.totalTaskDurationMs)}</b></span>`
                  : ""
              }
            </div>
          </div>
          ${statusBadge}
        </div>

        ${
          run.errorMessage
            ? `<div class="error-alert">🚨 <b>任务中断 / 失败原因：</b>${escapeHtml(run.errorMessage)}</div>`
            : ""
        }

        ${opItemsHtml ? `<div class="collected-overview-grid">${opItemsHtml}</div>` : ""}
        ${videosHtml}
        ${commentsHtml}
      </div>
    `;
  }

  function renderCenterTimeline(run) {
    const container = $("timelineStagesContainer");
    const snaps = run.timelineSteps || [];

    if (snaps.length === 0) {
      container.innerHTML = `
        <div class="summary-box" style="text-align:center;color:var(--text-muted);">
          该任务无需操作手机画面（或未生成步骤快照），请查看上方执行结论。
        </div>
      `;
      return;
    }

    // Group consecutive steps by `stageGroup`
    const groups = [];
    for (let i = 0; i < snaps.length; i++) {
      const s = snaps[i];
      const gName = s.stageGroup || "执行步骤";
      if (groups.length === 0 || groups[groups.length - 1].name !== gName) {
        groups.push({ name: gName, items: [] });
      }
      groups[groups.length - 1].items.push({ snap: s, index: i });
    }

    container.innerHTML = "";
    for (const grp of groups) {
      const sec = document.createElement("div");
      sec.className = "stage-section";
      sec.innerHTML = `
        <div class="stage-section-header">
          <span>📂 ${escapeHtml(grp.name)}</span>
          <span class="badge badge-blue">${grp.items.length} 个步骤</span>
        </div>
      `;

      const listEl = document.createElement("div");
      listEl.className = "timeline-list";

      for (const { snap, index } of grp.items) {
        const card = document.createElement("div");
        card.className = `t-step-card ${state.activeSnapshotIndex === index ? "active" : ""}`;
        card.dataset.stepIndex = String(index);

        const clickHtml = snap.clickTarget
          ? `<div class="t-step-action-pill">👆 <b>本步动作：</b>${escapeHtml(snap.clickTarget.label)} (坐标 X:${snap.clickTarget.x}, Y:${snap.clickTarget.y})</div>`
          : "";

        const collected = snap.collectedItems || [];
        const CIRCLED_NUMS = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"];
        let dataBoxHtml = "";
        if (collected.length > 0) {
          dataBoxHtml = `
            <div class="t-step-data-box">
              <div class="t-step-data-title">📦 本步收集 / 提取到的数据（共 ${collected.length} 项）</div>
              ${collected
                .map((item, cIdx) => {
                  const numBadge = CIRCLED_NUMS[cIdx] || `${cIdx + 1}.`;
                  return `
                  <div class="t-step-data-row">
                    <span class="t-step-data-k">${numBadge} ${escapeHtml(item.label)}</span>
                    <span class="t-step-data-v">${escapeHtml(item.value)}</span>
                  </div>
                `;
                })
                .join("")}
            </div>
          `;
        }

        card.innerHTML = `
          <div class="t-step-head">
            <span class="t-step-num">第 ${snap.sequence} 步</span>
            <span class="t-step-title">${escapeHtml(snap.titleCn || snap.label)}</span>
            <span class="badge badge-blue" title="底层快照文件名: ${escapeHtml(snap.filename)}">
              ${escapeHtml(snap.sourceCn || snap.source)}${snap.stepKind === "snapshot" ? ` · ${formatMs(snap.recognitionDurationMs)}` : ""}
            </span>
          </div>
          <div class="t-step-desc">${escapeHtml(snap.actionCn || "")}</div>
          ${clickHtml}
          ${dataBoxHtml}
        `;

        card.addEventListener("click", () => {
          selectSnapshotByIndex(index);
        });
        listEl.appendChild(card);
      }

      sec.appendChild(listEl);
      container.appendChild(sec);
    }
  }

  async function selectSnapshotByIndex(idx, hotReload = false) {
    const snaps = state.activeRun?.timelineSteps || [];
    if (idx < 0 || idx >= snaps.length) return;
    state.activeSnapshotIndex = idx;
    state.selectedLineIndex = -1;
    state.hoveredLineIndex = -1;

    // Highlight active step card in center timeline
    document.querySelectorAll(".t-step-card").forEach((el) => {
      const isAct = Number(el.dataset.stepIndex) === idx;
      el.classList.toggle("active", isAct);
    });

    const snapSummary = snaps[idx];
    $("rightScreenTitle").textContent = snapSummary.stepKind === "snapshot"
      ? `📱 第 ${snapSummary.sequence} 步画面（共 ${snaps.length} 步）`
      : `第 ${snapSummary.sequence} 步 · ${snapSummary.sourceCn}`;

    // Update top caption in right panel
    const actionParts = [`<b>第 ${snapSummary.sequence} 步：</b>${escapeHtml(snapSummary.titleCn || snapSummary.label)}`];
    if (snapSummary.clickTarget) {
      actionParts.push(`<span style="color:var(--red);">🎯 ${escapeHtml(snapSummary.clickTarget.label)}</span>`);
    }
    if ((snapSummary.collectedItems || []).length > 0) {
      actionParts.push(`<span style="color:var(--green);">📦 提取了 ${snapSummary.collectedItems.length} 项数据</span>`);
    }
    $("screenStepActionText").innerHTML = actionParts.join(" &nbsp;|&nbsp; ");

    const hasSnapshot = snapSummary.stepKind === "snapshot";
    $("screenCanvas").hidden = !hasSnapshot;
    $("screenCanvas").style.display = hasSnapshot ? "" : "none";
    $("btnQuickHotReplay").disabled = !hasSnapshot;
    if (!hasSnapshot) {
      state.activeSnapshot = null;
      state.activeFrameImg = null;
      updateRealFrameToggleButton();
      renderRawLinesTable();
      $("screenStepActionText").textContent = `${snapSummary.titleCn}。此步骤记录执行结果，没有手机画面。`;
      return;
    }

    try {
      const params = new URLSearchParams({
        runRef: state.activeRun.runRef || state.activeRunRef,
        filename: snapSummary.filename,
        hotReload: hotReload ? "1" : "0",
      });
      const data = await apiGet(`/api/runs/snapshot?${params.toString()}`);
      if (state.activeRun?.timelineSteps?.[state.activeSnapshotIndex] !== snapSummary) return;
      state.activeSnapshot = {
        ...data.snapshot,
        summaryMeta: snapSummary,
      };

      state.activeFrameImg = null;
      updateRealFrameToggleButton();
      if (data.snapshot.hasFrame) {
        const frameUrl = `/api/runs/frame?runRef=${encodeURIComponent(
          state.activeRun.runRef || state.activeRunRef
        )}&filename=${encodeURIComponent(snapSummary.filename)}`;
        loadFrameImage(frameUrl);
      } else {
        drawCanvas();
      }
      renderRawLinesTable();
    } catch (err) {
      showToast(`加载步骤画面失败: ${err.message}`);
    }
  }

  function updateRealFrameToggleButton() {
    const btn = $("btnToggleRealFrame");
    if (!btn) return;
    const hasFrame = Boolean(state.activeSnapshot?.hasFrame);
    btn.style.display = hasFrame ? "" : "none";
    btn.textContent = state.showRealFrame ? "🖼️ 真实截图" : "🧩 控件模拟";
  }

  function loadFrameImage(url) {
    if (state.frameImageCache[url]) {
      state.activeFrameImg = state.frameImageCache[url];
      drawCanvas();
      return;
    }
    const requestedSnapshot = state.activeSnapshot;
    const img = new Image();
    img.onload = () => {
      if (state.activeSnapshot !== requestedSnapshot) return;
      state.frameImageCache[url] = img;
      state.activeFrameImg = img;
      drawCanvas();
    };
    img.onerror = () => {
      if (state.activeSnapshot !== requestedSnapshot) return;
      state.activeFrameImg = null;
      drawCanvas();
    };
    img.src = url;
  }

  // --- 4. Right Panel: Intuitive Phone Screen Reconstruction Canvas ---
  function drawRoundedRect(ctx, x, y, w, h, r) {
    const radius = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + radius, y);
    ctx.arcTo(x + w, y, x + w, y + h, radius);
    ctx.arcTo(x + w, y + h, x, y + h, radius);
    ctx.arcTo(x, y + h, x, y, radius);
    ctx.arcTo(x, y, x + w, y, radius);
    ctx.closePath();
  }

  function drawCanvas() {
    const canvas = $("screenCanvas");
    const ctx = canvas.getContext("2d");
    const snap = state.activeSnapshot;
    const meta = snap?.summaryMeta || {};

    const logicalW = snap?.width || 720;
    const logicalH = snap?.height || 1560;
    const stageEl = canvas.closest(".phone-stage");
    const availH = stageEl ? Math.max(400, stageEl.clientHeight - 84) : 520;
    const scale = Math.min(0.42, Math.max(0.28, availH / logicalH));
    state.currentCanvasScale = scale;
    canvas.width = Math.round(logicalW * scale);
    canvas.height = Math.round(logicalH * scale);

    ctx.save();
    ctx.scale(scale, scale);

    const theme = meta.uiTheme || "light";
    const useRealFrame = Boolean(state.activeFrameImg && state.showRealFrame);

    // 1. Draw Realistic Phone Screen Background
    if (useRealFrame) {
      ctx.drawImage(state.activeFrameImg, 0, 0, logicalW, logicalH);
    } else if (theme === "dark-video") {
      // Dark Video Player Theme
      const grad = ctx.createLinearGradient(0, 0, 0, logicalH);
      grad.addColorStop(0, "#18181b");
      grad.addColorStop(0.7, "#27272a");
      grad.addColorStop(1, "#09090b");
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, logicalW, logicalH);

      // Subtle center video play icon watermark
      ctx.save();
      ctx.fillStyle = "rgba(255, 255, 255, 0.06)";
      ctx.beginPath();
      ctx.arc(logicalW / 2, logicalH * 0.42, 68, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "rgba(255, 255, 255, 0.12)";
      ctx.beginPath();
      ctx.moveTo(logicalW / 2 - 18, logicalH * 0.42 - 28);
      ctx.lineTo(logicalW / 2 - 18, logicalH * 0.42 + 28);
      ctx.lineTo(logicalW / 2 + 30, logicalH * 0.42);
      ctx.closePath();
      ctx.fill();
      ctx.restore();
    } else if (theme === "comment-drawer") {
      // Top dark video + bottom white comment drawer sheet
      ctx.fillStyle = "#18181b";
      ctx.fillRect(0, 0, logicalW, logicalH);
      const drawerTop = Math.round(logicalH * 0.26);
      ctx.fillStyle = "#f8fafc";
      drawRoundedRect(ctx, 0, drawerTop, logicalW, logicalH - drawerTop, 28);
      ctx.fill();
      // Bottom input bar background
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, logicalH - 110, logicalW, 110);
    } else {
      // Clean WeChat Light UI Theme
      ctx.fillStyle = "#f1f5f9";
      ctx.fillRect(0, 0, logicalW, logicalH);
      // Top WeChat navigation bar background
      ctx.fillStyle = "#e2e8f0";
      ctx.fillRect(0, 0, logicalW, 150);
      // Bottom bar background
      ctx.fillStyle = "#f8fafc";
      ctx.fillRect(0, logicalH - 130, logicalW, 130);
    }

    // Top Status Bar Mock (`9:41  5G 100%`) only on synthetic wireframe, never over real screenshots
    if (!useRealFrame) {
      ctx.fillStyle = theme === "light" ? "#475569" : "rgba(255,255,255,0.75)";
      ctx.font = "bold 20px -apple-system, sans-serif";
      ctx.fillText("9:41", 32, 42);
      ctx.fillText("5G  100%", logicalW - 135, 42);
    }

    if (!snap) {
      ctx.fillStyle = "#64748b";
      ctx.font = "24px -apple-system, sans-serif";
      ctx.fillText("请在左侧或中间选择步骤", 210, logicalH / 2);
      ctx.restore();
      return;
    }

    // 2. Subtle Scan Region (ROI) if present
    if (meta.roiCn && Array.isArray(snap.roi) && snap.roi[2] > 0 && snap.roi[3] > 0) {
      const [rx, ry, rw, rh] = snap.roi;
      ctx.save();
      ctx.fillStyle = "rgba(2, 132, 199, 0.06)";
      ctx.strokeStyle = "rgba(2, 132, 199, 0.45)";
      ctx.lineWidth = 2.5;
      ctx.setLineDash([10, 6]);
      ctx.fillRect(rx + 4, ry + 4, rw - 8, rh - 8);
      ctx.strokeRect(rx + 4, ry + 4, rw - 8, rh - 8);
      ctx.setLineDash([]);
      ctx.fillStyle = "#0284c7";
      ctx.font = "bold 19px -apple-system, sans-serif";
      ctx.fillText(`🔍 ${meta.roiCn}`, rx + 16, Math.max(32, ry + 30));
      ctx.restore();
    }

    // Build set of highlighted boxes (data extracted in this step)
    const highlightBoxes = meta.highlightBoxes || [];
    const CIRCLED_NUMS = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"];
    function matchHighlight(box) {
      const [bx, by] = box;
      for (let i = 0; i < highlightBoxes.length; i++) {
        const hb = highlightBoxes[i];
        if (!Array.isArray(hb.box)) continue;
        const [hx, hy] = hb.box;
        if (Math.abs(bx - hx) <= 16 && Math.abs(by - hy) <= 16) {
          const bIdx = (hb.badgeIndex || i + 1) - 1;
          return {
            label: hb.label || "已采集",
            numTag: CIRCLED_NUMS[bIdx] || `#${bIdx + 1}`,
          };
        }
      }
      return null;
    }

    // 3. Draw Clean De-duplicated UI Elements (`visualLines`)
    const vLines = snap.visualLines || snap.lines || [];
    const badgeBadgesToDraw = [];

    vLines.forEach((line, idx) => {
      if (!Array.isArray(line.box) || line.box.length < 4) return;
      const [bx, by, bw, bh] = line.box;
      const text = String(line.text || "").trim();
      if (!text) return;

      const hl = matchHighlight(line.box);
      const isHovered = idx === state.hoveredLineIndex;
      const isSelected = idx === state.selectedLineIndex;
      const isDarkBg =
        theme === "dark-video" || (theme === "comment-drawer" && by < logicalH * 0.26);

      ctx.save();
      const cardH = Math.max(bh, 32);
      if (!useRealFrame) {
        if (hl) {
          ctx.fillStyle = isDarkBg ? "rgba(22, 163, 74, 0.35)" : "#dcfce7";
          ctx.strokeStyle = "#16a34a";
          ctx.lineWidth = 3.5;
        } else if (isSelected || isHovered) {
          ctx.fillStyle = isDarkBg ? "rgba(13, 148, 136, 0.4)" : "#ccfbf1";
          ctx.strokeStyle = "#0d9488";
          ctx.lineWidth = 3;
        } else {
          ctx.fillStyle = isDarkBg ? "rgba(255, 255, 255, 0.12)" : "#ffffff";
          ctx.strokeStyle = isDarkBg ? "rgba(255, 255, 255, 0.24)" : "#cbd5e1";
          ctx.lineWidth = 1.5;
        }
        drawRoundedRect(ctx, bx, by, bw, cardH, 8);
        ctx.fill();
        ctx.stroke();

        // Draw readable Chinese text inside card, stepping down font size if needed so short labels never truncate
        ctx.fillStyle = hl
          ? isDarkBg
            ? "#4ade80"
            : "#14532d"
          : isDarkBg
          ? "#f8fafc"
          : "#1e293b";
        const cleanSingleLine = text.replace(/\s+/g, " ");
        let fontSize = Math.min(22, Math.max(15, Math.round(bh * 0.58)));
        const weight = hl ? "bold " : "";
        ctx.font = `${weight}${fontSize}px -apple-system, "PingFang SC", sans-serif`;
        while (fontSize > 12 && ctx.measureText(cleanSingleLine).width > bw - 12) {
          fontSize -= 1;
          ctx.font = `${weight}${fontSize}px -apple-system, "PingFang SC", sans-serif`;
        }
        let displayTxt = cleanSingleLine;
        if (ctx.measureText(displayTxt).width > bw - 10) {
          while (displayTxt.length > 2 && ctx.measureText(displayTxt + "…").width > bw - 10) {
            displayTxt = displayTxt.slice(0, -1);
          }
          displayTxt += "…";
        }
        ctx.fillText(displayTxt, bx + 6, by + Math.round((cardH + fontSize * 0.76) / 2));
      } else {
        ctx.strokeStyle = hl ? "#16a34a" : isSelected || isHovered ? "#0d9488" : "rgba(148,163,184,0.55)";
        ctx.lineWidth = hl ? 4 : 2;
        ctx.strokeRect(bx, by, bw, bh);
      }

      if (hl) {
        badgeBadgesToDraw.push({ x: bx, y: by, numTag: hl.numTag });
      }
      ctx.restore();
    });

    // Draw compact numbered badges (① ② ③) at top-left corner of highlighted boxes
    for (const b of badgeBadgesToDraw) {
      ctx.save();
      const cx = Math.max(16, b.x - 4);
      const cy = Math.max(16, b.y + 4);
      ctx.fillStyle = "#16a34a";
      ctx.beginPath();
      ctx.arc(cx, cy, 14, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.fillStyle = "#ffffff";
      ctx.font = "bold 16px -apple-system, sans-serif";
      const tw = ctx.measureText(b.numTag).width;
      ctx.fillText(b.numTag, cx - tw / 2, cy + 6);
      ctx.restore();
    }

    // 4. Draw Prominent Red Click Target Bullseye & Callout Bubble (`clickTarget`)
    const clickTarget = meta.clickTarget;
    if (clickTarget && typeof clickTarget.x === "number" && typeof clickTarget.y === "number") {
      const cx = clickTarget.x;
      const cy = clickTarget.y;
      ctx.save();

      // Outer halo
      ctx.fillStyle = "rgba(225, 29, 72, 0.22)";
      ctx.beginPath();
      ctx.arc(cx, cy, 32, 0, Math.PI * 2);
      ctx.fill();

      // Target circle
      ctx.strokeStyle = "#e11d48";
      ctx.lineWidth = 4;
      ctx.beginPath();
      ctx.arc(cx, cy, 18, 0, Math.PI * 2);
      ctx.stroke();

      // Center dot
      ctx.fillStyle = "#e11d48";
      ctx.beginPath();
      ctx.arc(cx, cy, 6, 0, Math.PI * 2);
      ctx.fill();

      // Callout pill label positioned above (or below if near top) so it never covers the clicked row text
      const calloutTxt = `👆 ${clickTarget.label || "本步点击位置"}`;
      ctx.font = "bold 18px -apple-system, sans-serif";
      const pillW = Math.min(460, ctx.measureText(calloutTxt).width + 24);
      const pillH = 32;
      const pillX = Math.max(12, Math.min(logicalW - pillW - 12, cx - pillW / 2));
      const placeAbove = cy > 140;
      const pillY = placeAbove ? cy - pillH - 24 : cy + 24;

      // Connector line from pill to target circle
      ctx.strokeStyle = "#e11d48";
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      ctx.moveTo(cx, placeAbove ? pillY + pillH : pillY);
      ctx.lineTo(cx, placeAbove ? cy - 18 : cy + 18);
      ctx.stroke();

      ctx.fillStyle = "#e11d48";
      drawRoundedRect(ctx, pillX, pillY, pillW, pillH, 9);
      ctx.fill();
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 2;
      ctx.stroke();

      ctx.fillStyle = "#ffffff";
      ctx.fillText(calloutTxt, pillX + 12, pillY + 22);
      ctx.restore();
    }

    ctx.restore();
  }

  function renderRawLinesTable() {
    const snap = state.activeSnapshot;
    const vLines = snap?.visualLines || [];
    $("rawLinesCount").textContent = String(vLines.length);
    const tbody = $("linesTableBody");
    const q = ($("lineFilterInput").value || "").trim().toLowerCase();
    tbody.innerHTML = "";

    vLines.forEach((line, idx) => {
      if (q && !String(line.text || "").toLowerCase().includes(q)) return;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${idx + 1}</td>
        <td><b>${escapeHtml(line.text)}</b></td>
        <td style="font-family:var(--font-mono);font-size:11px;">[${(line.box || []).join(", ")}]</td>
        <td>${escapeHtml(line.source)}</td>
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
        state.selectedLineIndex = idx;
        $("selectedBoxDetail").textContent = `已选中 #${idx + 1}: "${line.text}" 坐标=[${(line.box || []).join(", ")}]`;
        drawCanvas();
      });
      tbody.appendChild(tr);
    });
  }

  // --- 5. Live Probe, Code Push & Developer Modal ---
  async function handleLiveProbe() {
    if (!state.selectedSerial) {
      showToast("请先连接一台手机设备");
      return;
    }
    const btn = $("btnLiveProbe");
    btn.disabled = true;
    btn.textContent = "⏳ 正在抓屏...";
    try {
      const res = await apiPost("/api/device/probe", {
        serial: state.selectedSerial,
        label: "live-probe",
      });
      state.activeRunRef = res.probe.runRef;
      state.activeRun = res.run;
      onRunLoaded(res.run);
      const lastIdx = (res.run.timelineSteps || []).findLastIndex((step) => step.stepKind === "snapshot");
      if (lastIdx >= 0) await selectSnapshotByIndex(lastIdx);
      showToast(`📸 已抓取手机当前屏幕 (${res.probe.lineCount} 项控件)！`);
      loadRuns();
    } catch (err) {
      showToast(`抓屏失败: ${err.message}`, 4500);
    } finally {
      btn.disabled = false;
      btn.textContent = "📸 抓取当前手机屏幕";
    }
  }

  async function handlePushCode() {
    if (!state.selectedSerial) {
      showToast("请先选择一台手机设备");
      return;
    }
    const btn = $("btnPushCode");
    btn.disabled = true;
    try {
      showToast(`正在推送最新代码到手机 ${state.selectedSerial}...`);
      const res = await apiPost("/api/device/push-code", { serial: state.selectedSerial });
      const appCount = (res.result.appRuntimesSynced || []).length;
      showToast(`✅ 已同步 ${res.result.pushedCount} 个文件（同时更新正式版与 Debug 版共 ${appCount} 个运行时）`);
    } catch (err) {
      showToast(`推送失败: ${err.message}`, 4500);
    } finally {
      btn.disabled = false;
    }
  }

  async function startDeviceTask() {
    if (!state.selectedSerial) {
      showToast("请先选择一台手机设备");
      return;
    }
    let cfg = {};
    try {
      const txt = $("runnerConfigTextarea").value.trim();
      cfg = txt ? JSON.parse(txt) : {};
    } catch (err) {
      showToast(`JSON 参数格式错误: ${err.message}`);
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
      showToast(`🚀 任务 #${res.task.taskId} 已在手机启动！`);
      loadDeviceTasks();
      if (state.taskPollingTimer) clearInterval(state.taskPollingTimer);
      state.taskPollingTimer = setInterval(loadDeviceTasks, 2000);
    } catch (err) {
      showToast(`启动失败: ${err.message}`, 4500);
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
      container.innerHTML = tasks
        .map(
          (t) => `
          <div style="border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:8px;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <strong>#${escapeHtml(t.taskId)} · ${escapeHtml(t.taskMode)}</strong>
              <span class="badge ${t.status === "succeeded" ? "badge-succeeded" : t.status === "failed" ? "badge-failed" : "badge-warning"}">${escapeHtml(
            t.status
          )}</span>
            </div>
            <pre class="code-block" style="margin-top:6px;max-height:180px;">${escapeHtml(
              (t.logs || []).join("\n") || "正在执行..."
            )}</pre>
          </div>
        `
        )
        .join("");
    } catch (_) {}
  }

  function exportSnapshotUnitTest() {
    const snap = state.activeSnapshot;
    if (!snap) {
      showToast("请先选择一个步骤");
      return;
    }
    const compactLines = (snap.lines || []).map((l) => ({
      text: l.text,
      box: l.box,
      source: l.source || "ocr",
    }));
    const pState = snap.replay?.parsers?.pageState?.state || "unknown";
    const code = `def test_snapshot_${snap.label.replace(/[^a-zA-Z0-9_]/g, "_")}(self):\n    lines = ${JSON.stringify(
      compactLines,
      null,
      4
    )}\n    self.assertEqual(classify_page_state(lines, ${snap.height || 1605}), "${pState}")\n`;
    navigator.clipboard.writeText(code);
    showToast("📋 已复制单元测试代码到剪贴板！");
  }

  function bindEvents() {
    document.querySelectorAll("#sourceFilterTabs .seg-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.sourceFilter = btn.dataset.source;
        syncSourceTabsUI();
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
      updateOriginExplain();
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
      showToast("已刷新列表");
    });

    $("btnLiveProbe").addEventListener("click", handleLiveProbe);
    $("btnPushCode").addEventListener("click", handlePushCode);

    const btnToggleFrame = $("btnToggleRealFrame");
    if (btnToggleFrame) {
      btnToggleFrame.addEventListener("click", () => {
        state.showRealFrame = !state.showRealFrame;
        updateRealFrameToggleButton();
        drawCanvas();
      });
    }

    $("btnPrevStep").addEventListener("click", () => {
      if (state.activeSnapshotIndex > 0) selectSnapshotByIndex(state.activeSnapshotIndex - 1);
    });
    $("btnNextStep").addEventListener("click", () => {
      const len = state.activeRun?.timelineSteps?.length || 0;
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
        const len = state.activeRun?.timelineSteps?.length || 0;
        if (state.activeSnapshotIndex + 1 < len) {
          e.preventDefault();
          selectSnapshotByIndex(state.activeSnapshotIndex + 1);
        }
      }
    });

    // Canvas click to inspect element
    const canvas = $("screenCanvas");
    window.addEventListener("resize", () => drawCanvas());
    canvas.addEventListener("mousemove", (e) => {
      const rect = canvas.getBoundingClientRect();
      const scale = state.currentCanvasScale || 0.4;
      const lx = Math.round((e.clientX - rect.left) / scale);
      const ly = Math.round((e.clientY - rect.top) / scale);
      const vLines = state.activeSnapshot?.visualLines || [];
      let found = -1;
      let bestArea = Infinity;
      vLines.forEach((line, idx) => {
        const [bx, by, bw, bh] = line.box || [0, 0, 0, 0];
        if (lx >= bx && lx <= bx + bw && ly >= by && ly <= by + Math.max(bh, 32)) {
          const area = bw * bh;
          if (area < bestArea) {
            bestArea = area;
            found = idx;
          }
        }
      });
      if (found !== state.hoveredLineIndex) {
        state.hoveredLineIndex = found;
        drawCanvas();
      }
    });

    canvas.addEventListener("click", () => {
      if (state.hoveredLineIndex >= 0) {
        state.selectedLineIndex = state.hoveredLineIndex;
        const line = (state.activeSnapshot?.visualLines || [])[state.selectedLineIndex];
        if (line) {
          $("selectedBoxDetail").textContent = `选中元素: "${line.text}" (坐标: [${line.box.join(", ")}])`;
        }
        drawCanvas();
      }
    });

    $("lineFilterInput").addEventListener("input", renderRawLinesTable);
    $("btnQuickHotReplay").addEventListener("click", async () => {
      if (state.activeSnapshotIndex >= 0) {
        await selectSnapshotByIndex(state.activeSnapshotIndex, true);
        showToast("🔥 已热重载解析器并更新当前步！");
      }
    });

    // Developer modal
    $("btnOpenDevModal").addEventListener("click", () => {
      $("devModalBackdrop").classList.add("open");
      loadDeviceTasks();
    });
    $("btnCloseDevModal").addEventListener("click", () => {
      $("devModalBackdrop").classList.remove("open");
    });
    $("runnerTaskMode").addEventListener("change", (e) => {
      $("runnerConfigTextarea").value = JSON.stringify(TASK_TEMPLATES[e.target.value] ?? {}, null, 2);
    });
    $("btnStartDeviceTask").addEventListener("click", startDeviceTask);
    $("btnExportUnitTest").addEventListener("click", exportSnapshotUnitTest);
  }

  async function init() {
    bindEvents();
    // Wait for ADB device probe once so the left list renders directly with the true latest runs
    // instead of flashing local cache and then replacing the entire list mid-click.
    await loadDevices();
    if (!state.selectedSerial) {
      state.sourceFilter = "local";
    }
    syncSourceTabsUI();
    await loadRuns();
  }

  init();
})();
