const state = {
  apiKey: "",
  sourceFile: null,
  referenceFile: null,
  sourceObjectUrl: null,
  referenceObjectUrl: null,
  sourceAssetId: null,
  referenceAssetId: null,
  uploaded: false,
  requestId: null,
  resumeOnly: false,
  submissionUnknown: false,
  finished: false,
  ffmpeg: false,
  busy: false,
};

const $ = (selector) => document.querySelector(selector);
const elements = {
  apiKey: $("#api-key"),
  sourceFile: $("#source-file"),
  referenceFile: $("#reference-file"),
  sourceName: $("#source-name"),
  referenceName: $("#reference-name"),
  sourcePreview: $("#source-preview"),
  referencePreview: $("#reference-preview"),
  uploadButton: $("#upload-button"),
  recastButton: $("#recast-button"),
  uploadStatus: $("#upload-status"),
  prompt: $("#prompt"),
  error: $("#error-box"),
  progress: $("#progress-panel"),
  progressTitle: $("#progress-title"),
  progressCopy: $("#progress-copy"),
  progressBar: $("#progress-bar"),
  progressLabel: $("#progress-label"),
  progressRequest: $("#progress-request"),
  resultPanel: $("#result-panel"),
  resultVideo: $("#result-video"),
  resultLink: $("#result-open-link"),
  downloadLink: $("#download-link"),
  requirements: $("#requirements-card"),
};

function setError(message) {
  elements.error.textContent = message || "Something went wrong.";
  elements.error.classList.remove("hidden");
}

function clearError() {
  elements.error.textContent = "";
  elements.error.classList.add("hidden");
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  let body = options.body;
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.json);
  }
  const response = await fetch(path, { method: options.method || "GET", headers, body });
  let data = {};
  try { data = await response.json(); } catch { /* An empty body is explained by the status code. */ }
  if (!response.ok) {
    let detail = data.detail;
    if (detail && typeof detail === "object") detail = detail.error?.message || detail.message;
    const error = new Error(typeof detail === "string" ? detail : data.message || "Request failed (" + response.status + ").");
    error.status = response.status;
    throw error;
  }
  return data;
}

function updateControls() {
  const hasInputs = Boolean(state.apiKey.value.trim() && state.sourceFile && state.referenceFile);
  elements.uploadButton.disabled = !hasInputs || state.busy || state.uploaded;
  elements.apiKey.disabled = state.busy || state.uploaded;
  elements.sourceFile.disabled = state.busy || state.uploaded;
  elements.referenceFile.disabled = state.busy || state.uploaded;
  elements.prompt.disabled = state.busy || !state.uploaded;
  elements.recastButton.disabled = state.busy || !state.uploaded || state.submissionUnknown || state.finished;
}

function setStep(activeStep) {
  document.querySelectorAll(".stepper-step").forEach((step) => {
    const number = Number(step.dataset.step);
    step.classList.toggle("active", number === activeStep);
    step.classList.toggle("done", number < activeStep);
  });
}

function showProgress(requestId, status = "processing") {
  elements.progress.classList.remove("hidden");
  elements.resultPanel.classList.add("hidden");
  elements.progressTitle.textContent = "Recasting the performer…";
  elements.progressCopy.textContent = "Motion guidance and scene preparation run inside the workflow. This may take a few minutes; keep this tab open.";
  elements.progressRequest.textContent = requestId ? "REQUEST " + requestId.slice(0, 12).toUpperCase() : "";
  const labels = { submitting: "SUBMITTING REQUEST", processing: "WAITING FOR SERVICE", completed: "COMPLETE" };
  elements.progressLabel.textContent = labels[status] || "WAITING FOR SERVICE";
  elements.progressBar.classList.toggle("indeterminate", status !== "completed");
  elements.progressBar.classList.toggle("finished", status === "completed");
  $("#progress-eyebrow").textContent = "SEEDANCE MOTION CONTROL";
}

function findVideoUrl(payload) {
  const seen = new Set();
  const visit = (value) => {
    if (typeof value === "string") {
      return /^https:\/\//i.test(value) && /\.(mp4|webm|mov|mkv|gif)(\?|$)/i.test(value) ? value : null;
    }
    if (!value || typeof value !== "object" || seen.has(value)) return null;
    seen.add(value);
    if (Array.isArray(value)) {
      for (const item of value) {
        const found = visit(item);
        if (found) return found;
      }
      return null;
    }
    for (const key of ["video_url", "video", "output_url", "url", "outputs"]) {
      const found = visit(value[key]);
      if (found) return found;
    }
    for (const child of Object.values(value)) {
      const found = visit(child);
      if (found) return found;
    }
    return null;
  };
  return visit(payload?.output_data || payload?.output || payload?.result || payload?.data || payload);
}

function taskStatus(payload) {
  return String(payload?.status || payload?.task_status || payload?.prediction?.status || payload?.data?.status || "processing").toLowerCase();
}

