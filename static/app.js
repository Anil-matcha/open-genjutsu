const state = {
  apiKey: "",
  sourceFile: null,
  referenceFiles: [],
  sourceObjectUrl: null,
  referenceObjectUrls: [],
  sourceAssetId: null,
  referenceAssetIds: [],
  activeModel: "wan",
  sourceDuration: null,
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
  model: $("#model-select"),
  mode: $("#mode-select"),
  aspectRatio: $("#aspect-ratio"),
  duration: $("#duration-input"),
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

function flowName(model = elements.model.value) {
  return model === "seedance" ? "Genjutsu" : "Genjutsu Lite";
}

function engineName(model = elements.model.value) {
  return model === "seedance" ? "Seedance 2.5 Motion Control" : "Wan 3.0";
}

function updateModelPresentation() {
  const model = elements.model.value;
  const flow = flowName(model);
  $("#brand-mode").textContent = model === "seedance" ? "" : " lite";
  $("#model-hint").textContent = model === "seedance"
    ? "Seedance Motion Control uses the prepared depth video, character sheets, and clean environment plates to guide the recast."
    : "Wan 3.0 generates a new take from the prepared references; it may not reproduce motion frame for frame. Source and output together must fit 30 seconds.";
  if (!state.busy) {
    elements.recastButton.textContent = state.resumeOnly
      ? "Resume flow checks →"
      : state.submissionUnknown
        ? "Check generation history"
        : "Start " + flow + " flow →";
  }
  elements.downloadLink.textContent = "Download " + flow + " MP4 ↓";
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
  const hasInputs = Boolean(elements.apiKey.value.trim() && state.sourceFile && state.referenceFiles.length);
  elements.uploadButton.disabled = !hasInputs || state.busy || state.uploaded;
  elements.apiKey.disabled = state.busy || state.uploaded;
  elements.sourceFile.disabled = state.busy || state.uploaded;
  elements.referenceFile.disabled = state.busy || state.uploaded;
  elements.model.disabled = state.busy || state.resumeOnly || state.submissionUnknown || state.finished;
  elements.prompt.disabled = state.busy || !state.uploaded;
  elements.mode.disabled = state.busy;
  elements.aspectRatio.disabled = state.busy;
  elements.duration.disabled = state.busy || !state.uploaded;
  elements.recastButton.disabled = state.busy || !state.uploaded || state.submissionUnknown || state.finished;
  updateModelPresentation();
}

function setStep(activeStep) {
  document.querySelectorAll(".stepper-step").forEach((step) => {
    const number = Number(step.dataset.step);
    step.classList.toggle("active", number === activeStep);
    step.classList.toggle("done", number < activeStep);
  });
}

function showProgress(requestId, status = "processing", payload = {}) {
  elements.progress.classList.remove("hidden");
  elements.resultPanel.classList.add("hidden");
  elements.progressTitle.textContent = String(payload.progress_label || "").startsWith("RENDERING WITH ")
    ? "Rendering with " + engineName(state.activeModel) + "…"
    : "Preparing the recast flow…";
  elements.progressCopy.textContent = payload.progress_message || "Submitting the first Muapi stage. Keep this tab open while the flow runs.";
  elements.progressRequest.textContent = requestId ? "REQUEST " + requestId.slice(0, 12).toUpperCase() : "";
  const labels = { submitting: "SUBMITTING FLOW", processing: payload.progress_label || "PROCESSING STAGES", completed: "COMPLETE" };
  elements.progressLabel.textContent = labels[status] || payload.progress_label || "PROCESSING STAGES";
  elements.progressBar.classList.toggle("indeterminate", status !== "completed");
  elements.progressBar.classList.toggle("finished", status === "completed");
  $("#progress-eyebrow").textContent = payload.progress_label || "MUAPI MULTI-STAGE RECAST";
}

function findVideoUrl(payload) {
  const seen = new Set();
  const visit = (value, keyHint = "") => {
    if (typeof value === "string") {
      const mediaUrl = /\.(mp4|webm|mov|mkv|gif)(\?|$)/i.test(value);
      const videoField = ["video_url", "video", "output_url"].includes(keyHint);
      return /^https:\/\//i.test(value) && (mediaUrl || videoField) ? value : null;
    }
    if (!value || typeof value !== "object" || seen.has(value)) return null;
    seen.add(value);
    if (Array.isArray(value)) {
      for (const item of value) {
        const found = visit(item, keyHint);
        if (found) return found;
      }
      return null;
    }
    for (const key of ["video_url", "video", "output_url", "url", "outputs"]) {
      const found = visit(value[key], key);
      if (found) return found;
    }
    for (const child of Object.values(value)) {
      const found = visit(child, keyHint);
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
    showProgress(requestId, status, result);
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

function renderReferencePreviews(files) {
  state.referenceObjectUrls.forEach((url) => URL.revokeObjectURL(url));
  state.referenceObjectUrls = files.map((file) => URL.createObjectURL(file));
  elements.referencePreview.replaceChildren();
  state.referenceObjectUrls.forEach((url, index) => {
    const image = document.createElement("img");
    image.src = url;
    image.alt = "Character reference " + (index + 1);
    image.className = "reference-preview-thumb";
    elements.referencePreview.append(image);
  });
  elements.referencePreview.classList.toggle("hidden", files.length === 0);
}

function updateOutputDurationLimit(sourceDuration) {
  const seedance = elements.model.value === "seedance";
  const minDuration = seedance ? 4 : 2;
  const maxDuration = seedance ? 30 : Math.max(minDuration, Math.floor(30 - Number(sourceDuration || 0)));
  elements.duration.min = String(minDuration);
  elements.duration.max = String(maxDuration);
  const current = Number(elements.duration.value || 5);
  const suggested = Math.max(minDuration, Math.min(maxDuration, Number.isFinite(current) ? current : 5));
  elements.duration.value = String(Math.round(suggested));
}

elements.model.addEventListener("change", () => {
  updateOutputDurationLimit(state.sourceDuration);
  updateModelPresentation();
});

elements.apiKey.addEventListener("input", updateControls);
elements.sourceFile.addEventListener("change", () => {
  state.sourceFile = elements.sourceFile.files?.[0] || null;
  elements.sourceName.textContent = state.sourceFile?.name || "Choose your clip";
  setObjectUrl(elements.sourcePreview, "sourceObjectUrl", state.sourceFile);
  updateControls();
});
elements.referenceFile.addEventListener("change", () => {
  const selected = Array.from(elements.referenceFile.files || []);
  if (selected.length > 8) {
    setError("Choose up to 8 character images. Genjutsu Lite reserves room for environment plates within Wan 3.0's 10-image limit.");
  } else {
    clearError();
  }
  state.referenceFiles = selected.slice(0, 8);
  elements.referenceName.textContent = state.referenceFiles.length
    ? state.referenceFiles.length + " character reference" + (state.referenceFiles.length === 1 ? "" : "s")
    : "Choose one or more clear images";
  renderReferencePreviews(state.referenceFiles);
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
  if (!state.apiKey || !state.sourceFile || !state.referenceFiles.length) return;
  state.busy = true;
  updateControls();
  elements.uploadButton.textContent = "Uploading…";
  elements.uploadStatus.textContent = "Uploading source video and " + state.referenceFiles.length + " character reference" + (state.referenceFiles.length === 1 ? "…" : "s…");
  try {
    const [source, ...references] = await Promise.all([
      uploadOne(state.sourceFile, "source"),
      ...state.referenceFiles.map((file) => uploadOne(file, "reference")),
    ]);
    state.sourceAssetId = source.asset_id;
    state.referenceAssetIds = references.map((reference) => reference.asset_id);
    state.sourceDuration = Number(source.duration);
    updateOutputDurationLimit(state.sourceDuration);
    state.uploaded = true;
    elements.uploadStatus.innerHTML = '<span class="uploaded-check">✓</span> Ready. Review the source, character references, mode, and output settings.';
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
  const requestedModel = state.resumeOnly ? state.activeModel : elements.model.value;
  const requestedDuration = Number(elements.duration.value);
  const minimumDuration = requestedModel === "seedance" ? 4 : 2;
  const maximumDuration = Number(elements.duration.max);
  if (!Number.isInteger(requestedDuration) || requestedDuration < minimumDuration || requestedDuration > maximumDuration) {
    return setError("Choose an output duration between " + minimumDuration + " and " + maximumDuration + " seconds for " + flowName(requestedModel) + ".");
  }
  state.activeModel = requestedModel;
  state.busy = true;
  updateControls();
  setStep(2);
  showProgress(state.requestId, state.resumeOnly ? "processing" : "submitting");
  elements.recastButton.textContent = state.resumeOnly ? "Checking flow status…" : "Starting flow…";
  try {
    if (!state.resumeOnly) {
      const submitted = await api("/api/recast", {
        method: "POST",
        json: {
          api_key: state.apiKey,
          source_asset_id: state.sourceAssetId,
          reference_asset_ids: state.referenceAssetIds,
          model: state.activeModel,
          prompt: elements.prompt.value.trim(),
          mode: elements.mode.value,
          aspect_ratio: elements.aspectRatio.value,
          duration: Number(elements.duration.value),
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
    elements.progressCopy.textContent = "The " + engineName(state.activeModel) + " result is being saved locally with the source soundtrack, if present.";
    const exported = await api("/api/export", {
      method: "POST",
      json: { source_asset_id: state.sourceAssetId, video_url: completed.videoUrl },
    });
    state.resumeOnly = false;
    state.finished = true;
    elements.resultVideo.src = exported.download_url;
    elements.resultLink.href = completed.videoUrl;
    elements.downloadLink.href = exported.download_url;
    elements.downloadLink.download = state.activeModel === "seedance" ? "genjutsu.mp4" : "genjutsu-lite.mp4";
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
    updateModelPresentation();
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
  state.sourceFile = null;
  state.referenceFiles = [];
  state.sourceAssetId = null;
  state.referenceAssetIds = [];
  state.sourceDuration = null;
  state.uploaded = false;
  state.requestId = null;
  state.resumeOnly = false;
  state.submissionUnknown = false;
  state.finished = false;
  elements.apiKey.value = "";
  elements.sourceFile.value = "";
  elements.referenceFile.value = "";
  elements.duration.value = "5";
  elements.model.value = "wan";
  state.activeModel = "wan";
  updateOutputDurationLimit(null);
  [elements.sourcePreview, elements.resultVideo].forEach((video) => {
    video.pause();
    video.removeAttribute("src");
    video.load();
  });
  if (state.sourceObjectUrl) URL.revokeObjectURL(state.sourceObjectUrl);
  state.referenceObjectUrls.forEach((url) => URL.revokeObjectURL(url));
  state.sourceObjectUrl = null;
  state.referenceObjectUrls = [];
  elements.referencePreview.replaceChildren();
  elements.referencePreview.classList.add("hidden");
  elements.resultLink.href = "#";
  elements.downloadLink.href = "#";
  elements.resultPanel.classList.add("hidden");
  elements.progress.classList.add("hidden");
  elements.recastButton.classList.add("hidden");
  elements.recastButton.textContent = "Start Genjutsu Lite flow →";
  elements.uploadButton.classList.remove("hidden");
  elements.uploadButton.textContent = "Upload media →";
  elements.uploadStatus.textContent = "Add a source clip, character reference images, and your API key to continue.";
  elements.sourceName.textContent = "Choose your clip";
  elements.referenceName.textContent = "Choose one or more clear images";
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
