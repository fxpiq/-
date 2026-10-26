const recordingsRoot = document.querySelector("#recordings");
const recordingTemplate = document.querySelector("#recording-template");
const clipTemplate = document.querySelector("#clip-template");
const messageBox = document.querySelector("#message");
const serviceStatus = document.querySelector("#service-status");
const runtimeBanner = document.querySelector("#runtime-banner");
const runtimeLabel = document.querySelector("#runtime-label");
const runtimeDetail = document.querySelector("#runtime-detail");
const cleanupButton = document.querySelector("#cleanup-button");
const diskFree = document.querySelector("#disk-free");
const diskUsageBar = document.querySelector("#disk-usage-bar");
const diskNote = document.querySelector("#disk-note");
let latestRecordings = [];
let runtime = {
  recording_busy: false,
  clipping_busy: false,
  storage: null,
};
let pollTimer;

const labels = {
  starting: "准备中",
  recording: "录制中",
  stopping: "处理中",
  recorded: "录像完成",
  failed: "失败",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[char]);
}

function formatTime(seconds) {
  if (!seconds) return "—";
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return [hours, minutes, secs]
    .map((value) => String(value).padStart(2, "0"))
    .join(":");
}

function formatClock(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function showMessage(message) {
  messageBox.textContent = message;
  messageBox.hidden = !message;
  if (message) {
    window.clearTimeout(showMessage.timer);
    showMessage.timer = window.setTimeout(() => {
      messageBox.hidden = true;
    }, 6000);
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch {
      // Keep the HTTP status as the message.
    }
    throw new Error(detail);
  }
  return response.json();
}

function statusClass(status) {
  if (status === "recording" || status === "starting") return "live";
  if (status === "recorded") return "done";
  if (status === "failed") return "fail";
  return "";
}

function canStartRecording() {
  return Boolean(runtime.storage?.can_start) && !runtime.recording_busy;
}

function makeClip(clip) {
  const node = clipTemplate.content.firstElementChild.cloneNode(true);
  node.querySelector("video").src = clip.preview_url;
  node.querySelector(".clip-title").textContent = clip.title;
  node.querySelector(".clip-meta").textContent =
    `原片 ${formatTime(clip.start_seconds)}–${formatTime(clip.end_seconds)} · ${Math.round(clip.end_seconds - clip.start_seconds)} 秒`;
  node.querySelector(".clip-reason").textContent = clip.reason || "AI 高光候选";
  const download = node.querySelector(".download");
  download.href = clip.download_url;
  return node;
}

async function loadClips(recordingId, container) {
  try {
    const clips = await api(`/api/recordings/${recordingId}/clips`);
    container.replaceChildren(...clips.map(makeClip));
    container.hidden = clips.length === 0;
  } catch {
    container.hidden = true;
  }
}

function makeRecording(recording) {
  const node = recordingTemplate.content.firstElementChild.cloneNode(true);
  node.dataset.id = recording.id;
  node.querySelector(".recording-title").textContent = recording.title;
  node.querySelector(".recording-meta").textContent =
    `${formatClock(recording.created_at)} · ${recording.id}`;
  const badge = node.querySelector(".badge");
  badge.textContent = labels[recording.record_status] || recording.record_status;
  badge.classList.add(statusClass(recording.record_status));

  const progressTrack = node.querySelector(".progress-track");
  const progressBar = progressTrack.querySelector("span");
  const progressPercent = node.querySelector(".progress-percent");
  const isProcessing = recording.process_status === "processing";
  if (isProcessing) {
    progressTrack.hidden = false;
    progressBar.style.width = `${Math.max(3, recording.process_progress)}%`;
    progressPercent.hidden = false;
    progressPercent.textContent = `${recording.process_progress}%`;
  } else {
    progressPercent.hidden = true;
  }

  let stage = recording.process_stage || "";
  if (recording.record_status === "recorded" && !isProcessing) {
    stage = `时长 ${formatTime(recording.duration_seconds)} · 可生成 AI 切片`;
  }
  node.querySelector(".stage").textContent = stage;

  if (recording.error) {
    const error = node.querySelector(".error");
    error.hidden = false;
    error.textContent = recording.error;
  }

  const stopButton = node.querySelector(".stop-button");
  stopButton.hidden = !["starting", "recording"].includes(recording.record_status);
  stopButton.addEventListener("click", async () => {
    if (!window.confirm("确定停止当前录像并合并文件吗？")) return;
    stopButton.disabled = true;
    try {
      await api(`/api/recordings/${recording.id}/stop`, { method: "POST" });
      showMessage("已发送停止指令，正在安全合并录像。");
      await refresh();
    } catch (error) {
      showMessage(error.message);
      stopButton.disabled = false;
    }
  });

  const form = node.querySelector(".clip-form");
  form.hidden = recording.record_status !== "recorded";
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector(".process-button");
    const [minSeconds, maxSeconds] = form
      .querySelector(".clip-duration")
      .value.split(",")
      .map(Number);
    button.disabled = true;
    try {
      await api(`/api/recordings/${recording.id}/process`, {
        method: "POST",
        body: JSON.stringify({
          clip_count: Number(form.querySelector(".clip-count").value),
          min_seconds: minSeconds,
          max_seconds: maxSeconds,
        }),
      });
      showMessage("AI 切片任务已启动，页面会自动刷新进度。");
      await refresh();
    } catch (error) {
      showMessage(error.message);
      button.disabled = false;
    }
  });

  if (recording.video_url) {
    const videoLink = node.querySelector(".video-link");
    videoLink.hidden = false;
    videoLink.href = recording.video_url;
  }

  const deleteButton = node.querySelector(".delete-button");
  const processing = recording.process_status === "processing";
  const canDelete =
    ["recorded", "failed"].includes(recording.record_status) && !processing;
  deleteButton.hidden = !canDelete;
  deleteButton.addEventListener("click", async () => {
    const confirmed = window.confirm(
      `确定永久删除“${recording.title}”吗？\n\n录像、切片和任务记录都会被删除，无法从控制台恢复。`
    );
    if (!confirmed) return;
    deleteButton.disabled = true;
    try {
      await api(`/api/recordings/${recording.id}`, { method: "DELETE" });
      showMessage("历史任务及其本地视频已删除。");
      await refresh();
    } catch (error) {
      showMessage(error.message);
      deleteButton.disabled = false;
    }
  });

  const clips = node.querySelector(".clips");
  if (recording.process_status === "completed") {
    loadClips(recording.id, clips);
  }
  return node;
}

