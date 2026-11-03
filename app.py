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
from typing import Any, Literal
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
MAX_VIDEO_SECONDS = 15
MAX_REFERENCE_IMAGES = 8
MAX_WAN_IMAGES = 10
MAX_ENVPLATE_FRAMES = 4
WAN30_MODEL = "wan3.0-reference-to-video"
SEEDANCE25_MODEL = "seedance-2.5-omni-reference"
DEPTH_MODEL = "depth-anything"
FRAME_ANALYSIS_MODEL = "gemini-video-vision"
PROMPT_MODEL = "gemini-2-5-flash"
ENVPLATE_MODEL = "nano-banana-2-edit"
CHARACTER_SHEET_MODEL = "gpt-image-2-image-to-image"
ASSETS: dict[str, dict[str, Any]] = {}
EXPORTS: dict[str, Path] = {}
JOBS: dict[str, dict[str, Any]] = {}
BACKGROUND_TASKS: set[asyncio.Task] = set()
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

for directory in (ASSET_DIR, EXPORT_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Genjutsu", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class RecastRequest(BaseModel):
    api_key: str = Field(min_length=8, max_length=500)
    source_asset_id: str
    reference_asset_ids: list[str] = Field(min_length=1, max_length=MAX_REFERENCE_IMAGES)
    model: Literal["seedance", "wan"] = "wan"
    prompt: str = Field(default="", max_length=4000)
    mode: Literal["motion_transfer", "objects_swap"] = "motion_transfer"
    aspect_ratio: Literal["adaptive", "16:9", "9:16", "1:1", "4:3", "3:4"] = "adaptive"
    duration: int = Field(default=5, ge=2, le=30)


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
        raise HTTPException(status_code=422, detail=f"Source videos must be between {MIN_VIDEO_SECONDS} and {MAX_VIDEO_SECONDS} seconds. Wan 3.0 also requires the source and generated video to fit its 30-second combined reference limit.")
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
) -> dict[str, Any]:
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

    ASSETS[asset_id] = {
        "kind": kind,
        "path": local_path,
        "name": filename,
        "url": uploaded_url,
        "duration": duration,
    }
    return {"asset_id": asset_id, "url": uploaded_url, "filename": filename, "duration": duration}


def _job_progress(job_id: str, label: str, message: str) -> None:
    job = JOBS.get(job_id)
    if job is not None:
        job.update({"status": "processing", "progress_label": label, "progress_message": message})