function sleep(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function waitForTask(requestId) {
  const started = Date.now();
  const terminalSuccess = new Set(["completed", "succeeded", "success", "done"]);
  const terminalFailure = new Set(["failed", "error", "cancelled", "canceled"]);
  while (Date.now() - started < 45 * 60 * 1000) {
    const result = await api("/api/predictions/" + encodeURIComponent(requestId), {
      headers: { "x-api-key": state.apiKey },
    });
    const status = taskStatus(result);
    showProgress(requestId, status);
    if (terminalSuccess.has(status)) {
      const videoUrl = findVideoUrl(result);
      if (!videoUrl) throw new Error("The request completed, but no video URL was returned.");
      return { result, videoUrl };
    }
    if (terminalFailure.has(status)) {
      const detail = result?.error_message || result?.error?.message || result?.output_data?.error || "The service could not complete this request.";
      const error = new Error(String(detail));
      error.terminal = true;
      throw error;
    }
    await sleep(3500);
  }
  throw new Error("This request is taking longer than expected. Resume status checks before submitting another generation.");
}

async function uploadOne(file, kind) {
  const form = new FormData();
  form.set("api_key", state.apiKey);
  form.set("kind", kind);
  form.set("file", file, file.name);
  return api("/api/upload", { method: "POST", body: form });
}

function setObjectUrl(videoOrImage, field, file) {
  if (state[field]) URL.revokeObjectURL(state[field]);
  state[field] = file ? URL.createObjectURL(file) : null;
  if (state[field]) {
    videoOrImage.src = state[field];
    videoOrImage.classList.remove("hidden");
  } else {
    videoOrImage.classList.add("hidden");
    videoOrImage.removeAttribute("src");
  }
}

elements.apiKey.addEventListener("input", updateControls);
elements.sourceFile.addEventListener("change", () => {
  state.sourceFile = elements.sourceFile.files?.[0] || null;
  elements.sourceName.textContent = state.sourceFile?.name || "Choose your clip";
  setObjectUrl(elements.sourcePreview, "sourceObjectUrl", state.sourceFile);
  updateControls();
});
elements.referenceFile.addEventListener("change", () => {
  state.referenceFile = elements.referenceFile.files?.[0] || null;
  elements.referenceName.textContent = state.referenceFile?.name || "Choose a clear image";
  setObjectUrl(elements.referencePreview, "referenceObjectUrl", state.referenceFile);
  updateControls();
});

$("#toggle-key").addEventListener("click", () => {
  const isHidden = elements.apiKey.type === "password";
  elements.apiKey.type = isHidden ? "text" : "password";
  $("#toggle-key").textContent = isHidden ? "Hide" : "Show";
});

elements.uploadButton.addEventListener("click", async () => {
  clearError();
  state.apiKey = elements.apiKey.value.trim();
  if (!state.apiKey || !state.sourceFile || !state.referenceFile) return;
  state.busy = true;
  updateControls();
  elements.uploadButton.textContent = "Uploading…";
  elements.uploadStatus.textContent = "Uploading source video and character image…";
  try {
    const [source, reference] = await Promise.all([
      uploadOne(state.sourceFile, "source"),
      uploadOne(state.referenceFile, "reference"),
    ]);
    state.sourceAssetId = source.asset_id;
    state.referenceAssetId = reference.asset_id;
    state.uploaded = true;
    elements.uploadStatus.innerHTML = '<span class="uploaded-check">✓</span> Ready. Review both inputs, then submit one recast request.';
    elements.uploadButton.classList.add("hidden");
    elements.recastButton.classList.remove("hidden");
    elements.recastButton.disabled = false;
    setStep(2);
  } catch (error) {
    setError(error.message);
    elements.uploadStatus.textContent = "Upload did not finish. Check your key and try again.";
  } finally {
    state.busy = false;
    if (!state.uploaded) elements.uploadButton.textContent = "Upload media →";
    updateControls();
  }
});

elements.recastButton.addEventListener("click", async () => {
  clearError();
  state.apiKey = elements.apiKey.value.trim() || state.apiKey;
  if (!state.uploaded || !state.apiKey || state.submissionUnknown || state.finished) return;
  if (!state.ffmpeg) return setError("Install FFmpeg and ffprobe before generating. They check the source clip and restore its soundtrack locally.");
  state.busy = true;
  updateControls();
  setStep(2);
  showProgress(state.requestId, state.resumeOnly ? "processing" : "submitting");
  elements.recastButton.textContent = state.resumeOnly ? "Checking status…" : "Submitting recast…";
  try {
    if (!state.resumeOnly) {
      const submitted = await api("/api/recast", {
        method: "POST",
        json: {
          api_key: state.apiKey,
          source_asset_id: state.sourceAssetId,
          reference_asset_id: state.referenceAssetId,
          prompt: elements.prompt.value.trim(),
        },
      });
      state.requestId = submitted.request_id;
      if (!state.requestId) {
        state.submissionUnknown = true;
        throw new Error("The service did not return a recast request ID. Check generation history before retrying.");
      }
    }
    const completed = await waitForTask(state.requestId);
    showProgress(state.requestId, "completed");
    elements.progressTitle.textContent = "Preparing your download…";
    elements.progressCopy.textContent = "The generated video is being saved locally with the source soundtrack, if present.";
    const exported = await api("/api/export", {
      method: "POST",
      json: { source_asset_id: state.sourceAssetId, video_url: completed.videoUrl },
    });
    state.resumeOnly = false;
    state.finished = true;
    elements.resultVideo.src = exported.download_url;
    elements.resultLink.href = completed.videoUrl;
    elements.downloadLink.href = exported.download_url;
    elements.downloadLink.download = "open-genjutsu-recast.mp4";
    $("#export-headline").textContent = exported.source_audio_restored ? "Original soundtrack restored." : "Recast render ready.";
    $("#export-copy").textContent = exported.source_audio_restored
      ? "The complete source soundtrack has been muxed into the recast locally."
      : "The source clip has no audio track, so the generated video was exported as-is.";
    elements.resultPanel.classList.remove("hidden");
    elements.progress.classList.add("hidden");
    setStep(3);
    elements.resultPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    elements.progress.classList.add("hidden");
    if (error.terminal) {
      state.requestId = null;
      state.resumeOnly = false;
      setError("The service marked this recast as failed. Check request history before retrying. " + error.message);
    } else if (state.requestId) {
      state.resumeOnly = true;
      setError("Recast request " + state.requestId + " was submitted. Resume status checks here to avoid a duplicate generation. " + error.message);
    } else if (state.submissionUnknown || Number(error.status) >= 500) {
      state.submissionUnknown = true;
      setError("The service did not confirm whether the recast was accepted. Check generation history before trying again; this button is locked to avoid a duplicate charge.");
    } else {
      setError(error.message);
    }
  } finally {
    state.busy = false;
    elements.recastButton.textContent = state.resumeOnly ? "Resume status checks →" : state.submissionUnknown ? "Check generation history" : "Start recast →";
    updateControls();
  }
});

$("#clear-button").addEventListener("click", async () => {
  if (state.busy) {
    setError("Wait for the current upload or recast request to finish before clearing the session.");
    return;
  }
  try { await api("/api/clear", { method: "POST", json: {} }); } catch { /* Clear browser state even if server cleanup is unavailable. */ }
  state.apiKey = "";
  state.sourceFile = state.referenceFile = null;
  state.sourceAssetId = state.referenceAssetId = null;
  state.uploaded = false;
  state.requestId = null;
  state.resumeOnly = false;
  state.submissionUnknown = false;
  state.finished = false;
  elements.apiKey.value = "";
  elements.sourceFile.value = "";
  elements.referenceFile.value = "";
  [elements.sourcePreview, elements.resultVideo].forEach((video) => {
    video.pause();
    video.removeAttribute("src");
    video.load();
  });
  elements.referencePreview.removeAttribute("src");
  if (state.sourceObjectUrl) URL.revokeObjectURL(state.sourceObjectUrl);
  if (state.referenceObjectUrl) URL.revokeObjectURL(state.referenceObjectUrl);
  state.sourceObjectUrl = state.referenceObjectUrl = null;
  elements.resultLink.href = "#";
  elements.downloadLink.href = "#";
  elements.resultPanel.classList.add("hidden");
  elements.progress.classList.add("hidden");
  elements.recastButton.classList.add("hidden");
  elements.recastButton.textContent = "Start recast →";
  elements.uploadButton.classList.remove("hidden");
  elements.uploadButton.textContent = "Upload media →";
  elements.uploadStatus.textContent = "Add both files and your API key to continue.";
  elements.sourceName.textContent = "Choose your clip";
  elements.referenceName.textContent = "Choose a clear image";
  $("#toggle-key").textContent = "Show";
  elements.apiKey.type = "password";
  setStep(1);
  clearError();
  updateControls();
});

$("#new-recast-button").addEventListener("click", () => $("#clear-button").click());

api("/api/health").then((health) => {
  state.ffmpeg = Boolean(health.ffmpeg && health.ffprobe);
  if (state.ffmpeg) {
    elements.requirements.innerHTML = '<span class="requirements-icon good">✓</span><div><strong>FFmpeg ready</strong><span>Clip checks and source-audio restoration are available.</span></div>';
  } else {
    elements.requirements.innerHTML = '<span class="requirements-icon warn">!</span><div><strong>FFmpeg not found</strong><span>Install FFmpeg and ffprobe to check the clip and restore source audio.</span></div>';
  }
}).catch(() => {
  elements.requirements.innerHTML = '<span class="requirements-icon warn">!</span><div><strong>Local service unavailable</strong><span>Restart the app server, then refresh this page.</span></div>';
});