async function refresh() {
  window.clearTimeout(pollTimer);
  try {
    [latestRecordings, runtime] = await Promise.all([
      api("/api/recordings"),
      api("/api/runtime"),
    ]);
    serviceStatus.className = "status-pill online";
    serviceStatus.innerHTML = '<span class="dot"></span>服务在线';
    const startButton = document.querySelector("#start-form button[type=submit]");
    const storage = runtime.storage;
    if (storage) {
      diskFree.textContent = `${storage.free_gb.toFixed(1)} GB 可用`;
      diskUsageBar.style.width = `${storage.used_percent}%`;
      diskNote.textContent =
        `已用 ${storage.used_percent}% · 启动最低保留 ` +
        `${storage.min_start_free_gb} GB · 合并后至少保留 ` +
        `${storage.min_residual_gb} GB`;
    }
    if (runtime.recording_busy && runtime.active_recording) {
      const active = runtime.active_recording;
      runtimeBanner.className = "runtime-banner recording";
      runtimeLabel.textContent = `正在录制：${active.title}`;
      runtimeDetail.textContent =
        `${active.id} · ${active.record_status}` +
        (active.process_stage ? ` · ${active.process_stage}` : "");
      startButton.disabled = true;
    } else if (!storage?.can_start) {
      runtimeBanner.className = "runtime-banner warning";
      runtimeLabel.textContent = "磁盘空间不足，已禁止开始新录制";
      runtimeDetail.textContent =
        `当前可用 ${storage?.free_gb.toFixed(1) ?? "0"} GB，` +
        `至少需要 ${storage?.min_start_free_gb ?? "--"} GB。` +
        "请先删除不需要的历史任务。";
      startButton.disabled = true;
    } else {
      runtimeBanner.className = "runtime-banner";
      runtimeLabel.textContent = "当前空闲，可以开始新录制";
      runtimeDetail.textContent = runtime.clipping_busy
        ? "有一个 AI 切片任务正在后台处理"
        : "没有正在运行的录像任务";
      startButton.disabled = false;
    }
    const deletableCount = latestRecordings.filter(
      (item) =>
        ["recorded", "failed"].includes(item.record_status) &&
        item.process_status !== "processing"
    ).length;
    cleanupButton.disabled = deletableCount === 0;
    if (!latestRecordings.length) {
      recordingsRoot.innerHTML = '<div class="empty card">还没有录像任务。</div>';
    } else {
      recordingsRoot.replaceChildren(...latestRecordings.map(makeRecording));
    }
  } catch (error) {
    serviceStatus.className = "status-pill offline";
    serviceStatus.innerHTML = '<span class="dot"></span>连接异常';
    if (!latestRecordings.length) {
      recordingsRoot.innerHTML =
        `<div class="empty card">${escapeHtml(error.message)}</div>`;
    }
    runtimeBanner.className = "runtime-banner warning";
    runtimeLabel.textContent = "无法读取运行状态";
    runtimeDetail.textContent = "请检查服务器连接后刷新页面";
    diskNote.textContent = "磁盘状态未知";
    document.querySelector("#start-form button[type=submit]").disabled = true;
  } finally {
    const hasActive =
      runtime.recording_busy ||
      latestRecordings.some(
        (item) => item.process_status === "processing"
      );
    const isViewingClip = [...document.querySelectorAll(".clip video")].some(
      (video) => !video.paused
    );
    if (hasActive && !isViewingClip) {
      pollTimer = window.setTimeout(refresh, 2500);
    }
  }
}

document.querySelector("#start-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    await api("/api/recordings/start", {
      method: "POST",
      body: JSON.stringify({
        url: document.querySelector("#live-url").value.trim(),
        title: document.querySelector("#live-title").value.trim(),
      }),
    });
    document.querySelector("#live-title").value = "";
    showMessage("录像任务已创建，正在解析直播间。");
    await refresh();
  } catch (error) {
    showMessage(error.message);
  } finally {
    button.disabled = !canStartRecording();
  }
});

document.querySelector("#refresh-button").addEventListener("click", refresh);
cleanupButton.addEventListener("click", async () => {
  const endedCount = latestRecordings.filter(
    (item) =>
      ["recorded", "failed"].includes(item.record_status) &&
      item.process_status !== "processing"
  ).length;
  if (!endedCount) {
    showMessage("没有可清理的历史任务。");
    return;
  }
  const confirmed = window.confirm(
    `将删除 ${endedCount} 个已结束任务及其全部录像和切片。\n\n此操作无法从控制台恢复，是否继续？`
  );
  if (!confirmed) return;
  cleanupButton.disabled = true;
  try {
    const result = await api("/api/recordings/cleanup-history", {
      method: "POST",
    });
    showMessage(
      `已删除 ${result.deleted_count} 个历史任务` +
        (result.failed.length ? `，${result.failed.length} 个失败` : "")
    );
    await refresh();
  } catch (error) {
    showMessage(error.message);
    cleanupButton.disabled = false;
  }
});
refresh();