def _task_status(payload: dict[str, Any]) -> str:
    prediction = payload.get("prediction") if isinstance(payload.get("prediction"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    output_data = payload.get("output_data") if isinstance(payload.get("output_data"), dict) else {}
    return str(
        payload.get("status")
        or payload.get("task_status")
        or prediction.get("status")
        or data.get("status")
        or output.get("status")
        or output_data.get("status")
        or "processing"
    ).lower()


def _extract_text(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("text", "answer", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("output_data", "output", "result", "data"):
            found = _extract_text(payload.get(key))
            if found:
                return found
        for value in payload.values():
            found = _extract_text(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _extract_text(value)
            if found:
                return found
    return None


def _extract_media_url(payload: Any) -> str | None:
    media_keys = ("video_url", "video", "image_url", "image", "output_url", "url", "outputs")
    if isinstance(payload, dict):
        for key in media_keys:
            value = payload.get(key)
            found = _extract_media_url(value)
            if found:
                return found
        for key in ("output_data", "output", "result", "data"):
            found = _extract_media_url(payload.get(key))
            if found:
                return found
        for value in payload.values():
            found = _extract_media_url(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _extract_media_url(value)
            if found:
                return found
    elif isinstance(payload, str) and payload.startswith("https://"):
        parsed = urlparse(payload)
        if parsed.hostname and not parsed.username and not parsed.password:
            return payload
    return None


def _extract_error(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("error_message", "error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = _extract_error(value)
                if nested:
                    return nested
        for key in ("output_data", "output", "result", "data"):
            nested = _extract_error(payload.get(key))
            if nested:
                return nested
    return None


async def _wait_for_muapi_task(job_id: str, api_key: str, request_id: str, stage_label: str, stage_message: str) -> dict[str, Any]:
    started = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - started < 45 * 60:
        response = await _api_request("GET", f"predictions/{_safe_id(request_id, 'request ID')}/result", api_key)
        try:
            result = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="Muapi returned an unreadable stage result.") from exc
        status = _task_status(result)
        _job_progress(job_id, stage_label, stage_message)
        if status in {"completed", "succeeded", "success", "done"}:
            return result
        if status in {"failed", "error", "cancelled", "canceled"}:
            raise HTTPException(status_code=502, detail=_extract_error(result) or "A Muapi processing stage failed.")
        await asyncio.sleep(3.0)
    raise HTTPException(status_code=504, detail="A Muapi processing stage timed out. Check generation history before retrying.")


async def _run_muapi_stage(
    job_id: str,
    api_key: str,
    endpoint: str,
    payload: dict[str, Any],
    stage_label: str,
    stage_message: str,
) -> dict[str, Any]:
    _job_progress(job_id, stage_label, stage_message)
    response = await _api_request("POST", endpoint, api_key, json=payload)
    try:
        submitted = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Muapi returned an unreadable task response.") from exc
    request_id = submitted.get("request_id") if isinstance(submitted, dict) else None
    if not request_id:
        status = _task_status(submitted) if isinstance(submitted, dict) else ""
        if status in {"completed", "succeeded", "success", "done"}:
            return submitted
        raise HTTPException(status_code=502, detail="Muapi did not return a request ID for a processing stage.")
    return await _wait_for_muapi_task(job_id, api_key, str(request_id), stage_label, stage_message)


async def _upload_generated_image(api_key: str, image_bytes: bytes, filename: str) -> str:
    response = await _api_request(
        "POST",
        "upload_file",
        api_key,
        files={"file": (filename, image_bytes, "image/jpeg")},
    )
    try:
        url = response.json().get("url")
    except ValueError:
        url = None
    if not isinstance(url, str) or not url.startswith("https://"):
        raise HTTPException(status_code=502, detail="Muapi did not return a usable URL for a generated reference image.")
    return url


def _fallback_frame_timestamps(duration: float, maximum: int) -> list[float]:
    candidates = [duration * 0.15, duration * 0.5, duration * 0.85]
    cleaned = sorted({max(0.1, min(duration - 0.1, value)) for value in candidates if duration > 0.2})
    return cleaned[:maximum] or [max(0.1, duration / 2)]


async def _choose_environment_frames(job_id: str, api_key: str, source: dict[str, Any], character_count: int, model: str) -> tuple[list[float], str]:
    duration = float(source.get("duration") or 5)
    image_limit = MAX_WAN_IMAGES if model == "wan" else 30
    maximum = max(1, min(MAX_ENVPLATE_FRAMES, image_limit - character_count))
    prompt = (
        f"Analyze this {duration:.1f}-second reference video for a character-recast workflow. "
        f"Choose 1 to {maximum} timestamps, in seconds, for frames that best represent the background/location. "
        "Avoid frames near the first or last 5% of the video. Return one JSON object only, with keys "
        '"summary" (a concise description of the performer action and camera movement) and '
        '"timestamps" (an array of numeric seconds). Example: '
        '{"summary":"A person walks toward the camera as it pans left.","timestamps":[1.2,4.8]}.'
    )
    result = await _run_muapi_stage(
        job_id,
        api_key,
        FRAME_ANALYSIS_MODEL,
        {"prompt": prompt, "video_url": source["url"], "model": "gemini-2.5-flash"},
        "ANALYZING SOURCE VIDEO",
        "Gemini Video Vision is reading the source motion and selecting environment frames.",
    )
    text_value = _extract_text(result) or ""
    summary = text_value
    timestamps: list[float] = []
    try:
        cleaned = re.sub(r"^```(?:json)?|```$", "", text_value.strip(), flags=re.MULTILINE).strip()
        candidate = json.loads(cleaned)
        if isinstance(candidate, dict):
            summary = str(candidate.get("summary") or candidate.get("motion_analysis") or text_value).strip()
            raw_timestamps = candidate.get("timestamps") or candidate.get("keyframe_timestamps") or []
        else:
            raw_timestamps = candidate
        if isinstance(raw_timestamps, list):
            timestamps = sorted({
                float(value) for value in raw_timestamps
                if isinstance(value, (int, float)) and 0.1 < float(value) < duration - 0.1
            })[:maximum]
    except (TypeError, ValueError, json.JSONDecodeError):
        timestamps = []
    if not timestamps:
        timestamps = _fallback_frame_timestamps(duration, maximum)
    return timestamps, summary or "Use the source video for motion, choreography, and camera guidance."


async def _extract_video_frame(source_path: Path, timestamp: float, frame_index: int) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(status_code=503, detail="FFmpeg is required to extract environment frames. Install FFmpeg and restart the app.")
    with tempfile.TemporaryDirectory(prefix="genjutsu-frame-", dir=RUNTIME) as temporary_dir:
        output_path = Path(temporary_dir) / f"frame-{frame_index}.jpg"
        result = await asyncio.to_thread(
            subprocess.run,
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.3f}", "-i", str(source_path), "-frames:v", "1", "-q:v", "2", str(output_path)],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if result.returncode != 0 or not output_path.exists():
            raise HTTPException(status_code=422, detail="Could not extract a selected environment frame from the source video.")
        return output_path.read_bytes()


async def _download_image_bytes(url: str) -> bytes:
    url = _validate_result_url(url)
    maximum = 25 * 1024 * 1024
    total = 0
    chunks: list[bytes] = []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=30), follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > maximum:
                    raise HTTPException(status_code=413, detail="A generated reference image is too large to prepare the prompt contact sheet.")
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > maximum:
                        raise HTTPException(status_code=413, detail="A generated reference image is too large to prepare the prompt contact sheet.")
                    chunks.append(chunk)
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Could not retrieve a generated reference image for prompt preparation.") from exc
    return b"".join(chunks)


def _build_prompt_contact_sheet(items: list[tuple[str, bytes]]) -> bytes:
    from PIL import Image, ImageDraw, ImageOps

    columns = 3
    tile_width = 400
    tile_height = 360
    label_height = 44
    rows = max(1, (len(items) + columns - 1) // columns)
    sheet = Image.new("RGB", (columns * tile_width, rows * (tile_height + label_height)), "#171715")
    draw = ImageDraw.Draw(sheet)
    for index, (label, image_bytes) in enumerate(items):
        with Image.open(io.BytesIO(image_bytes)) as opened:
            image = ImageOps.contain(opened.convert("RGB"), (tile_width - 20, tile_height - 20))
        column = index % columns
        row = index // columns
        left = column * tile_width + (tile_width - image.width) // 2
        top = row * (tile_height + label_height) + label_height + (tile_height - image.height) // 2
        sheet.paste(image, (left, top))
        draw.text((column * tile_width + 12, row * (tile_height + label_height) + 14), label[:58], fill="#f4f0e8")
    output = io.BytesIO()
    sheet.save(output, format="JPEG", quality=88, optimize=True)
    return output.getvalue()


def _translate_wan_reference_markers(prompt: str) -> str:
    return re.sub(r"@(video|image|audio)(\d+)", lambda match: f"{match.group(1).capitalize()}{match.group(2)}", prompt, flags=re.IGNORECASE)


async def _create_environment_plate(job_id: str, api_key: str, frame_url: str, frame_index: int) -> str:
    prompt = (
        "Remove every person from this image completely. Reconstruct the background behind them so this is "
        "the same empty location photographed from the same camera angle. Keep framing, lighting, colors, "
        "architecture, furniture, objects, and perspective unchanged. Do not add people or new objects."
    )
    try:
        result = await _run_muapi_stage(
            job_id,
            api_key,
            ENVPLATE_MODEL,
            {"prompt": prompt, "images_list": [frame_url], "aspect_ratio": "Auto", "resolution": "1k", "output_format": "jpg"},
            "CLEANING ENVIRONMENT REFERENCES",
            "Nano Banana 2 Edit is removing performers from selected frames while preserving the location.",
        )
    except Exception:
        # Seedream Pro Edit is the Muapi-backed image-edit fallback used by the same motion-control flow.
        result = await _run_muapi_stage(
            job_id,
            api_key,
            "seedream-5.0-pro-edit",
            {"prompt": prompt, "images_list": [frame_url], "aspect_ratio": "16:9", "resolution": "1K"},
            "CLEANING ENVIRONMENT REFERENCES",
            "Nano Banana 2 Edit was unavailable; Seedream Pro Edit is preparing the environment reference.",
        )
    url = _extract_media_url(result)
    if not url:
        raise HTTPException(status_code=502, detail=f"Environment frame {frame_index + 1} did not produce an image URL.")
    return url


_CHARACTER_SHEET_PROMPT = """SUBJECT = The exact person shown in the reference image. Preserve their face, bone structure, eye shape and color, nose, lips, skin tone, hair color, texture, and hairline. Treat identity consistency as the highest priority.
OUTFIT = {outfit_description}
STYLE = Photorealistic studio photography on a soft neutral background with even lighting.

Create a clean 16:9 character sheet with evenly spaced, clearly separated panels: full-body front view, full-body back view, strict side profile, one dynamic action pose, and a row of close-up neutral/angry/happy/sad/focused expressions. Keep the same face, body proportions, hairstyle, outfit, colors, and materials in every panel. Add simple centered labels below each panel: FRONT VIEW, BACK VIEW, SIDE PROFILE, ACTION POSE, FACIAL EXPRESSIONS. Keep accessories consistent with the reference and avoid inventing prominent props. No extra text, overlapping panels, background clutter, altered identity, inconsistent anatomy, or motion blur."""


async def _create_character_sheet(job_id: str, api_key: str, reference_url: str, user_prompt: str, index: int) -> str:
    prompt = _CHARACTER_SHEET_PROMPT.format(
        outfit_description=user_prompt.strip() or "Keep the exact outfit shown in the reference image."
    )
    try:
        result = await _run_muapi_stage(
            job_id,
            api_key,
            CHARACTER_SHEET_MODEL,
            {"prompt": prompt, "images_list": [reference_url], "aspect_ratio": "16:9", "resolution": "2K", "quality": "high"},
            "BUILDING CHARACTER SHEETS",
            "GPT Image 2 is preparing a consistent character sheet for each supplied reference.",
        )
        url = _extract_media_url(result)
        if url:
            return url
    except Exception:
        pass
    return reference_url


async def _generate_recast_prompt(
    job_id: str,
    api_key: str,
    frame_items: list[tuple[str, bytes]],
    character_urls: list[str],
    environment_urls: list[str],
    video_analysis: str,
    mode: str,
    user_prompt: str,
    model: str,
) -> str:
    image_items = list(frame_items)
    image_numbers: list[tuple[str, str]] = []
    next_image = 1
    for index, url in enumerate(character_urls, start=1):
        image_items.append((f"FINAL IMAGE {next_image}: CHARACTER {index}", await _download_image_bytes(url)))
        image_numbers.append((f"Image{next_image}", f"character identity {index}"))
        next_image += 1
    for index, url in enumerate(environment_urls, start=1):
        image_items.append((f"FINAL IMAGE {next_image}: ENVIRONMENT {index}", await _download_image_bytes(url)))
        image_numbers.append((f"Image{next_image}", f"clean environment plate {index}"))
        next_image += 1

    contact_sheet = _build_prompt_contact_sheet(image_items)
    contact_sheet_url = await _upload_generated_image(api_key, contact_sheet, "genjutsu-prompt-contact-sheet.jpg")
    image_map = "; ".join(f"{marker} = {description}" for marker, description in image_numbers)
    mode_instruction = (
        "OBJECT SWAP: keep the filmed scene and motion; replace the requested character, product, or clothing using the character references."
        if mode == "objects_swap"
        else "MOTION TRANSFER: use the source video for body motion, choreography, blocking, timing, and camera movement; rebuild the performer using the character references."
    )
    user_brief = user_prompt.strip() or "Infer the recast details from the source video and reference images."
    is_seedance = model == "seedance"
    model_name = "Seedance 2.5 Omni Reference" if is_seedance else "Wan 3.0 Reference to Video"
    video_marker = "@video1" if is_seedance else "Video1"
    image_marker_map = "; ".join(
        f"@{marker.lower()} = {description}" if is_seedance else f"{marker} = {description}"
        for marker, description in image_numbers
    )
    reference_syntax = (
        f"Use Seedance reference labels exactly: {video_marker} for the source performance depth video and "
        f"{image_marker_map}. Keep those labels in the prompt."
        if is_seedance
        else f"Use Wan reference labels exactly (Video1, Image1, Image2), without @ notation. Video1 is the source performance converted to a depth map. The image mapping is: {image_map}."
    )
    prompt_structure = (
        "Structure the final prompt with these sections: REFERENCE KEY (label each reference role); SCENE AND CAST "
        "(rebuild performers from character references and keep the cleaned location); MOTION REFERENCE (body positions, "
        "gestures, foot placement, timing, entrances, exits, blocking, spacing, depth and occlusion); CAMERA (preserve "
        "height, angle, framing, trajectory and timing, without inventing camera moves); CONSISTENCY AND PHYSICS "
        "(identity, anatomy, fabric/hair, lighting, and no extra performers); PRIORITY ORDER (motion/camera, identity, "
        "location, then the user's brief)."
    )
    prompt = (
        f"Write one detailed video-generation prompt for {model_name}. {reference_syntax}\n\n"
        "The attached contact sheet labels source keyframes and every final image reference. Use it to understand the "
        "performer identity and the cleaned location. Source video analysis: " + video_analysis + "\n\n"
        + mode_instruction + "\nUser direction: " + user_brief + "\n\n"
        + prompt_structure + "\n\n"
        f"Preserve a coherent scene, realistic anatomy and lighting, and keep character identity consistent. Clearly instruct {model_name} "
        f"to use {video_marker} for motion/camera guidance, character image references for identity and wardrobe, and environment image "
        f"references for the original location. Do not preserve source performer identity or wardrobe. Do not mention model/provider names. "
        f"Return only the final prompt text, with no preamble or JSON."
    )
    result = await _run_muapi_stage(
        job_id,
        api_key,
        PROMPT_MODEL,
        {
            "prompt": prompt,
            "image_url": contact_sheet_url,
            "system_prompt": f"You are an expert video prompt writer. Produce a specific, production-ready recast prompt that follows the requested structure, preserves the intended reference syntax, and never mentions an underlying model or provider.",
        },
        "WRITING THE SEEDANCE PROMPT" if is_seedance else "WRITING THE WAN PROMPT",
        f"Gemini 2.5 Flash is combining the motion analysis, character sheets, environment plates, and your direction for {model_name}.",
    )
    generated = _extract_text(result)
    if not generated:
        raise HTTPException(status_code=502, detail=f"Gemini did not return a usable {model_name} generation prompt.")
    if is_seedance:
        expected_markers = ["@video1"] + [f"@{marker.lower()}" for marker, _ in image_numbers]
        if not all(re.search(re.escape(marker) + r"\b", generated, flags=re.IGNORECASE) for marker in expected_markers):
            reference_key = ["REFERENCE KEY:", "@video1 = motion, choreography, blocking, timing, and camera reference only"]
            reference_key.extend(f"@{marker.lower()} = {description}" for marker, description in image_numbers)
            generated = "\n".join(reference_key) + "\n\n" + generated
        return generated.strip()
    return _translate_wan_reference_markers(generated)


async def _run_recast_pipeline(
    job_id: str,
    api_key: str,
    source: dict[str, Any],
    references: list[dict[str, Any]],
    mode: str,
    user_prompt: str,
    aspect_ratio: str,
    output_duration: int,
    model: str,
) -> None:
    try:
        source_duration = float(source.get("duration") or 5)
        depth_result = await _run_muapi_stage(
            job_id,
            api_key,
            DEPTH_MODEL,
            {"video_url": source["url"]},
            "BUILDING THE DEPTH VIDEO",
            "Depth Anything is extracting motion and structure from the source clip.",
        )
        depth_url = _extract_media_url(depth_result)
        if not depth_url:
            raise HTTPException(status_code=502, detail="Depth Anything did not return a depth video.")

        timestamps, video_analysis = await _choose_environment_frames(job_id, api_key, source, len(references), model)
        frame_items: list[tuple[str, bytes]] = []
        frame_urls: list[str] = []
        _job_progress(job_id, "EXTRACTING ENVIRONMENT FRAMES", "Extracting the selected source frames for background cleanup.")
        for index, timestamp in enumerate(timestamps):
            frame_bytes = await _extract_video_frame(Path(source["path"]), timestamp, index)
            frame_items.append((f"SOURCE KEYFRAME {index + 1} · {timestamp:.2f}s", frame_bytes))
            frame_urls.append(await _upload_generated_image(api_key, frame_bytes, f"genjutsu-source-frame-{index + 1}.jpg"))

        environment_results = await asyncio.gather(*[
            _create_environment_plate(job_id, api_key, frame_url, index)
            for index, frame_url in enumerate(frame_urls)
        ])

        character_results = await asyncio.gather(*[
            _create_character_sheet(job_id, api_key, reference["url"], user_prompt, index)
            for index, reference in enumerate(references)
        ])
        max_images = MAX_WAN_IMAGES if model == "wan" else 30
        if len(character_results) + len(environment_results) > max_images:
            limit = "Wan 3.0's 10-image limit" if model == "wan" else "Seedance 2.5's 30-image limit"
            raise HTTPException(status_code=422, detail=f"The selected references exceed {limit}.")

        final_prompt = await _generate_recast_prompt(
            job_id,
            api_key,
            frame_items,
            character_results,
            environment_results,
            video_analysis,
            mode,
            user_prompt,
            model,
        )
        is_seedance = model == "seedance"
        model_name = "Seedance 2.5" if is_seedance else "Wan 3.0"
        if model == "wan" and source_duration + output_duration > 30:
            raise HTTPException(status_code=422, detail="The source reference and output duration exceed Wan 3.0's 30-second combined limit.")
        if is_seedance and output_duration < 4:
            raise HTTPException(status_code=422, detail="Seedance 2.5 output duration must be at least 4 seconds.")
        final_endpoint = SEEDANCE25_MODEL if is_seedance else WAN30_MODEL
        final_payload = {
            "prompt": final_prompt,
            "images_list": character_results + environment_results,
            "videos_list": [depth_url],
            "aspect_ratio": aspect_ratio,
            "duration": output_duration,
            "resolution": "720p",
        }
        if not is_seedance:
            final_payload.update({"thinking_mode": False, "enable_audio": False})
        final_result = await _run_muapi_stage(
            job_id,
            api_key,
            final_endpoint,
            final_payload,
            f"RENDERING WITH {model_name.upper()}",
            f"{model_name} is rendering a new take from the depth video, character sheets, environment plates, and generated prompt.",
        )
        video_url = _extract_media_url(final_result)
        if not video_url:
            raise HTTPException(status_code=502, detail=f"{model_name} completed without returning a video URL.")
        JOBS[job_id].update({
            "status": "completed",
            "progress_label": "COMPLETE",
            "progress_message": f"{model_name} finished. Preparing your local export with the source soundtrack.",
            "output_data": {"video_url": video_url},
        })
    except Exception as exc:
        if isinstance(exc, HTTPException):
            message = str(exc.detail)
        else:
            message = "The staged recast could not be completed. " + str(exc)
        JOBS[job_id].update({
            "status": "failed",
            "progress_label": "GENERATION FAILED",
            "progress_message": "A model stage failed. Review the error before submitting another generation.",
            "error_message": message[:2000],
        })


@app.post("/api/recast")
async def create_recast(request: RecastRequest) -> dict[str, Any]:
    source = ASSETS.get(_safe_id(request.source_asset_id, "source asset id"))
    if not source or source.get("kind") != "source" or not source.get("url") or not source.get("path"):
        raise HTTPException(status_code=404, detail="The source video is no longer available. Upload it again.")
    references: list[dict[str, Any]] = []
    for reference_asset_id in request.reference_asset_ids:
        reference = ASSETS.get(_safe_id(reference_asset_id, "character reference asset id"))
        if not reference or reference.get("kind") != "reference" or not reference.get("url"):
            raise HTTPException(status_code=404, detail="A character reference is no longer available. Upload it again.")
        references.append(reference)

    source_duration = float(source.get("duration") or 0)
    if request.model == "wan" and source_duration + request.duration > 30:
        max_duration = max(2, int(30 - source_duration))
        raise HTTPException(status_code=422, detail=f"Choose an output duration of {max_duration} seconds or less so the source and generated video fit Wan 3.0's 30-second combined limit.")
    if request.model == "seedance" and request.duration < 4:
        raise HTTPException(status_code=422, detail="Seedance 2.5 output duration must be between 4 and 30 seconds.")

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "status": "processing",
        "progress_label": "STARTING FLOW",
        "progress_message": "Preparing the staged motion-control flow.",
    }
    task = asyncio.create_task(
        _run_recast_pipeline(
            job_id,
            _api_key(request.api_key),
            source,
            references,
            request.mode,
            request.prompt.strip(),
            request.aspect_ratio,
            request.duration,
            request.model,
        )
    )
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return {"request_id": job_id, "status": "processing"}


@app.get("/api/predictions/{request_id}")
async def prediction(request_id: str, x_api_key: str = Header(..., alias="x-api-key")) -> dict[str, Any]:
    safe_request_id = _safe_id(request_id, "request ID")
    local_job = JOBS.get(safe_request_id)
    if local_job:
        return dict(local_job)
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
    return FileResponse(path, media_type="video/mp4", filename="genjutsu-lite-final.mp4")


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
