from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
ASSET_DIR = RUNTIME / "assets"
EXPORT_DIR = RUNTIME / "exports"
STATIC_DIR = ROOT / "static"
API_BASE = "https://api.muapi.ai/api/v1"
MAX_VIDEO_BYTES = 50 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MIN_VIDEO_SECONDS = 4
MAX_VIDEO_SECONDS = 30
ASSETS: dict[str, dict[str, Any]] = {}
EXPORTS: dict[str, Path] = {}
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

for directory in (ASSET_DIR, EXPORT_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Open Genjutsu", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class RecastRequest(BaseModel):
    api_key: str = Field(min_length=8, max_length=500)
    source_asset_id: str
    reference_asset_id: str
    prompt: str = Field(default="", max_length=4000)


class ExportRequest(BaseModel):
    source_asset_id: str
    video_url: str


class ClearRequest(BaseModel):
    source_asset_id: str | None = None


def _safe_id(value: str, label: str) -> str:
    if not ID_PATTERN.fullmatch(value or ""):
        raise HTTPException(status_code=400, detail=f"Invalid {label}.")
    return value


def _api_key(value: str) -> str:
    key = value.strip()
    if not key or "\n" in key or "\r" in key:
        raise HTTPException(status_code=400, detail="Enter a valid API key.")
    return key


def _message_from_response(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(detail.get("message"), str):
            return detail["message"]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(payload.get("message"), str):
            return payload["message"]
    return f"The generation API returned HTTP {response.status_code}. Check the API key, credits, and request fields."


async def _api_request(method: str, path: str, key: str, **kwargs: Any) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}))
    headers["x-api-key"] = _api_key(key)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=20), follow_redirects=False) as client:
            response = await client.request(method, f"{API_BASE}/{path.lstrip('/')}", headers=headers, **kwargs)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="The generation API did not respond in time. Check request status before retrying to avoid a duplicate generation.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not reach the generation API. Check your connection and try again.") from exc
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=_message_from_response(response))
    return response


def _validate_source_video(data: bytes, suffix: str) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise HTTPException(status_code=503, detail="ffprobe is required to check source-video timing. Install FFmpeg and restart the app.")
    with tempfile.TemporaryDirectory(prefix="recast-probe-", dir=RUNTIME) as temporary_dir:
        sample = Path(temporary_dir) / f"source{suffix}"
        sample.write_bytes(data)
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(sample)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    if result.returncode != 0:
        raise HTTPException(status_code=422, detail="This source video could not be read. Export it as a valid MP4 or MOV and try again.")
    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="The source video duration could not be read.") from exc
    if duration < MIN_VIDEO_SECONDS or duration > MAX_VIDEO_SECONDS:
        raise HTTPException(status_code=422, detail=f"Source videos must be between {MIN_VIDEO_SECONDS} and {MAX_VIDEO_SECONDS} seconds so the edit can preserve the full performance and soundtrack.")
    return duration


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
    }


@app.post("/api/upload")
async def upload_media(
    api_key: str = Form(...),
    kind: str = Form(...),
    file: UploadFile = File(...),
) -> dict[str, str]:
    if kind not in {"source", "reference"}:
        raise HTTPException(status_code=400, detail="Upload must be a source video or a reference image.")
    content_type = (file.content_type or "").lower()
    filename = Path(file.filename or "upload").name
    suffix = Path(filename).suffix.lower()
    allowed = {"source": {"video/mp4", "video/quicktime", "application/octet-stream"}, "reference": {"image/jpeg", "image/png", "image/webp"}}
    max_bytes = MAX_VIDEO_BYTES if kind == "source" else MAX_IMAGE_BYTES
    if content_type not in allowed[kind]:
        raise HTTPException(status_code=415, detail="Use an MP4 or MOV source video and a JPEG, PNG, or WebP character image.")
    data = await file.read(max_bytes + 1)
    if not data:
        raise HTTPException(status_code=400, detail="The selected file is empty.")
    if len(data) > max_bytes:
        limit_mb = max_bytes // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"This file is over the {limit_mb} MB upload limit.")

    if kind == "source" and suffix not in {".mp4", ".mov"}:
        suffix = ".mp4" if content_type == "video/mp4" else ".mov"
    if kind == "reference" and suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[content_type]

    duration = _validate_source_video(data, suffix) if kind == "source" else None

    asset_id = uuid.uuid4().hex
    local_path = None
    if kind == "source":
        local_path = ASSET_DIR / f"{asset_id}{suffix}"
        local_path.write_bytes(data)
    try:
        response = await _api_request(
            "POST",
            "upload_file",
            api_key,
            files={"file": (filename or f"upload{suffix}", data, content_type)},
        )
    except Exception:
        if local_path:
            local_path.unlink(missing_ok=True)
        raise
    try:
        uploaded_url = response.json().get("url")
    except ValueError:
        uploaded_url = None
    if not isinstance(uploaded_url, str) or not uploaded_url.startswith("https://"):
        if local_path:
            local_path.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail="The service accepted the upload but did not return a usable media URL.")

    ASSETS[asset_id] = {"kind": kind, "path": local_path, "name": filename, "url": uploaded_url, "duration": duration}
    return {"asset_id": asset_id, "url": uploaded_url, "filename": filename}


@app.post("/api/recast")
async def create_recast(request: RecastRequest) -> dict[str, Any]:
    source = ASSETS.get(_safe_id(request.source_asset_id, "source asset id"))
    reference = ASSETS.get(_safe_id(request.reference_asset_id, "reference asset id"))
    if not source or source.get("kind") != "source" or not source.get("url") or not source.get("path"):
        raise HTTPException(status_code=404, detail="The source video is no longer available. Upload it again.")
    if not reference or reference.get("kind") != "reference" or not reference.get("url"):
        raise HTTPException(status_code=404, detail="The character image is no longer available. Upload it again.")

    duration = max(MIN_VIDEO_SECONDS, min(MAX_VIDEO_SECONDS, int(round(float(source.get("duration") or 5)))))
    payload = {
        "video_url": source["url"],
        "images_list": [reference["url"]],
        "aspect_ratio": "adaptive",
        "duration": duration,
        "generate_audio": False,
    }
    prompt = request.prompt.strip()
    if prompt:
        payload["prompt"] = prompt

    response = await _api_request("POST", "seedance-2.5-motion-control", request.api_key, json=payload)
    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="The service returned an unreadable recast response.") from exc
    if not result.get("request_id"):
        raise HTTPException(status_code=502, detail="The service did not return a recast request ID.")
    return result


@app.get("/api/predictions/{request_id}")
async def prediction(request_id: str, x_api_key: str = Header(..., alias="x-api-key")) -> dict[str, Any]:
    safe_request_id = _safe_id(request_id, "request ID")
    response = await _api_request("GET", f"predictions/{safe_request_id}/result", x_api_key)
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="The service returned an unreadable prediction response.") from exc


def _validate_result_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="The result URL must be a public HTTPS URL.")
    return value


def _has_audio_track(path: Path) -> bool:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise HTTPException(status_code=503, detail="ffprobe is required to inspect the source audio. Install FFmpeg and restart the app.")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=422, detail="The source video could not be read by ffprobe.")
    return bool(result.stdout.strip())


async def _download_result(url: str, destination: Path) -> None:
    url = _validate_result_url(url)
    size = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=30), follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with destination.open("wb") as file_handle:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > 1024 * 1024 * 1024:
                            raise HTTPException(status_code=413, detail="The generated video is larger than the 1 GB local export limit.")
                        file_handle.write(chunk)
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not download the generated video. The result URL remains available in the generation service.") from exc
    if size == 0:
        raise HTTPException(status_code=502, detail="The generated video download was empty.")


@app.post("/api/export")
async def export_with_source_audio(request: ExportRequest) -> dict[str, Any]:
    asset_id = _safe_id(request.source_asset_id, "asset id")
    source = ASSETS.get(asset_id)
    if not source or source.get("kind") != "source" or not source.get("path"):
        raise HTTPException(status_code=404, detail="The source video is no longer available locally. Upload it again.")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(status_code=503, detail="FFmpeg is required to make the source-audio export. Install FFmpeg and restart the app.")

    export_id = uuid.uuid4().hex
    downloaded = EXPORT_DIR / f"{export_id}-generated.mp4"
    final_path = EXPORT_DIR / f"{export_id}-final.mp4"
    await _download_result(request.video_url, downloaded)
    source_path: Path = source["path"]

    if _has_audio_track(source_path):
        command = [
            ffmpeg, "-y", "-i", str(downloaded), "-i", str(source_path),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(final_path),
        ]
    else:
        command = [ffmpeg, "-y", "-i", str(downloaded), "-c", "copy", "-movflags", "+faststart", str(final_path)]
    result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=300, check=False)
    downloaded.unlink(missing_ok=True)
    if result.returncode != 0 or not final_path.exists() or final_path.stat().st_size == 0:
        final_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="FFmpeg could not create the final MP4. Check the input codecs and FFmpeg installation.")
    EXPORTS[export_id] = final_path
    return {"export_id": export_id, "download_url": f"/api/download/{export_id}", "source_audio_restored": _has_audio_track(source_path)}


@app.get("/api/download/{export_id}")
async def download_export(export_id: str) -> FileResponse:
    export_id = _safe_id(export_id, "export id")
    path = EXPORTS.get(export_id)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="This export is no longer available. Create it again.")
    return FileResponse(path, media_type="video/mp4", filename="open-genjutsu-final.mp4")


@app.post("/api/clear")
async def clear_local_files(request: ClearRequest) -> dict[str, bool]:
    if request.source_asset_id:
        asset_id = _safe_id(request.source_asset_id, "asset id")
        asset = ASSETS.pop(asset_id, None)
        path = asset.get("path") if asset else None
        if path:
            Path(path).unlink(missing_ok=True)
    else:
        for asset in ASSETS.values():
            path = asset.get("path")
            if path:
                Path(path).unlink(missing_ok=True)
        ASSETS.clear()
        for path in ASSET_DIR.glob("*"):
            if path.is_file():
                path.unlink(missing_ok=True)
    for path in EXPORTS.values():
        Path(path).unlink(missing_ok=True)
    EXPORTS.clear()
    for path in EXPORT_DIR.glob("*"):
        if path.is_file():
            path.unlink(missing_ok=True)
    return {"ok": True}
